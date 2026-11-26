"""Video and audio with ffprobe and ffmpeg (milestone 7).

Two commands, both run without a shell and with a fixed argument list (SC3):

* ``ffprobe`` reads the headers: duration, codecs, resolution, bitrate and
  the music tags. It reads a few kilobytes, not the whole file.
* ``ffmpeg -c copy -f streamhash`` hashes the compressed packets of every
  audio and video stream, leaving out tags, cover art, chapters and the
  container. Two files with the same stream hash are the same recording,
  even when one was retagged or remuxed from MKV to MP4.

A crafted media file must not make ffmpeg read anything else, so both
commands only open local files (``-protocol_whitelist file``) and only with
the demuxers of ordinary media files (``-format_whitelist``): no playlists,
no ``concat:``, no network. Paths pass :func:`safe_for_ffmpeg` and are given
as ``file:/absolute/path`` so a name can never be read as an option or a
protocol. Every run has a time limit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction

from .categories import exts_for

log = logging.getLogger("deedup.av")

VIDEO_EXTS = frozenset(exts_for("video"))
AUDIO_EXTS = frozenset(exts_for("audio"))

# Demuxers allowed to open a file. Ordinary media only: no hls, concat, image2,
# subtitles or other formats that refer to further files or URLs.
FORMATS = ",".join([
    "mov,mp4,m4a,3gp,3g2,mj2", "matroska,webm", "avi", "asf", "flv", "mpeg", "mpegts", "mpegvideo",
    "mp3", "flac", "ogg", "wav", "w64", "aac", "aiff", "ape", "wv", "ac3", "eac3", "dts", "truehd",
    "mpc", "mpc8", "tta", "caf", "dsf", "amr", "h264", "hevc", "mxf", "dv", "m4v",
])
PROBE_TIMEOUT = 30          # seconds for ffprobe, which reads headers only
HASH_MIN_SPEED = 20e6       # bytes per second the stream hash is allowed, at worst
HASH_TIMEOUT_BASE = 60      # plus this many seconds

# Music tags, by the names ffprobe gives them (lower-cased).
TAG_FIELDS = ("artist", "album", "album_artist", "title", "track", "disc", "date", "genre")
UNSAFE_NAME = "name not readable by ffmpeg (it contains a line break or control character)"


def tools_available() -> bool:
    return shutil.which("ffprobe") is not None and shutil.which("ffmpeg") is not None


def safe_for_ffmpeg(path: str) -> bool:
    """SC3: absolute paths without control characters only. With the ``file:``
    prefix, ffmpeg reads the rest as a plain path."""
    return path.startswith("/") and not any(ord(ch) < 32 or ord(ch) == 127 for ch in path)


def _input_args(path: str) -> list[str]:
    if not safe_for_ffmpeg(path):
        raise ValueError(UNSAFE_NAME)
    return ["-protocol_whitelist", "file", "-format_whitelist", FORMATS, "-i", "file:" + path]


def kind_of(ext: str) -> str | None:
    ext = ext.lower()
    return "video" if ext in VIDEO_EXTS else "audio" if ext in AUDIO_EXTS else None


@dataclass
class Probe:
    kind: str = "audio"            # audio or video: what the streams say, not the extension
    container: str | None = None
    duration: float | None = None
    bitrate: int | None = None
    vcodec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    acodec: str | None = None
    channels: int | None = None
    sample_rate: int | None = None
    layout: str = ""               # stream signature used to find stream-hash candidates
    tags: dict[str, str] = field(default_factory=dict)
    has_cover: bool = False
    error: str | None = None

    @property
    def tag_count(self) -> int:
        """How complete the tags are: filled tag fields, plus one for cover art."""
        return sum(1 for k in TAG_FIELDS if self.tags.get(k)) + (1 if self.has_cover else 0)

    @property
    def year(self) -> str | None:
        d = self.tags.get("date") or ""
        return d[:4] if len(d) >= 4 and d[:4].isdigit() else None

    def number(self, key: str) -> int | None:
        """'3/12' -> 3."""
        v = (self.tags.get(key) or "").split("/")[0].strip()
        return int(v) if v.isdigit() else None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f > 0 else None  # not NaN, positive


def _rate(v) -> float | None:
    try:
        f = float(Fraction(str(v)))
    except (ValueError, ZeroDivisionError):
        return None
    return round(f, 3) if 0 < f < 1000 else None


def _clean(v) -> str:
    return " ".join(str(v).split())[:200]


def parse_probe(data: dict) -> Probe:
    """Turn ffprobe's JSON into a :class:`Probe`."""
    p = Probe()
    fmt = data.get("format") or {}
    p.container = (fmt.get("format_name") or "")[:40] or None
    p.duration = _float(fmt.get("duration"))
    p.bitrate = _int(fmt.get("bit_rate"))
    tags: dict[str, str] = {}
    for k, v in (fmt.get("tags") or {}).items():
        tags.setdefault(k.lower(), _clean(v))
    sig = []
    for s in data.get("streams") or []:
        ctype = s.get("codec_type")
        codec = (s.get("codec_name") or "?")[:20]
        if ctype == "video" and (s.get("disposition") or {}).get("attached_pic"):
            p.has_cover = True  # cover art: not part of the recording
            continue
        if ctype == "video":
            p.kind = "video"
            if p.vcodec is None:
                p.vcodec, p.width, p.height = codec, _int(s.get("width")), _int(s.get("height"))
                p.fps = _rate(s.get("avg_frame_rate")) or _rate(s.get("r_frame_rate"))
            sig.append(f"v:{codec}:{s.get('width')}x{s.get('height')}")
        elif ctype == "audio":
            if p.acodec is None:
                p.acodec, p.channels = codec, _int(s.get("channels"))
                p.sample_rate = _int(s.get("sample_rate"))
            sig.append(f"a:{codec}:{s.get('channels')}:{s.get('sample_rate')}")
        else:
            continue
        p.duration = p.duration or _float(s.get("duration"))
        for k, v in (s.get("tags") or {}).items():  # Ogg and FLAC keep tags on the stream
            tags.setdefault(k.lower(), _clean(v))
    # 'track' may be called 'tracknumber' (Vorbis comments); the year 'date' or 'year'.
    for alias, name in (("tracknumber", "track"), ("year", "date"), ("albumartist", "album_artist"),
                        ("discnumber", "disc")):
        if alias in tags and name not in tags:
            tags[name] = tags[alias]
    p.tags = {k: tags[k] for k in TAG_FIELDS if tags.get(k)}
    p.layout = "|".join(sig)
    if not sig:
        p.error = "no audio or video stream"
    return p


def _run(args: list[str], timeout: float) -> bytes:
    # SC3: a list of fixed options plus one checked path; never a shell.
    res = subprocess.run(args, capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)  # noqa: S603
    if res.returncode != 0:
        msg = res.stderr.decode(errors="replace").strip().splitlines()
        raise RuntimeError((msg[-1] if msg else f"exit code {res.returncode}")[:200])
    return res.stdout


def probe(path: str) -> Probe:
    """Read the headers of one file. Never raises: problems go in ``error``."""
    if not safe_for_ffmpeg(path):
        return Probe(error=UNSAFE_NAME)
    args = ["ffprobe", "-v", "error", "-hide_banner", *_input_args(path),
            "-print_format", "json", "-show_format", "-show_streams"]
    try:
        out = _run(args, PROBE_TIMEOUT)
        return parse_probe(json.loads(out or b"{}"))
    except subprocess.TimeoutExpired:
        return Probe(error="ffprobe took too long")
    except (RuntimeError, ValueError, OSError) as e:
        return Probe(error=f"ffprobe: {e}"[:300])


def stream_hash(path: str, size: int) -> bytes:
    """BLAKE2b-256 over the per-stream SHA-256 hashes of the compressed audio and
    video packets (cover art left out). Raises ``RuntimeError`` when ffmpeg
    cannot read the file."""
    args = ["ffmpeg", "-nostdin", "-v", "error", "-hide_banner", *_input_args(path),
            "-map", "0:V?", "-map", "0:a?", "-c", "copy", "-f", "streamhash", "-hash", "sha256", "-"]
    try:
        out = _run(args, HASH_TIMEOUT_BASE + size / HASH_MIN_SPEED)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg took too long") from None
    lines = []
    for line in out.decode(errors="replace").splitlines():
        # "0,v,SHA256=…": stream index, type, hash. The index is left out so
        # that the order of streams in the container does not matter.
        parts = line.strip().split(",", 2)
        if len(parts) == 3 and parts[2].startswith("SHA256="):
            lines.append(f"{parts[1]}:{parts[2][7:]}")
    if not lines:
        raise RuntimeError("no audio or video packets")
    return hashlib.blake2b("\n".join(sorted(lines)).encode(), digest_size=32).digest()

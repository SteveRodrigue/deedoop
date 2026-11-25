"""Read archives in place: zip, tar (plain, gz, bz2, xz) and 7z.

Nothing is extracted to disk. Each member is streamed through the same
hashes as a loose file (partial, BLAKE3 and, for photos, the image-data
hash), so members join the same duplicate groups and the coverage report
can tell what an archive holds that is not on disk yet. An archive inside
an archive is opened too, one level deep.

Google Takeout specifics live here as well: multi-part downloads
(``takeout-20240301T101010Z-001.zip``, ``-002.zip``, ...) form one set, and
each photo is paired with its JSON sidecar, following the naming rules
Google uses (truncated names, ``.supplemental-metadata``, ``(1)`` counters,
``-edited`` copies).
"""

from __future__ import annotations

import calendar
import datetime as dt
import io
import json
import os
import re
import stat
import tarfile
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import blake3
import xxhash

from .hashing import EDGE, SMALL, Expected, FileChanged, _open
from .imaging import IMAGE_HASH_EXTS, MAX_IMAGE_BYTES, NotAnImage, image_hash_for
from .metadata import Media, camera_name, normalise_date
from .scanner import ext_of

TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tbz", ".tar.xz", ".txz")
ARCHIVE_EXTS = {"zip", "7z", "tar", "gz", "tgz", "bz2", "tbz2", "tbz", "xz", "txz"}
# A nested archive is opened from memory when it is at most this big.
NESTED_LIMIT = 512 * 1024 * 1024
JSON_LIMIT = 4 * 1024 * 1024
CHUNK = 1024 * 1024

TAKEOUT_PART = re.compile(r"^(takeout-[0-9A-Za-z]+)-(\d{3})\.(zip|tgz|tar\.gz)$", re.IGNORECASE)


def archive_kind(name: str) -> str | None:
    n = name.lower()
    if n.endswith(".zip"):
        return "zip"
    if n.endswith(".7z"):
        return "7z"
    if n.endswith(TAR_SUFFIXES):
        return "tar"
    return None


def takeout_part(name: str) -> tuple[str, int] | None:
    m = TAKEOUT_PART.match(name)
    return (m.group(1).lower(), int(m.group(2))) if m else None


# -- streaming hashes ------------------------------------------------------------


class Sink:
    """Collects one member's bytes into the hashes, keeping a copy in memory
    only when it is needed (photos, nested archives, Takeout JSON)."""

    def __init__(self, keep_limit: int = 0):
        self.b3 = blake3.blake3()
        self.n = 0
        self.head = bytearray()
        self.tail = b""
        self.small = bytearray()
        self.keep_limit = keep_limit
        self.buf: bytearray | None = bytearray() if keep_limit else None

    def write(self, b: bytes) -> int:
        self.b3.update(b)
        self.n += len(b)
        if len(self.head) < EDGE:
            self.head += b[: EDGE - len(self.head)]
        self.tail = b[-EDGE:] if len(b) >= EDGE else (self.tail + b)[-EDGE:]
        if self.n <= SMALL:
            self.small += b
        if self.buf is not None:
            if len(self.buf) + len(b) > self.keep_limit:
                self.buf = None
            else:
                self.buf += b
        return len(b)

    def partial(self) -> bytes:
        h = xxhash.xxh3_128()
        h.update(self.n.to_bytes(8, "little"))
        if self.n <= SMALL:
            h.update(bytes(self.small))
        else:
            h.update(bytes(self.head))
            h.update(self.tail)
        return h.digest()


@dataclass
class Member:
    path: str  # inside the archive, '/'-separated
    size: int
    mtime_ns: int
    hashes: dict[str, bytes] = field(default_factory=dict)
    media: Media | None = None
    sidecar: dict | None = None  # parsed Takeout JSON
    error: str | None = None
    children: list[Member] = field(default_factory=list)
    nested_kind: str | None = None

    @property
    def dir(self) -> str:
        return self.path.rpartition("/")[0]

    @property
    def name(self) -> str:
        return self.path.rpartition("/")[2]


def _keep_limit(name: str, size: int, depth: int) -> int:
    ext = ext_of(name)
    if ext in IMAGE_HASH_EXTS and size <= MAX_IMAGE_BYTES:
        return MAX_IMAGE_BYTES
    if depth == 0 and archive_kind(name) and size <= NESTED_LIMIT:
        return NESTED_LIMIT
    if ext == "json" and size <= JSON_LIMIT:
        return JSON_LIMIT
    return 0


def _finish(m: Member, sink: Sink, depth: int, checkpoint) -> Member:
    m.size = sink.n
    m.hashes["blake3"] = sink.b3.digest()
    m.hashes["partial"] = sink.partial()
    data = bytes(sink.buf) if sink.buf is not None else None
    ext = ext_of(m.name)
    if data is not None and ext in IMAGE_HASH_EXTS:
        try:
            m.hashes["img_exact"] = image_hash_for(ext, data)
        except NotAnImage as e:
            m.hashes["img_none"] = str(e).encode()
        m.media = pillow_media(data)
    elif data is not None and ext == "json":
        m.sidecar = parse_sidecar(data)
    elif data is not None and depth == 0 and archive_kind(m.name):
        m.nested_kind = archive_kind(m.name)
        try:
            m.children = list(iter_members(io.BytesIO(data), m.nested_kind, depth=1, checkpoint=checkpoint))
        except Exception as e:  # noqa: BLE001 - a broken inner archive is just a file
            m.error = f"cannot open inner archive: {e}"[:300]
            m.children = []
    return m


def _pump(src, sink: Sink, checkpoint) -> None:
    while True:
        if checkpoint:
            checkpoint()
        b = src.read(CHUNK)
        if not b:
            break
        sink.write(b)


# -- readers -----------------------------------------------------------------


def _iter_zip(fileobj, depth: int, checkpoint) -> Iterator[Member]:
    with zipfile.ZipFile(fileobj) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            try:
                ts = calendar.timegm((*info.date_time, 0, 0, 0))
            except (ValueError, OverflowError):
                ts = 0
            m = Member(info.filename.lstrip("/"), info.file_size, ts * 1_000_000_000)
            sink = Sink(_keep_limit(m.name, info.file_size, depth))
            try:
                with zf.open(info) as f:
                    _pump(f, sink, checkpoint)
            except (RuntimeError, zipfile.BadZipFile, NotImplementedError, OSError, EOFError) as e:
                m.error = f"{type(e).__name__}: {e}"[:300]
                yield m
                continue
            yield _finish(m, sink, depth, checkpoint)


def _iter_tar(fileobj, depth: int, checkpoint) -> Iterator[Member]:
    with tarfile.open(fileobj=fileobj, mode="r|*") as tf:
        for ti in tf:
            if not ti.isfile():
                continue
            m = Member(ti.name.lstrip("./").lstrip("/"), ti.size, int(ti.mtime) * 1_000_000_000)
            sink = Sink(_keep_limit(m.name, ti.size, depth))
            f = tf.extractfile(ti)
            if f is None:
                continue
            try:
                _pump(f, sink, checkpoint)
            except (tarfile.TarError, OSError, EOFError) as e:
                m.error = f"{type(e).__name__}: {e}"[:300]
                yield m
                continue
            yield _finish(m, sink, depth, checkpoint)


def _iter_7z(fileobj, depth: int, checkpoint) -> Iterator[Member]:
    import py7zr
    from py7zr.io import Py7zIO, WriterFactory

    done: list[Member] = []

    with py7zr.SevenZipFile(fileobj, mode="r") as z:
        infos = {i.filename: i for i in z.list() if not i.is_directory}

        class Writer(Py7zIO):
            def __init__(self, name: str):
                info = infos.get(name)
                size = info.uncompressed if info else 0
                ts = info.creationtime.timestamp() if info and info.creationtime else 0
                self.member = Member(name.lstrip("/"), size, int(ts) * 1_000_000_000)
                self.sink = Sink(_keep_limit(self.member.name, size, depth))
                self.closed = False

            def write(self, s):
                if checkpoint:
                    checkpoint()
                return self.sink.write(bytes(s))

            def read(self, size=None):
                return b""

            def seek(self, offset, whence=0):
                return 0

            def flush(self):
                return None

            def size(self):
                return self.sink.n

            def close(self):
                if not self.closed:
                    self.closed = True
                    done.append(_finish(self.member, self.sink, depth, checkpoint))

        class Factory(WriterFactory):
            def create(self, filename: str) -> Py7zIO:
                return Writer(filename)

        z.extract(factory=Factory())
    # Members that produced no write (empty files) still exist.
    seen = {m.path for m in done}
    for name, info in infos.items():
        if name.lstrip("/") not in seen:
            ts = info.creationtime.timestamp() if info.creationtime else 0
            m = Member(name.lstrip("/"), 0, int(ts) * 1_000_000_000)
            done.append(_finish(m, Sink(), depth, checkpoint))
    yield from done


def iter_members(fileobj, kind: str, depth: int = 0, checkpoint: Callable | None = None) -> Iterator[Member]:
    if kind == "zip":
        yield from _iter_zip(fileobj, depth, checkpoint)
    elif kind == "tar":
        yield from _iter_tar(fileobj, depth, checkpoint)
    elif kind == "7z":
        yield from _iter_7z(fileobj, depth, checkpoint)
    else:
        raise ValueError(kind)


def read_archive(path: str, exp: Expected, checkpoint: Callable | None = None) -> list[Member]:
    """Index every member of the archive at ``path`` (read-only)."""
    kind = archive_kind(os.path.basename(path))
    if kind is None:
        raise ValueError(f"not an archive: {path}")
    fd = _open(path)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if st.st_size != exp.size or st.st_mtime_ns != exp.mtime_ns or not stat.S_ISREG(st.st_mode):
            raise FileChanged()
        members = list(iter_members(f, kind, 0, checkpoint))
        st = os.fstat(f.fileno())
        if st.st_size != exp.size or st.st_mtime_ns != exp.mtime_ns:
            raise FileChanged()
    return members


def read_member_bytes(path: str, member: str, limit: int = MAX_IMAGE_BYTES) -> bytes:
    """Bytes of one zip member, for thumbnails. Other formats would need a
    full sequential read, so they are not supported here."""
    if archive_kind(os.path.basename(path)) != "zip":
        raise NotAnImage("previews are only available for zip members")
    fd = _open(path)
    with os.fdopen(fd, "rb") as f, zipfile.ZipFile(f) as zf:
        info = zf.getinfo(member)
        if info.file_size > limit:
            raise NotAnImage("too large to preview")
        return zf.read(info)


# -- photo metadata from memory -------------------------------------------------


def _ratio(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _gps(coord, ref) -> float | None:
    try:
        d, m, s = (_ratio(x) for x in coord)
    except (TypeError, ValueError):
        return None
    v = d + m / 60 + s / 3600
    return -v if str(ref).upper() in ("S", "W") else v


def pillow_media(data: bytes) -> Media | None:
    """Basic metadata for a photo held in memory (ExifTool needs a file)."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            exif = im.getexif()
            sub = exif.get_ifd(0x8769)
            gps = exif.get_ifd(0x8825)
            m = Media(mime=Image.MIME.get(im.format or "", None), width=im.size[0], height=im.size[1])
            m.taken_at = normalise_date(sub.get(36867) or sub.get(36868) or exif.get(306))
            m.camera = camera_name(exif.get(271), exif.get(272))
            m.orientation = exif.get(274)
            if gps.get(2) and gps.get(4):
                lat, lon = _gps(gps[2], gps.get(1)), _gps(gps[4], gps.get(3))
                if lat is not None and lon is not None and (lat or lon):
                    m.gps_lat, m.gps_lon = lat, lon
            return m
    except Exception:  # noqa: BLE001 - metadata is best effort
        return None


# -- Google Takeout sidecars ----------------------------------------------------


def parse_sidecar(data: bytes) -> dict | None:
    """The useful part of a Google Photos JSON sidecar, or None for other JSON."""
    try:
        d = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(d, dict) or "title" not in d or not ("photoTakenTime" in d or "creationTime" in d):
        return None
    taken = None
    ts = (d.get("photoTakenTime") or d.get("creationTime") or {}).get("timestamp")
    try:
        if ts:
            taken = dt.datetime.fromtimestamp(int(ts), dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OverflowError, OSError):
        taken = None
    geo = d.get("geoData") or d.get("geoDataExif") or {}
    lat, lon = geo.get("latitude"), geo.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)) or (lat == 0 and lon == 0):
        lat = lon = None
    return {"title": str(d.get("title") or ""), "taken_at": taken, "lat": lat, "lon": lon,
            "description": (d.get("description") or "")[:1000] or None}


_SUPPLEMENTAL = "supplemental-metadata"
_COUNTER = re.compile(r"^(?P<base>.*?)(?P<n>\(\d+\))$")
_EDITED = re.compile(r"-(edited|bearbeitet|modifié|editado|modificato|bewerkt|編集済み)$", re.IGNORECASE)


def sidecar_base(json_name: str) -> tuple[str, str]:
    """``IMG_1.JPG.supplemental-metadata(1).json`` -> (``IMG_1.JPG``, ``(1)``).

    Google truncates ``.supplemental-metadata`` to fit a 51-character name,
    so any non-empty prefix of it is removed too."""
    base = json_name[:-5] if json_name.lower().endswith(".json") else json_name
    counter = ""
    m = _COUNTER.match(base)
    if m:
        base, counter = m.group("base"), m.group("n")
    head, dot, last = base.rpartition(".")
    if dot and head and last and _SUPPLEMENTAL.startswith(last.lower()) and "." in head:
        base = head
    return base, counter


def match_sidecars(names: dict[str, int], jsons: dict[str, tuple[int, dict]]) -> dict[int, int]:
    """Pair photos with sidecars inside one folder.

    ``names`` maps media file names to file ids; ``jsons`` maps JSON names to
    (file id, parsed sidecar). Returns {media file id: json file id}.
    """
    out: dict[int, int] = {}
    lower = {n.lower(): n for n in names}

    def find(candidate: str) -> str | None:
        return candidate if candidate in names else lower.get(candidate.lower())

    for jname, (jid, side) in jsons.items():
        base, counter = sidecar_base(jname)
        targets: list[str] = []
        title = side.get("title") or ""
        for cand in (base, title):
            if not cand:
                continue
            stem, dot, ext = cand.rpartition(".")
            if counter and dot:
                targets.append(f"{stem}{counter}.{ext}")
            targets.append(cand)
        hit = None
        for t in targets:
            hit = find(t)
            if hit:
                break
        if hit is None and len(base) >= 30:
            # Long names: Google cut the media name too; match on the prefix.
            pref = [n for n in names if n.startswith(base) or n.lower().startswith(base.lower())]
            if len(pref) == 1:
                hit = pref[0]
        if hit is None:
            continue
        out.setdefault(names[hit], jid)
        # The "-edited" copy shares the original's sidecar.
        stem, dot, ext = hit.rpartition(".")
        for n, fid in names.items():
            s2, d2, e2 = n.rpartition(".")
            if fid not in out and d2 and e2.lower() == ext.lower() and _EDITED.sub("", s2) == stem and s2 != stem:
                out[fid] = jid
    return out

"""Photo and video metadata with ExifTool.

ExifTool runs as one long-lived process (``-stay_open``) and reads only the
headers it needs (``-fast2``). It is given files one batch at a time, so a
bad file only costs a retry of its batch, one file at a time.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from typing import Self

log = logging.getLogger("deedoop.metadata")

TAGS = [
    "MIMEType", "DateTimeOriginal", "SubSecDateTimeOriginal", "CreateDate", "MediaCreateDate",
    "ModifyDate", "Make", "Model", "ImageWidth", "ImageHeight", "ExifImageWidth", "ExifImageHeight",
    "Duration", "GPSLatitude", "GPSLongitude", "Orientation",
]
# Preferred source for each field, best first. Keys are ExifTool "Group:Tag".
DATE_KEYS = ["EXIF:DateTimeOriginal", "XMP:DateTimeOriginal", "Composite:SubSecDateTimeOriginal",
             "EXIF:CreateDate", "XMP:CreateDate", "QuickTime:CreateDate", "QuickTime:MediaCreateDate",
             "PNG:CreateDate", "PNG:ModifyDate"]
WIDTH_KEYS = ["File:ImageWidth", "EXIF:ExifImageWidth", "PNG:ImageWidth", "QuickTime:ImageWidth",
              "RIFF:ImageWidth", "EXIF:ImageWidth"]
HEIGHT_KEYS = ["File:ImageHeight", "EXIF:ExifImageHeight", "PNG:ImageHeight", "QuickTime:ImageHeight",
               "RIFF:ImageHeight", "EXIF:ImageHeight"]

METADATA_EXTS = {
    # images
    "jpg", "jpeg", "jpe", "jfif", "png", "heic", "heif", "webp", "avif", "tif", "tiff", "gif", "bmp",
    "cr2", "cr3", "nef", "arw", "dng", "orf", "rw2", "raf", "srw", "pef",
    # video
    "mp4", "m4v", "mov", "avi", "mkv", "wmv", "mpg", "mpeg", "mts", "m2ts", "3gp", "webm",
}

BATCH = 100
_DATE = re.compile(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


@dataclass
class Media:
    mime: str | None = None
    taken_at: str | None = None
    camera: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None
    orientation: int | None = None
    error: str | None = None


UNSAFE_NAME = "name not readable by ExifTool (it contains a line break or control character)"


def safe_for_exiftool(path: str) -> bool:
    """A2: ExifTool runs with ``-stay_open`` and reads one argument per line, so
    a file name with a line break would add options (``-all=`` strips the tags
    of every file in the batch, ``-w`` writes files). Only absolute paths
    without control characters are passed; an absolute path never starts with
    ``-``, so it cannot be read as an option."""
    return path.startswith("/") and not any(ord(ch) < 32 or ord(ch) == 127 for ch in path)


def exiftool_available() -> bool:
    return shutil.which("exiftool") is not None


def _first(d: dict, keys: list[str]):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", 0, "0000:00:00 00:00:00"):
            return v
    return None


def _by_tag(d: dict, tag: str):
    for k, v in d.items():
        if k.split(":")[-1] == tag and v not in (None, ""):
            return v
    return None


def normalise_date(v) -> str | None:
    if v is None:
        return None
    m = _DATE.match(str(v).strip())
    if not m or m.group(1) in ("0000", "1904", "1970") and m.group(2) in ("00", "01"):
        # 1904-01-01 and 1970-01-01 are empty QuickTime / Unix epochs.
        return None
    y, mo, d, h, mi, s = m.groups()
    return f"{y}-{mo}-{d} {h}:{mi}:{s}"


def camera_name(make, model) -> str | None:
    """'Canon' + 'Canon EOS 80D' -> 'Canon EOS 80D'; 'Apple' + 'iPhone 12' -> 'Apple iPhone 12'."""
    make = str(make or "").strip().strip("\x00")
    model = str(model or "").strip().strip("\x00")
    if model and make and model.lower().startswith(make.split()[0].lower()):
        make = ""
    return " ".join(x for x in (make, model) if x) or None


def parse(d: dict) -> Media:
    m = Media()
    m.mime = _by_tag(d, "MIMEType")
    m.taken_at = normalise_date(_first(d, DATE_KEYS))
    m.camera = camera_name(_by_tag(d, "Make"), _by_tag(d, "Model"))
    w, h = _first(d, WIDTH_KEYS), _first(d, HEIGHT_KEYS)
    m.width = int(w) if isinstance(w, (int, float)) else None
    m.height = int(h) if isinstance(h, (int, float)) else None
    dur = _by_tag(d, "Duration")
    m.duration = float(dur) if isinstance(dur, (int, float)) and dur > 0 else None
    # Composite GPS values are signed (south and west negative); raw EXIF ones are not.
    lat = _first(d, ["Composite:GPSLatitude", "XMP:GPSLatitude"])
    lon = _first(d, ["Composite:GPSLongitude", "XMP:GPSLongitude"])
    if lat is None or lon is None:
        lat, lon = _by_tag(d, "GPSLatitude"), _by_tag(d, "GPSLongitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat or lon):
        m.gps_lat, m.gps_lon = float(lat), float(lon)
    o = _by_tag(d, "Orientation")
    m.orientation = int(o) if isinstance(o, (int, float)) else None
    return m


class Reader:
    """Context manager around one ExifTool process."""

    def __init__(self) -> None:
        self._et = None

    def __enter__(self) -> Self:
        import exiftool

        self._et = exiftool.ExifToolHelper(common_args=["-G", "-n", "-fast2"], check_execute=False)
        self._et.run()
        return self

    def __exit__(self, *exc) -> None:
        if self._et is not None:
            try:
                self._et.terminate()
            except Exception:
                log.debug("exiftool did not stop cleanly", exc_info=True)

    def _run(self, paths: list[str]) -> list[dict]:
        # SC3: every argument is a fixed option or a path checked by safe_for_exiftool.
        if not all(safe_for_exiftool(p) for p in paths):
            raise ValueError("unsafe path for ExifTool")  # read() filters them first; never reached
        args = [f"-{t}" for t in TAGS] + ["-j", "-charset", "filename=utf8", *paths]
        out = self._et.execute(*args)
        return json.loads(out) if out else []

    def read(self, paths: list[str]) -> dict[str, Media]:
        """Map each path to its :class:`Media`. Unreadable files get ``error``."""
        result: dict[str, Media] = {p: Media(error=UNSAFE_NAME) for p in paths if not safe_for_exiftool(p)}
        paths = [p for p in paths if p not in result]
        if not paths:
            return result
        try:
            rows = self._run(paths)
        except Exception as e:  # noqa: BLE001 - fall back to one file at a time
            if len(paths) == 1:
                return {paths[0]: Media(error=f"exiftool: {e}"[:300])}
            for p in paths:
                result.update(self.read([p]))
            return result
        for row in rows:
            src = row.get("SourceFile")
            if src:
                result[src] = parse(row)
        for p in paths:
            result.setdefault(p, Media(error="no metadata returned"))
        return result

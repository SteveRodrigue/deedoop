"""The scanner registry: optional analysis that an admin turns on or off.

deedup always finds exact copies by content hash, for every file type. On
top of that, each scanner below adds its own understanding of one family of
files. Turning a scanner off in Setup skips its scan phases, drops its
duplicate groups and its marks at the next regroup, and leaves everything
else alone. With every scanner off, deedup compares raw file contents only.

A scanner is a plugin: a row in ``REGISTRY`` plus hook functions, named as
``"module:function"`` strings and imported only when the scanner runs, so a
scanner whose tools are missing costs nothing. The hooks each take the scan
(:class:`deedup.engine.Scan`):

* ``index`` runs after the walk and before the content hashes (cheap reads
  and anything that also yields the exact hashes, such as photos).
* ``after_hashes`` runs once the exact hashes are known (work that is only
  worth doing for files that are not plain exact copies).
* ``group`` rebuilds the scanner's duplicate groups and returns a summary.
* ``drop`` removes them when the scanner is off.

Hooks shared by two scanners (video and audio share the "Same recording,
other tags" method) run once per scan.
"""

from __future__ import annotations

import importlib
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field

from .categories import exts_for


@dataclass(frozen=True)
class Scanner:
    key: str
    label: str
    finds: str                      # what it adds, for the Setup screen
    cost: str                       # what it reads
    methods: tuple[str, ...]        # duplicate methods (group kinds) it produces
    tools: tuple[str, ...] = ()     # programs it needs in the container
    exts: frozenset[str] = field(default_factory=frozenset)
    index: str = ""
    after_hashes: str = ""
    group: str = ""
    drop: str = ""

    def available(self) -> bool:
        return all(shutil.which(t) for t in self.tools)

    def missing(self) -> list[str]:
        return [t for t in self.tools if not shutil.which(t)]


REGISTRY: dict[str, Scanner] = {s.key: s for s in [
    Scanner(
        key="photo", label="Photos",
        finds="Same photo with different metadata, and similar photos (recompressed or resized) for you to review.",
        cost="Reads every photo once in full.",
        methods=("image", "similar"), exts=frozenset(exts_for("image")),
        index="deedup.engine:photo_index", group="deedup.engine:photo_group", drop="deedup.engine:photo_drop",
    ),
    Scanner(
        key="video", label="Videos",
        finds="Same video with other tags, dates or container (for example MKV remuxed to MP4); codec, "
              "resolution and duration in Browse.",
        cost="Reads video headers; reads in full only videos that share a length and stream layout with another.",
        methods=("media",), tools=("ffprobe", "ffmpeg"), exts=frozenset(exts_for("video")),
        index="deedup.media_index:index", after_hashes="deedup.media_index:after_hashes",
        group="deedup.media_index:group", drop="deedup.media_index:drop",
    ),
    Scanner(
        key="audio", label="Music and audio",
        finds="Same song with other tags or cover art; artist, album and codec in Browse, with a music view.",
        cost="Reads audio headers; reads in full only songs that share a length and stream layout with another.",
        methods=("media",), tools=("ffprobe", "ffmpeg"), exts=frozenset(exts_for("audio")),
        index="deedup.media_index:index", after_hashes="deedup.media_index:after_hashes",
        group="deedup.media_index:group", drop="deedup.media_index:drop",
    ),
    Scanner(
        key="archive", label="Archives and Google Takeout",
        finds="Opens zip, tar and 7z files in your Takeout folders and compares their contents with your disks.",
        cost="Reads each archive once in full; never extracts anything.",
        methods=(), exts=frozenset(exts_for("archive")),
        index="deedup.engine:archive_index", group="deedup.engine:archive_group",
    ),
]}

METHOD_LABELS = {
    "exact": "Exact copies",
    "image": "Same photo, different metadata",
    "media": "Same recording, other tags",
    "similar": "Similar photos",
}


def enabled(settings: dict, key: str) -> bool:
    """Turned on in Setup (every scanner is on unless switched off)."""
    return bool((settings.get("scanners") or {}).get(key, True))


def active(settings: dict, key: str) -> bool:
    """Turned on and its tools are installed."""
    s = REGISTRY[key]
    return enabled(settings, key) and s.available()


def active_keys(settings: dict) -> list[str]:
    return [k for k in REGISTRY if active(settings, k)]


def raw_only(settings: dict) -> bool:
    """Every scanner is off: exact content matching only."""
    return not active_keys(settings)


def methods(settings: dict) -> list[str]:
    """Duplicate methods that are on, in display order."""
    on = {"exact"}
    if active(settings, "photo"):
        if settings.get("image_matching"):
            on.add("image")
        if settings.get("similar_matching"):
            on.add("similar")
    if media_kinds(settings):
        on.add("media")
    return [m for m in METHOD_LABELS if m in on]


def media_kinds(settings: dict) -> list[str]:
    return [k for k in ("video", "audio") if active(settings, k)]


def media_exts(settings: dict) -> list[str]:
    return sorted(set().union(*(REGISTRY[k].exts for k in media_kinds(settings))))


def exiftool_exts(settings: dict) -> set[str]:
    """Files whose date taken, camera and GPS ExifTool reads: photos and videos,
    each only while its scanner is on."""
    from .metadata import METADATA_EXTS

    want: set[str] = set()
    for k in ("photo", "video"):
        if active(settings, k):
            want |= REGISTRY[k].exts
    return METADATA_EXTS & want


def hook(path: str) -> Callable:
    module, _, name = path.partition(":")
    return getattr(importlib.import_module(module), name)


def status(settings: dict) -> list[dict]:
    """One row per scanner for the Setup screen."""
    return [{"key": s.key, "label": s.label, "finds": s.finds, "cost": s.cost, "enabled": enabled(settings, s.key),
             "available": s.available(), "missing": s.missing()} for s in REGISTRY.values()]

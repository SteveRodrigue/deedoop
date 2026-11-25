"""Broad file-type categories, used for filters and the wasted-space breakdown."""

from __future__ import annotations

from collections import Counter

_CATS = {
    "image": "jpg jpeg jpe png gif bmp tif tiff webp heic heif avif raw cr2 cr3 nef arw dng orf rw2 raf srw pef psd svg ico jxl",
    "video": "mp4 m4v mov avi mkv wmv flv webm mpg mpeg mts m2ts ts 3gp vob mxf",
    "audio": "mp3 flac wav aac m4a ogg opus wma aiff aif alac ape mka",
    "document": "pdf doc docx xls xlsx ppt pptx odt ods odp rtf txt md csv epub mobi pages numbers key",
    "archive": "zip tar gz tgz bz2 xz 7z rar zst lz4 iso img dmg cab",
    "code": "py js ts java c h cpp go rs rb php html css json xml yaml yml sh",
}
EXT_TO_CAT = {e: cat for cat, exts in _CATS.items() for e in exts.split()}
CATEGORIES = list(_CATS) + ["other"]


def category(ext: str) -> str:
    return EXT_TO_CAT.get(ext.lower(), "other")


def group_category(exts: list[str]) -> str:
    cats = Counter(category(e) for e in exts)
    known = [(n, c) for c, n in cats.items() if c != "other"]
    return max(known)[1] if known else "other"


def exts_for(cat: str) -> list[str]:
    return sorted(_CATS.get(cat, "").split())

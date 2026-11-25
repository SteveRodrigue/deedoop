"""Image-data hashing that ignores metadata, plus thumbnails.

Two photos are "the same photo" when their image data is identical even if
their EXIF, XMP, IPTC, embedded thumbnails or comments differ (for example
after a date fix, a GPS tag or a rename by a photo manager).

* JPEG: the compressed image is hashed as it is stored. Every APPn segment
  (EXIF, XMP, ICC, Photoshop/IPTC, embedded thumbnails) and every comment is
  skipped; quantisation and Huffman tables, frame and scan headers and the
  entropy-coded data are kept. No decoding is needed, so this is fast and
  exact.
* PNG, HEIC/HEIF, WebP, AVIF, TIFF, BMP: the file is decoded and the pixels
  are hashed together with the pixel format and size.

The two kinds use different prefixes, so a JPEG never matches a PNG.
Re-compressed or resized copies do not match here; that is milestone 4.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import blake3
import xxhash

from .hashing import EDGE, SMALL, Expected, FileChanged, _check, _drop_cache, _open

try:
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 250_000_000
except ImportError:  # pragma: no cover - Pillow is a hard dependency in the image
    Image = None

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover
    pillow_heif = None

JPEG_EXTS = {"jpg", "jpeg", "jpe", "jfif"}
PIXEL_EXTS = {"png", "heic", "heif", "webp", "avif", "tif", "tiff", "bmp"}
IMAGE_HASH_EXTS = JPEG_EXTS | PIXEL_EXTS
THUMB_EXTS = IMAGE_HASH_EXTS | {"gif"}
# Images larger than this are not read whole into memory for the image hash.
MAX_IMAGE_BYTES = 400 * 1024 * 1024


class NotAnImage(ValueError):
    """The file does not decode as the image its extension promises."""


def jpeg_data_hash(data: bytes) -> bytes:
    """Hash a JPEG's image data, skipping APPn and COM segments."""
    n = len(data)
    if n < 4 or data[0] != 0xFF or data[1] != 0xD8:
        raise NotAnImage("not a JPEG (no SOI marker)")
    h = blake3.blake3(b"deedup-jpeg-v1\0")
    i = 2
    saw_scan = False
    while i < n:
        if data[i] != 0xFF:
            raise NotAnImage(f"bad JPEG marker at byte {i}")
        while i < n and data[i] == 0xFF:  # fill bytes
            i += 1
        if i >= n:
            break
        marker = data[i]
        i += 1
        if marker == 0xD9:  # EOI: anything after it (trailers, MPF images) is ignored
            break
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:  # standalone markers
            h.update(bytes((0xFF, marker)))
            continue
        if i + 2 > n:
            raise NotAnImage("truncated JPEG segment")
        seglen = int.from_bytes(data[i:i + 2], "big")
        if seglen < 2 or i + seglen > n:
            raise NotAnImage("bad JPEG segment length")
        if not (0xE0 <= marker <= 0xEF or marker == 0xFE):
            h.update(bytes((0xFF, marker)))
            h.update(data[i:i + seglen])
        i += seglen
        if marker == 0xDA:  # SOS: entropy-coded data follows until the next real marker
            saw_scan = True
            j = i
            while True:
                j = data.find(b"\xff", j)
                if j == -1 or j + 1 >= n:
                    j = n
                    break
                nxt = data[j + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:  # stuffed byte or restart marker
                    j += 2
                    continue
                break
            h.update(data[i:j])
            i = j
    if not saw_scan:
        raise NotAnImage("JPEG without image data")
    return h.digest()


def pixel_hash(data: bytes) -> bytes:
    if Image is None:  # pragma: no cover
        raise NotAnImage("Pillow is not installed")
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            h = blake3.blake3(b"deedup-pixels-v1\0")
            h.update(f"{im.mode}:{im.size[0]}x{im.size[1]}\0".encode())
            if im.mode == "P" and im.palette is not None:
                h.update(bytes(im.getpalette() or []))
            h.update(im.tobytes())
            return h.digest()
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as e:
        raise NotAnImage(f"cannot decode: {e}") from e


def image_hash_for(ext: str, data: bytes) -> bytes:
    if ext in JPEG_EXTS:
        return jpeg_data_hash(data)
    return pixel_hash(data)


def read_and_hash_image(path: str, ext: str, exp: Expected, checkpoint=None) -> dict[str, bytes]:
    """Read an image once and return its partial, BLAKE3 and image-data hashes.

    Image-data hashing needs every byte anyway, so the exact-match hashes come
    from the same read for free. If the image does not decode, the exact
    hashes are still returned and ``img_error`` holds the reason.
    """
    if exp.size > MAX_IMAGE_BYTES:
        raise NotAnImage("too large to hash as an image")
    fd = _open(path)
    try:
        _check(fd, exp)
        chunks = []
        got = 0
        while True:
            if checkpoint:
                checkpoint()
            b = os.read(fd, 4 * 1024 * 1024)
            if not b:
                break
            chunks.append(b)
            got += len(b)
        if got != exp.size:
            raise FileChanged()
        _check(fd, exp)
        _drop_cache(fd)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    out: dict[str, bytes] = {"blake3": blake3.blake3(data).digest(), "partial": partial_from_bytes(data)}
    try:
        out["img_exact"] = image_hash_for(ext, data)
    except NotAnImage as e:
        out["img_error"] = str(e).encode()
    return out


def partial_from_bytes(data: bytes) -> bytes:
    """Same value as :func:`deedup.hashing.partial_hash` for these bytes."""
    h = xxhash.xxh3_128()
    h.update(len(data).to_bytes(8, "little"))
    if len(data) <= SMALL:
        h.update(data)
    else:
        h.update(data[:EDGE])
        h.update(data[-EDGE:])
    return h.digest()


# -- thumbnails ---------------------------------------------------------------


def make_thumbnail(src: str, dest: Path, size: int = 320) -> Path:
    """Write a JPEG thumbnail of ``src`` to ``dest`` (in the config folder)."""
    if Image is None:  # pragma: no cover
        raise NotAnImage("Pillow is not installed")
    try:
        with Image.open(src) as im:
            if im.format == "JPEG":
                im.draft("RGB", (size * 2, size * 2))
            im = ImageOps.exif_transpose(im)
            im.thumbnail((size, size))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".tmp")
            im.save(tmp, "JPEG", quality=80, optimize=True)
            tmp.replace(dest)
            return dest
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as e:
        raise NotAnImage(str(e)) from e

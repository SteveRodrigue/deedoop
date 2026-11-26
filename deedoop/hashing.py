"""Read-only file hashing.

Two tiers, borrowed from File Hunter and fclones:

* ``partial``: xxh3-128 over the file size, the first 64 KiB and the last
  64 KiB. Cheap, and rules out most same-size files.
* ``blake3``: BLAKE3 of the whole content. Strong enough to trust before a
  delete in a later milestone.

Files of 128 KiB or less are read whole by the partial pass, so their
BLAKE3 is computed in the same read.

Files are opened read-only (``O_RDONLY``, with ``O_NOATIME`` when allowed),
and the page cache is released after big reads so a multi-terabyte scan
does not push everything else out of RAM.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import blake3
import xxhash

EDGE = 64 * 1024
SMALL = 2 * EDGE
CHUNK = 4 * 1024 * 1024


class FileChanged(Exception):
    """The file's size or mtime changed while it was being read."""


@dataclass
class Expected:
    size: int
    mtime_ns: int


def _open(path: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    noatime = getattr(os, "O_NOATIME", 0)
    if noatime:
        try:
            return os.open(path, flags | noatime)
        except PermissionError:
            pass  # O_NOATIME needs file ownership; fall back to a plain read
    return os.open(path, flags)


def _check(fd: int, exp: Expected) -> None:
    st = os.fstat(fd)
    if st.st_size != exp.size or st.st_mtime_ns != exp.mtime_ns:
        raise FileChanged()


def _drop_cache(fd: int) -> None:
    if hasattr(os, "posix_fadvise"):
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass


def _read_exact(fd: int, n: int, offset: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        b = os.pread(fd, n - len(out), offset + len(out))
        if not b:
            break
        out += b
    return bytes(out)


def partial_hash(path: str, exp: Expected) -> tuple[bytes, bytes | None]:
    """Return ``(partial, blake3_or_None)``. ``blake3`` is set for small files."""
    fd = _open(path)
    try:
        _check(fd, exp)
        h = xxhash.xxh3_128()
        h.update(exp.size.to_bytes(8, "little"))
        if exp.size <= SMALL:
            data = _read_exact(fd, exp.size, 0)
            if len(data) != exp.size:
                raise FileChanged()
            h.update(data)
            full = blake3.blake3(data).digest()
        else:
            head = _read_exact(fd, EDGE, 0)
            tail = _read_exact(fd, EDGE, exp.size - EDGE)
            if len(head) != EDGE or len(tail) != EDGE:
                raise FileChanged()
            h.update(head)
            h.update(tail)
            full = None
        _check(fd, exp)
        return h.digest(), full
    finally:
        os.close(fd)


def full_hash(path: str, exp: Expected, threads: int = 1, on_bytes=None, checkpoint=None) -> bytes:
    """BLAKE3 of the whole file. ``on_bytes(n)`` reports progress and
    ``checkpoint()`` is called between chunks so a scan can pause or stop."""
    fd = _open(path)
    try:
        _check(fd, exp)
        if hasattr(os, "posix_fadvise"):
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
            except OSError:
                pass
        h = blake3.blake3(max_threads=max(1, threads)) if threads > 1 else blake3.blake3()
        read = 0
        while True:
            if checkpoint:
                checkpoint()
            b = os.read(fd, CHUNK)
            if not b:
                break
            h.update(b)
            read += len(b)
            if on_bytes:
                on_bytes(len(b))
        if read != exp.size:
            raise FileChanged()
        _check(fd, exp)
        if exp.size > SMALL:
            _drop_cache(fd)
        return h.digest()
    finally:
        os.close(fd)

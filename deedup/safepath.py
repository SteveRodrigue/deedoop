"""The only place that renames, links or deletes files on /mnt (rule SC2).

Paths come from the index, so they were real folders when the scan saw them
(scans never follow symbolic links). Between the scan and a change, someone
who can write to a share could swap a folder for a symlink that points
elsewhere (audit finding A9). Every function here therefore opens each folder
of the path from ``/`` down with ``O_NOFOLLOW`` and acts through that folder's
file descriptor: a symlink anywhere in the path makes the call fail instead
of being followed, and the check cannot go stale before the action.

All functions take absolute, normalised paths and raise :class:`UnsafePath`
for anything else.
"""

from __future__ import annotations

import errno
import os
from contextlib import contextmanager

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


class UnsafePath(OSError):
    """The path is not absolute and normalised, or goes through a symbolic link."""


class Exists(FileExistsError):
    """Something is already at the destination; nothing was overwritten."""


def check(path: str) -> list[str]:
    """Split an absolute, normalised path into its parts."""
    if not path.startswith("/") or "\0" in path:
        raise UnsafePath(f"not an absolute path: {path!r}")
    parts = path.split("/")[1:]
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise UnsafePath(f"not a normalised path: {path!r}")
    return parts


def inside(path: str, base: str) -> bool:
    """``path`` is ``base`` or below it, comparing the real locations."""
    real = os.path.realpath(path)
    root = os.path.realpath(base).rstrip("/")
    return real == root or real.startswith(root + "/")


@contextmanager
def parent(path: str):
    """Yield ``(dir_fd, name)`` for the folder holding ``path``, opened without
    following any symbolic link on the way."""
    parts = check(path)
    fd = os.open("/", _DIR_FLAGS)
    try:
        for comp in parts[:-1]:
            try:
                nfd = os.open(comp, _DIR_FLAGS, dir_fd=fd)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise UnsafePath(f"a folder in the path is a symbolic link or not a folder: {path}") from None
                raise
            os.close(fd)
            fd = nfd
        yield fd, parts[-1]
    finally:
        os.close(fd)


def rename_noclobber(src: str, dst: str) -> None:
    """Move ``src`` to ``dst`` without ever overwriting ``dst``."""
    with parent(src) as (sfd, sname), parent(dst) as (dfd, dname):
        try:
            os.stat(dname, dir_fd=dfd, follow_symlinks=False)
            raise Exists(dst)
        except FileNotFoundError:
            pass
        try:
            # link() fails if dst appeared meanwhile, so it never overwrites.
            os.link(sname, dname, src_dir_fd=sfd, dst_dir_fd=dfd, follow_symlinks=False)
        except FileExistsError:
            raise Exists(dst) from None
        except OSError:
            os.rename(sname, dname, src_dir_fd=sfd, dst_dir_fd=dfd)  # filesystems without hardlinks
            return
        os.unlink(sname, dir_fd=sfd)


def link(target: str, new: str) -> None:
    """Make ``new`` a hardlink to ``target``."""
    with parent(target) as (tfd, tname), parent(new) as (nfd, nname):
        os.link(tname, nname, src_dir_fd=tfd, dst_dir_fd=nfd, follow_symlinks=False)


def unlink(path: str, missing_ok: bool = False) -> None:
    with parent(path) as (fd, name):
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            if not missing_ok:
                raise


def mkdir(path: str) -> None:
    with parent(path) as (fd, name):
        os.mkdir(name, dir_fd=fd)


def rmdir(path: str) -> None:
    with parent(path) as (fd, name):
        os.rmdir(name, dir_fd=fd)


def open_new(path: str, mode: int = 0o644):
    """Open a new file for writing; fails if anything is already there."""
    with parent(path) as (fd, name):
        nfd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                      mode, dir_fd=fd)
    return os.fdopen(nfd, "wb")

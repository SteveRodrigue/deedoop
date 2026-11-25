"""Find the disks, pools and shares under ``/mnt`` and turn the user's
selection into scan roots.

On Unraid, every share in ``/mnt/user`` is a union of same-named folders on
the array disks (``/mnt/disk1``, ``/mnt/disk2``, ...) and pools
(``/mnt/cache``, ...). deedup scans the physical folders, never the
``/mnt/user`` FUSE view, so each disk is read on its own and each file knows
which disk it lives on.

On a machine without ``/mnt/user`` every top-level folder under the mount
point is treated as a disk, and its sub-folders as shares.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .db import Tx, get_settings

# Folders under /mnt that are not physical disks or pools.
NOT_DISKS = {"user", "user0", "addons", "rootshare", "remotes", "disks", "rootfs"}
# Folders under /mnt that hold one mount per device.
DEVICE_PARENTS = {"disks": "unassigned", "remotes": "remote"}


@dataclass
class Disk:
    name: str
    path: Path
    kind: str  # array, pool, unassigned, remote, folder
    total: int = 0
    used: int = 0
    free: int = 0


@dataclass
class Share:
    name: str
    user_path: Path
    disks: list[str] = field(default_factory=list)


def natural_key(s: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def is_unraid(mnt: Path) -> bool:
    return (mnt / "user").is_dir()


def _statvfs(disk: Disk) -> Disk:
    try:
        st = os.statvfs(disk.path)
        disk.total = st.f_blocks * st.f_frsize
        disk.free = st.f_bavail * st.f_frsize
        disk.used = (st.f_blocks - st.f_bfree) * st.f_frsize
    except OSError:
        pass
    return disk


def _subdirs(path: Path) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as it:
            return [e for e in it if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")]
    except OSError:
        return []


def list_disks(mnt: Path) -> list[Disk]:
    disks: list[Disk] = []
    unraid = is_unraid(mnt)
    for e in _subdirs(mnt):
        if unraid and e.name in DEVICE_PARENTS:
            for d in _subdirs(Path(e.path)):
                disks.append(Disk(f"{e.name}/{d.name}", Path(d.path), DEVICE_PARENTS[e.name]))
            continue
        if unraid and e.name in NOT_DISKS:
            continue
        if unraid:
            kind = "array" if re.fullmatch(r"disk\d+", e.name) else "pool"
        else:
            kind = "folder"
        disks.append(Disk(e.name, Path(e.path), kind))
    order = {"array": 0, "pool": 1, "folder": 1, "unassigned": 2, "remote": 3}
    disks.sort(key=lambda d: (order[d.kind], natural_key(d.name)))
    return [_statvfs(d) for d in disks]


def list_shares(mnt: Path, disks: list[Disk] | None = None) -> list[Share]:
    disks = disks if disks is not None else list_disks(mnt)
    # Unassigned devices and remotes are not part of Unraid shares.
    share_disks = [d for d in disks if d.kind in ("array", "pool", "folder")]
    if is_unraid(mnt):
        names = sorted({e.name for e in _subdirs(mnt / "user")}, key=natural_key)
    else:
        names = sorted({e.name for d in share_disks for e in _subdirs(d.path)}, key=natural_key)
    shares = []
    for name in names:
        on = [d.name for d in share_disks if (d.path / name).is_dir()]
        user_path = mnt / "user" / name if is_unraid(mnt) else mnt / name
        shares.append(Share(name, user_path, on))
    return shares


def disk_for_path(path: Path, disks: list[Disk]) -> str:
    best = None
    for d in disks:
        inside = path == d.path or d.path in path.parents
        if inside and (best is None or len(str(d.path)) > len(str(best.path))):
            best = d
    return best.name if best else "custom"


def _overlaps(a: str, b: str) -> bool:
    a, b = a.rstrip("/") + "/", b.rstrip("/") + "/"
    return a.startswith(b) or b.startswith(a)


class RootError(ValueError):
    pass


def sync_share_roots(conn: sqlite3.Connection, mnt: Path) -> None:
    """Create or enable a root for each selected share on each disk that holds
    it, and disable share roots that are no longer selected. Run before every
    scan, so a share that spreads onto a new disk is picked up."""
    selected = set(get_settings(conn)["shares"])
    disks = list_disks(mnt)
    by_name = {d.name: d for d in disks}
    wanted: dict[str, tuple[str, str, str]] = {}
    for share in list_shares(mnt, disks):
        if share.name not in selected:
            continue
        for disk_name in share.disks:
            p = str(by_name[disk_name].path / share.name)
            wanted[p] = (disk_name, share.name, str(share.user_path))
    with Tx(conn):
        existing = {r["path"]: r for r in conn.execute("SELECT * FROM roots WHERE source = 'share'")}
        for p, row in existing.items():
            en = 1 if p in wanted else 0
            if row["enabled"] != en:
                conn.execute("UPDATE roots SET enabled = ? WHERE id = ?", (en, row["id"]))
        for p, (disk, share, user_path) in wanted.items():
            if p not in existing:
                conn.execute(
                    "INSERT INTO roots(path, disk, share, user_path, source, created_at) "
                    "VALUES (?, ?, ?, ?, 'share', ?)",
                    (p, disk, share, user_path, time.time()),
                )


def add_custom_root(conn: sqlite3.Connection, mnt: Path, path_str: str) -> int:
    """Add a folder that is not an Unraid share (for example a folder on an
    unassigned device). It must be inside the read-only mount and must not
    overlap a root already in the index."""
    path = Path(os.path.normpath(path_str.strip()))
    if not path.is_absolute():
        raise RootError("Use an absolute path, for example /mnt/disks/usb/Backups.")
    if path != mnt and mnt not in path.parents:
        raise RootError(f"The folder must be inside {mnt}.")
    if is_unraid(mnt) and (path.parts[: len(mnt.parts) + 1] in {(*mnt.parts, "user"), (*mnt.parts, "user0")}):
        raise RootError("Pick shares from the list instead of /mnt/user paths; deedup reads the disks directly.")
    if not path.is_dir():
        raise RootError(f"{path} is not a folder.")
    for r in conn.execute("SELECT path FROM roots WHERE enabled = 1"):
        if _overlaps(str(path), r["path"]):
            raise RootError(f"{path} overlaps {r['path']}, which is already selected.")
    disk = disk_for_path(path, list_disks(mnt))
    cur = conn.execute(
        "INSERT INTO roots(path, disk, share, user_path, source, created_at) VALUES (?, ?, NULL, ?, 'custom', ?) "
        "ON CONFLICT(path) DO UPDATE SET enabled = 1",
        (str(path), disk, str(path), time.time()),
    )
    return int(cur.lastrowid)


def remove_root(conn: sqlite3.Connection, root_id: int) -> None:
    """Forget a root and everything indexed under it. Touches only the index."""
    with Tx(conn):
        conn.execute("DELETE FROM roots WHERE id = ?", (root_id,))

"""Directory walk: keep the ``files`` table in step with the disks.

The walk only calls ``stat``; it never opens a file. A file whose size,
mtime and inode are unchanged keeps its stored hashes, so a rescan of an
unchanged disk is a directory walk and nothing else. Files that disappeared
are marked ``gone`` once their root was walked completely.
"""

from __future__ import annotations

import fnmatch
import os
import sqlite3
import stat
import time
from dataclasses import dataclass

from .control import Control, Status
from .db import Tx

BATCH_SECONDS = 2.0
BATCH_ROWS = 5000


@dataclass
class WalkResult:
    files: int = 0
    bytes: int = 0
    dirs: int = 0
    new: int = 0
    changed: int = 0
    unreadable_dirs: int = 0
    complete: bool = False


QUARANTINE_DIR = ".deedoop-quarantine"


class Excluder:
    def __init__(self, patterns: list[str]):
        self.patterns = [p.strip().strip("/") for p in patterns if p.strip()]

    def __call__(self, name: str, rel: str) -> bool:
        if name == QUARANTINE_DIR:
            return True  # deedoop's own quarantine is never indexed
        for p in self.patterns:
            if fnmatch.fnmatchcase(name, p) or fnmatch.fnmatchcase(rel, p):
                return True
        return False


def ext_of(name: str) -> str:
    base, dot, ext = name.rpartition(".")
    return ext.lower() if dot and base and len(ext) <= 12 else ""


class _Writer:
    """Buffers per-directory changes and commits them in batches."""

    def __init__(self, conn: sqlite3.Connection, root_id: int, run_id: int):
        self.conn, self.root_id, self.run_id = conn, root_id, run_id
        self.inserts: list[tuple] = []
        self.updates: list[tuple] = []
        self.touches: list[tuple] = []
        self.touch_dirs: list[str] = []
        self.last = time.monotonic()

    def pending(self) -> int:
        return len(self.inserts) + len(self.updates) + len(self.touches)

    def maybe_flush(self) -> None:
        if self.pending() >= BATCH_ROWS or time.monotonic() - self.last > BATCH_SECONDS:
            self.flush()

    def flush(self) -> None:
        if not (self.pending() or self.touch_dirs):
            return
        c = self.conn
        with Tx(c):
            if self.updates:
                ids = [(u[-1],) for u in self.updates]
                c.executemany("DELETE FROM hashes WHERE file_id = ?", ids)
                c.executemany(
                    "UPDATE files SET size=?, mtime_ns=?, dev=?, inode=?, nlink=?, ext=?, "
                    "state='present', last_seen_run=?, error=NULL WHERE id=?",
                    self.updates,
                )
            if self.inserts:
                c.executemany(
                    "INSERT INTO files(root_id, dir, name, size, mtime_ns, dev, inode, nlink, ext, last_seen_run) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    self.inserts,
                )
            if self.touches:
                c.executemany(
                    "UPDATE files SET last_seen_run=?, state='present', nlink=? WHERE id=?", self.touches
                )
            for d in self.touch_dirs:
                # An unreadable folder: keep what we knew about it rather than
                # reporting its files as gone.
                if d:
                    c.execute(
                        "UPDATE files SET last_seen_run=? WHERE root_id=? AND state='present' AND container_id=0 "
                        "AND (dir=? OR substr(dir, 1, ?)=?)",
                        (self.run_id, self.root_id, d, len(d) + 1, d + "/"),
                    )
                else:
                    c.execute(
                        "UPDATE files SET last_seen_run=? WHERE root_id=? AND state='present' AND container_id=0",
                        (self.run_id, self.root_id),
                    )
        self.inserts, self.updates, self.touches, self.touch_dirs = [], [], [], []
        self.last = time.monotonic()


def walk_root(
    conn: sqlite3.Connection,
    root: sqlite3.Row,
    run_id: int,
    excluder: Excluder,
    control: Control,
    status: Status,
) -> WalkResult:
    disk = root["disk"]
    res = WalkResult()
    w = _Writer(conn, root["id"], run_id)
    stack = [""]
    base = root["path"]
    status.disk(disk, current=base)
    reported = (0, 0, 0, 0, 0)
    try:
        while stack:
            control.checkpoint()
            rel_dir = stack.pop()
            abs_dir = os.path.join(base, rel_dir) if rel_dir else base
            try:
                with os.scandir(abs_dir) as it:
                    entries = list(it)
            except OSError as e:
                res.unreadable_dirs += 1
                status.log("warn", f"{abs_dir}: cannot open folder ({e.strerror or type(e).__name__})")
                w.touch_dirs.append(rel_dir)
                continue
            known = {
                r["name"]: r
                for r in conn.execute(
                    "SELECT id, name, size, mtime_ns, inode, dev FROM files "
                    "WHERE root_id=? AND dir=? AND container_id=0",
                    (root["id"], rel_dir),
                )
            }
            subdirs = []
            for e in entries:
                rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
                if excluder(e.name, rel):
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    subdirs.append(rel)
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue  # symlinks, sockets, devices
                res.files += 1
                res.bytes += st.st_size
                if res.files % REPORT_EVERY == 0:  # a big folder still shows progress
                    reported = _report(status, disk, abs_dir, res, reported)
                k = known.get(e.name)
                if k is None:
                    res.new += 1
                    w.inserts.append(
                        (root["id"], rel_dir, e.name, st.st_size, st.st_mtime_ns, st.st_dev,
                         st.st_ino, st.st_nlink, ext_of(e.name), run_id)
                    )
                elif (k["size"], k["mtime_ns"], k["inode"], k["dev"]) != (
                    st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev
                ):
                    res.changed += 1
                    w.updates.append(
                        (st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino, st.st_nlink,
                         ext_of(e.name), run_id, k["id"])
                    )
                else:
                    w.touches.append((run_id, st.st_nlink, k["id"]))
            stack.extend(sorted(subdirs, reverse=True))
            res.dirs += 1
            w.maybe_flush()
            reported = _report(status, disk, abs_dir, res, reported)
        w.flush()
        res.complete = True
    finally:
        try:
            w.flush()
        except sqlite3.Error:
            pass
    if res.complete:
        with Tx(conn):
            conn.execute(
                "UPDATE files SET state='gone' WHERE root_id=? AND last_seen_run<? AND state='present' "
                "AND container_id=0",
                (root["id"], run_id),
            )
            conn.execute(
                "UPDATE roots SET last_scan_job=?, last_scan_at=? WHERE id=?",
                (run_id, time.time(), root["id"]),
            )
    return res


REPORT_EVERY = 500


def _report(status: Status, disk: str, current: str, res: WalkResult, last: tuple) -> tuple:
    now = (res.files, res.new, res.changed, res.bytes, res.dirs)
    for key, a, b in zip(("walked", "new", "changed", "walked_bytes", "walked_dirs"), now, last, strict=True):
        if a != b:
            status.add(disk, key, a - b)
    status.disk(disk, current=current)
    return now

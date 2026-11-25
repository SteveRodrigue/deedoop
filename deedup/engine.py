"""The exact-duplicate scan: walk, partial hash, full hash, group.

Reads are organised per disk. In ``single`` mode the disks are processed one
after the other, so only one array disk works at a time; in ``parallel``
mode up to ``parallel_disks`` disks are read at once, one reader thread
each. Hashes are computed lazily: only files that share a size with another
physical file get a partial hash, and only files that also share a partial
hash get a full BLAKE3 hash.

The scan is resumable: hashes are committed as they are made, and roots
already walked by an interrupted job are not walked again when it resumes.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import db
from .categories import group_category
from .control import Control, Status
from .discovery import natural_key, sync_share_roots
from .hashing import Expected, FileChanged, full_hash, partial_hash
from .scanner import Excluder, walk_root

log = logging.getLogger("deedup.engine")

FLUSH_SECONDS = 2.0


@dataclass
class ScanContext:
    db_path: Path
    mnt: Path
    job_id: int
    control: Control
    status: Status


def _disks_in_order(rows: list[sqlite3.Row]) -> list[str]:
    """Array disks first (disk1, disk2, ... disk10), then pools and other devices."""
    return sorted({r["disk"] for r in rows},
                  key=lambda d: (0 if re.fullmatch(r"disk\d+", d) else 1, natural_key(d)))


def for_each_disk(disks: list[str], settings: dict, fn: Callable[[str], None]) -> None:
    """Run ``fn(disk)`` for every disk, one at a time or in parallel."""
    if settings["disk_mode"] == "parallel" and len(disks) > 1:
        workers = max(1, min(int(settings["parallel_disks"]), len(disks)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="disk") as pool:
            for f in [pool.submit(fn, d) for d in disks]:
                f.result()
    else:
        for d in disks:
            fn(d)


def run_scan(ctx: ScanContext) -> dict:
    conn = db.connect(ctx.db_path)
    try:
        sync_share_roots(conn, ctx.mnt)
        settings = db.get_settings(conn)
        roots = conn.execute("SELECT * FROM roots WHERE enabled = 1 ORDER BY path").fetchall()
        disks = _disks_in_order(roots)
        st = ctx.status
        st.set(phase="walk", disks_order=disks, mode=settings["disk_mode"])
        for d in disks:
            st.disk(d, state="waiting")

        # 1. walk
        excluder = Excluder(settings["exclusions"])

        def walk_disk(disk: str) -> None:
            c = db.connect(ctx.db_path)
            try:
                st.disk(disk, state="walking")
                for r in roots:
                    if r["disk"] != disk:
                        continue
                    if r["last_scan_job"] == ctx.job_id:
                        continue  # already walked before a restart
                    walk_root(c, r, ctx.job_id, excluder, ctx.control, st)
                st.disk(disk, state="walked", current="")
            finally:
                c.close()

        for_each_disk(disks, settings, walk_disk)
        _save_cursor(conn, ctx.job_id, {"phase": "hash_partial"})

        # 2. partial hashes, 3. full hashes
        min_size = max(1, int(settings["min_size"]))
        for phase in ("hash_partial", "hash_full"):
            st.set(phase=phase)
            todo = _candidates(conn, phase, min_size)
            by_disk = {d: [] for d in disks}
            for row in todo:
                by_disk.setdefault(row["disk"], []).append(row)
            for d, rows in by_disk.items():
                st.disk(d, state="waiting" if rows else "idle", todo_files=len(rows),
                        todo_bytes=sum(r["size"] for r in rows) if phase == "hash_full" else 0,
                        done_files=0, done_bytes=0, current="")

            def hash_disk(disk: str, phase=phase, by_disk=by_disk) -> None:
                _hash_disk(ctx, disk, phase, by_disk.get(disk, []), settings)

            for_each_disk([d for d in disks if by_disk.get(d)], settings, hash_disk)
            _save_cursor(conn, ctx.job_id, {"phase": phase + "_done"})

        # 4. groups
        st.set(phase="group")
        summary = rebuild_exact_groups(conn, min_size)
        st.set(phase="done")
        return summary
    finally:
        conn.close()


def _save_cursor(conn: sqlite3.Connection, job_id: int, cursor: dict) -> None:
    conn.execute("UPDATE jobs SET cursor = ? WHERE id = ?", (json.dumps(cursor), job_id))


_LIVE = """
    SELECT f.id, f.size, f.dev, f.inode, f.mtime_ns, f.dir, f.name, r.disk, r.path AS root_path {extra}
    FROM files f JOIN roots r ON r.id = f.root_id {join}
    WHERE f.state = 'present' AND r.enabled = 1 AND f.container_id = 0 AND f.size >= :min
"""


def _candidates(conn: sqlite3.Connection, phase: str, min_size: int) -> list[sqlite3.Row]:
    if phase == "hash_partial":
        live = _LIVE.format(extra="", join="")
        sql = f"""
            WITH live AS ({live}),
            keys AS (SELECT size FROM live GROUP BY size HAVING COUNT(DISTINCT dev || ':' || inode) > 1)
            SELECT live.* FROM live JOIN keys USING (size)
            WHERE NOT EXISTS (SELECT 1 FROM hashes h WHERE h.file_id = live.id AND h.kind = 'partial')
            ORDER BY live.disk, live.dev, live.inode
        """
    else:
        live = _LIVE.format(
            extra=", p.value AS pv",
            join="JOIN hashes p ON p.file_id = f.id AND p.kind = 'partial'",
        )
        sql = f"""
            WITH live AS ({live}),
            keys AS (SELECT size, pv FROM live GROUP BY size, pv
                     HAVING COUNT(DISTINCT dev || ':' || inode) > 1)
            SELECT live.* FROM live JOIN keys USING (size, pv)
            WHERE NOT EXISTS (SELECT 1 FROM hashes h WHERE h.file_id = live.id AND h.kind = 'blake3')
            ORDER BY live.disk, live.dev, live.inode
        """
    return conn.execute(sql, {"min": min_size}).fetchall()


def _hash_disk(ctx: ScanContext, disk: str, phase: str, rows: list[sqlite3.Row], settings: dict) -> None:
    st, control = ctx.status, ctx.control
    conn = db.connect(ctx.db_path)
    threads = max(1, int(settings["hash_threads"]))
    pending: list[tuple] = []
    errors: list[tuple] = []
    last_flush = time.monotonic()
    last_phys: tuple | None = None
    last_values: list[tuple[str, bytes]] = []

    def flush() -> None:
        nonlocal pending, errors, last_flush
        if pending or errors:
            with db.Tx(conn):
                conn.executemany(
                    "INSERT OR REPLACE INTO hashes(file_id, kind, value) VALUES (?, ?, ?)", pending
                )
                conn.executemany("UPDATE files SET error = ? WHERE id = ?", errors)
                conn.executemany(
                    "UPDATE files SET error = NULL WHERE id = ? AND error IS NOT NULL",
                    [(fid,) for fid in {p[0] for p in pending}],
                )
        pending, errors = [], []
        last_flush = time.monotonic()

    st.disk(disk, state="hashing" if phase == "hash_partial" else "reading")
    try:
        for row in rows:
            control.checkpoint()
            phys = (row["dev"], row["inode"], row["size"], row["mtime_ns"])
            if phys == last_phys:
                # Hardlink to the file just hashed: same content, no second read.
                pending.extend((row["id"], k, v) for k, v in last_values)
                st.add(disk, "done_files", 1)
                continue
            path = os.path.join(row["root_path"], row["dir"], row["name"])
            st.disk(disk, current=path)
            exp = Expected(row["size"], row["mtime_ns"])
            try:
                if phase == "hash_partial":
                    part, full = partial_hash(path, exp)
                    values = [("partial", part)] + ([("blake3", full)] if full else [])
                else:
                    values = [(
                        "blake3",
                        full_hash(path, exp, threads,
                                  on_bytes=lambda n: st.add(disk, "done_bytes", n),
                                  checkpoint=control.checkpoint),
                    )]
            except FileChanged:
                errors.append(("changed while reading; will retry next scan", row["id"]))
                last_phys = None
                continue
            except OSError as e:
                errors.append((f"{type(e).__name__}: {e.strerror or e}", row["id"]))
                last_phys = None
                continue
            pending.extend((row["id"], k, v) for k, v in values)
            last_phys, last_values = phys, values
            st.add(disk, "done_files", 1)
            if time.monotonic() - last_flush > FLUSH_SECONDS:
                flush()
        flush()
        st.disk(disk, state="hashed", current="")
    finally:
        try:
            flush()
        finally:
            conn.close()


def rebuild_exact_groups(conn: sqlite3.Connection, min_size: int = 1) -> dict:
    """Recompute the ``exact`` duplicate groups from stored BLAKE3 hashes."""
    rows = conn.execute(
        """
        WITH live AS (
            SELECT f.id, f.size, f.dev, f.inode, f.ext, h.value
            FROM files f
            JOIN roots r ON r.id = f.root_id
            JOIN hashes h ON h.file_id = f.id AND h.kind = 'blake3'
            WHERE f.state = 'present' AND r.enabled = 1 AND f.size >= :min
        ),
        keys AS (
            SELECT size, value FROM live GROUP BY size, value
            HAVING COUNT(DISTINCT dev || ':' || inode) > 1
        )
        SELECT live.* FROM live JOIN keys USING (size, value) ORDER BY live.size, live.value, live.id
        """,
        {"min": min_size},
    ).fetchall()
    now = time.time()
    groups = wasted = files = 0
    with db.Tx(conn):
        conn.execute("DELETE FROM groups WHERE method = 'exact'")
        for (size, value), members in itertools.groupby(rows, key=lambda r: (r["size"], r["value"])):
            members = list(members)
            copies = len({(m["dev"], m["inode"]) for m in members})
            w = size * (copies - 1)
            cur = conn.execute(
                "INSERT INTO groups(method, key, size, file_count, copies, wasted, category, built_at) "
                "VALUES ('exact', ?, ?, ?, ?, ?, ?, ?)",
                (value.hex(), size, len(members), copies, w, group_category([m["ext"] for m in members]), now),
            )
            gid = cur.lastrowid
            conn.executemany(
                "INSERT INTO group_members(group_id, file_id) VALUES (?, ?)", [(gid, m["id"]) for m in members]
            )
            groups += 1
            wasted += w
            files += len(members)
    return {"groups": groups, "wasted": wasted, "files": files}

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
from dataclasses import dataclass, field
from pathlib import Path

from . import db, scanners
from .archive_index import (
    archive_candidates,
    archive_disk,
    json_disk,
    loose_json_candidates,
    pair_sidecars,
    refresh_coverage,
)
from .categories import group_category
from .control import Control, JobProblem, Status, fmt_bytes
from .discovery import natural_key, sync_share_roots
from .hashing import Expected, FileChanged, full_hash, partial_hash
from .imaging import IMAGE_HASH_EXTS, MAX_IMAGE_BYTES, NotAnImage, read_and_hash_image, read_image_bytes
from .metadata import METADATA_EXTS, Reader, exiftool_available
from .scanner import Excluder, walk_root
from .similar import DEFAULT_THRESHOLD, MAX_DISTANCE, rebuild_pairs, rebuild_similar_groups, similar_hashes

log = logging.getLogger("deedoop.engine")

FLUSH_SECONDS = 2.0
META_BATCH = 100
# Phases whose progress is shown in bytes rather than files.
BYTE_PHASES = {"hash_full", "hash_image", "hash_similar", "archives", "hash_stream"}


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


@dataclass
class Scan:
    """One scan as the scanner plugins see it (see ``scanners.py``)."""

    ctx: ScanContext
    conn: sqlite3.Connection
    settings: dict
    disks: list[str]
    min_size: int = 1
    pairs: bool = True  # rebuild similar pairs and archive coverage too (False after a change: groups only)
    _once: set = field(default_factory=set)

    def once(self, name: str) -> bool:
        """True when a hook shared by two scanners already ran in this scan."""
        if name in self._once:
            return True
        self._once.add(name)
        return False

    def run_phase(self, phase: str, todo: list[sqlite3.Row], worker) -> None:
        st, disks, settings, ctx = self.ctx.status, self.disks, self.settings, self.ctx
        st.set(phase=phase)
        by_disk: dict[str, list] = {d: [] for d in disks}
        for row in todo:
            by_disk.setdefault(row["disk"], []).append(row)
        for d, rows in by_disk.items():
            st.disk(d, state="waiting" if rows else "idle", todo_files=len(rows),
                    todo_bytes=sum(r["size"] for r in rows) if phase in BYTE_PHASES else 0,
                    done_files=0, done_bytes=0, current="")
        if todo:
            size = f" ({fmt_bytes(sum(r['size'] for r in todo))})" if phase in BYTE_PHASES else ""
            st.log("info", f"{len(todo):,} files to read{size} on {sum(1 for r in by_disk.values() if r)} disks")
        else:
            st.log("info", "Nothing to read")
        for_each_disk([d for d in disks if by_disk.get(d)], settings,
                      lambda disk: worker(ctx, disk, phase, by_disk.get(disk, []), settings))
        _save_cursor(self.conn, ctx.job_id, {"phase": phase + "_done"})


def run_scan(ctx: ScanContext) -> dict:
    conn = db.connect(ctx.db_path)
    try:
        sync_share_roots(conn, ctx.mnt)
        settings = db.get_settings(conn)
        roots = conn.execute("SELECT * FROM roots WHERE enabled = 1 ORDER BY path").fetchall()
        if not roots:
            raise JobProblem("Nothing to scan: no share or folder is selected. Tick the shares to scan "
                             "on the Setup screen, save, then start the scan again.")
        disks = _disks_in_order(roots)
        st = ctx.status
        on = scanners.active_keys(settings)
        st.set(phase="walk", disks_order=disks, mode=settings["disk_mode"], scanners=on,
               plan=scanners.plan(settings))
        st.log("info", f"{len(roots)} folders on {len(disks)} disks, "
                       f"{'one disk at a time' if settings['disk_mode'] == 'single' else 'disks in parallel'}; "
                       f"scanners: {', '.join(scanners.REGISTRY[k].label for k in on) or 'none (raw content only)'}")
        for d in disks:
            st.disk(d, state="waiting")

        # 1. walk
        excluder = Excluder(settings["exclusions"])

        def walk_disk(disk: str) -> None:
            c = db.connect(ctx.db_path)
            try:
                st.disk(disk, state="walking")
                files = new = changed = 0
                for r in roots:
                    if r["disk"] != disk:
                        continue
                    if r["last_scan_job"] == ctx.job_id:
                        continue  # already walked before a restart
                    res = walk_root(c, r, ctx.job_id, excluder, ctx.control, st)
                    files, new, changed = files + res.files, new + res.new, changed + res.changed
                st.disk(disk, state="walked", current="")
                st.log("info", f"{disk}: {files:,} files, {new:,} new, {changed:,} changed")
            finally:
                c.close()

        for_each_disk(disks, settings, walk_disk)
        _save_cursor(conn, ctx.job_id, {"phase": "walked"})
        scan = Scan(ctx, conn, settings, disks, max(1, int(settings["min_size"])))

        # 2. scanners that read before the exact hashes: archives, photos, media headers
        for key in scanners.active_keys(settings):
            if scanners.REGISTRY[key].index:
                scanners.hook(scanners.REGISTRY[key].index)(scan)
        # 3. metadata: headers only, through ExifTool, for the photo and video scanners
        if settings["metadata"] and (exts := scanners.exiftool_exts(settings)):
            if exiftool_available():
                scan.run_phase("metadata", _metadata_candidates(conn, exts), _metadata_disk)
            else:
                log.warning("exiftool not found; skipping metadata")
        # 4. partial hashes, 5. full hashes: every file type, always
        for phase in ("hash_partial", "hash_full"):
            scan.run_phase(phase, _candidates(conn, phase, scan.min_size), _hash_disk)
        # 6. scanners that need the exact hashes: media stream hashes
        for key in scanners.active_keys(settings):
            if scanners.REGISTRY[key].after_hashes:
                scanners.hook(scanners.REGISTRY[key].after_hashes)(scan)

        # 7. groups
        st.set(phase="group")
        summary = regroup(conn, settings, scan=scan)
        st.set(phase="done")
        return summary
    finally:
        conn.close()


def regroup(conn: sqlite3.Connection, settings: dict, scan: Scan | None = None, pairs: bool = True) -> dict:
    """Rebuild every duplicate group from the stored hashes, for the scanners
    that are on, and drop the groups and marks of those that are off. Runs at
    the end of a scan, after changes, and when Setup turns a scanner off."""
    scan = scan or Scan(ScanContext(Path(), Path(), 0, Control(), Status()), conn, settings, [],
                        max(1, int(settings["min_size"])))
    scan.pairs = pairs
    summary = rebuild_exact_groups(conn, scan.min_size, members=scanners.active(settings, "archive"))
    on = scanners.active_keys(settings)
    for key, s in scanners.REGISTRY.items():
        if key not in on and s.drop:
            scanners.hook(s.drop)(scan)
    for key in on:
        if scanners.REGISTRY[key].group:
            summary.update(scanners.hook(scanners.REGISTRY[key].group)(scan) or {})
    methods = scanners.methods(settings)
    with db.Tx(conn):
        conn.execute(f"DELETE FROM marks WHERE method NOT IN ({','.join('?' * len(methods))})", methods)
    return summary


# -- the photo scanner (milestones 2 and 4) --------------------------------------------


def photo_index(scan: Scan) -> None:
    conn, settings, min_size = scan.conn, scan.settings, scan.min_size
    # one full read gives the image-data hash and the exact hashes
    similar = bool(settings["similar_matching"])
    if settings["image_matching"] or similar:
        scan.run_phase("hash_image", _image_candidates(conn, min_size), _hash_disk)
    # perceptual hashes for photos indexed before similar matching was on
    if similar:
        scan.run_phase("hash_similar", _similar_candidates(conn, min_size), _hash_disk)


def photo_plan(settings: dict) -> set[str]:
    out = set()
    if settings["image_matching"] or settings["similar_matching"]:
        out.add("hash_image")
    if settings["similar_matching"]:
        out.add("hash_similar")
    return out


def photo_group(scan: Scan) -> dict:
    conn, settings, min_size = scan.conn, scan.settings, scan.min_size
    members = scanners.active(settings, "archive")
    summary: dict = {}
    img = rebuild_image_groups(conn, min_size, members=members) if settings["image_matching"] \
        else _drop_groups(conn, "image")
    summary.update({"image_groups": img["groups"], "image_wasted": img["wasted"]})
    if settings["similar_matching"]:
        if scan.pairs:
            scan.ctx.status.set(phase="similar")
            summary["similar_pairs"] = rebuild_pairs(conn)
        sim = rebuild_similar_groups(conn, similar_threshold(settings))
        summary.update({"similar_groups": sim["groups"], "similar_wasted": sim["wasted"]})
    else:
        _drop_groups(conn, "similar")
        conn.execute("DELETE FROM similar_pairs")
    return summary


def photo_drop(scan: Scan) -> None:
    for m in ("image", "similar"):
        _drop_groups(scan.conn, m)
    scan.conn.execute("DELETE FROM similar_pairs")


# -- the archive scanner (milestone 3) ---------------------------------------------------


def archive_index(scan: Scan) -> None:
    # members are streamed through the same hashes, nothing is extracted
    scan.run_phase("archives", archive_candidates(scan.conn, scan.settings), archive_disk)
    scan.run_phase("sidecars", loose_json_candidates(scan.conn, scan.settings), json_disk)


def archive_group(scan: Scan) -> dict:
    conn, settings = scan.conn, scan.settings
    similar = scanners.active(settings, "photo") and settings["similar_matching"]
    out = {"sidecars": pair_sidecars(conn)}
    if scan.pairs:
        refresh_coverage(conn, similar_threshold(settings) if similar else None)
    out["archives"] = conn.execute("SELECT COUNT(*) FROM archives").fetchone()[0]
    return out


def _save_cursor(conn: sqlite3.Connection, job_id: int, cursor: dict) -> None:
    conn.execute("UPDATE jobs SET cursor = ? WHERE id = ?", (json.dumps(cursor), job_id))


# One physical copy: a loose file is its device and inode (hardlinks are one
# copy); an archive member is a copy of its own.
PHYS = "CASE WHEN f.container_id = 0 THEN f.dev || ':' || f.inode ELSE 'm' || f.id END"

_LIVE = f"""
    SELECT f.id, f.size, f.dev, f.inode, f.mtime_ns, f.dir, f.name, f.container_id, {PHYS} AS phys,
           r.disk, r.path AS root_path {{extra}}
    FROM files f JOIN roots r ON r.id = f.root_id {{join}}
    WHERE f.state = 'present' AND r.enabled = 1 AND f.size >= :min
"""


def _candidates(conn: sqlite3.Connection, phase: str, min_size: int) -> list[sqlite3.Row]:
    if phase == "hash_partial":
        live = _LIVE.format(extra="", join="")
        sql = f"""
            WITH live AS ({live}),
            keys AS (SELECT size FROM live GROUP BY size HAVING COUNT(DISTINCT phys) > 1)
            SELECT live.* FROM live JOIN keys USING (size)
            WHERE live.container_id = 0 AND NOT EXISTS (SELECT 1 FROM hashes h WHERE h.file_id = live.id AND h.kind = 'partial')
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
                     HAVING COUNT(DISTINCT phys) > 1)
            SELECT live.* FROM live JOIN keys USING (size, pv)
            WHERE live.container_id = 0 AND NOT EXISTS (SELECT 1 FROM hashes h WHERE h.file_id = live.id AND h.kind = 'blake3')
            ORDER BY live.disk, live.dev, live.inode
        """
    return conn.execute(sql, {"min": min_size}).fetchall()


def _image_candidates(conn: sqlite3.Connection, min_size: int) -> list[sqlite3.Row]:
    exts = sorted(IMAGE_HASH_EXTS)
    live = _LIVE.format(extra=", f.ext", join="")
    sql = f"""
        WITH live AS ({live} AND f.container_id = 0 AND f.ext IN ({",".join("?" * len(exts))})
                      AND f.size <= {MAX_IMAGE_BYTES})
        SELECT * FROM live
        WHERE NOT EXISTS (SELECT 1 FROM hashes h WHERE h.file_id = live.id AND h.kind IN ('img_exact', 'img_none'))
        ORDER BY live.disk, live.dev, live.inode
    """
    return conn.execute(sql.replace(":min", "?"), [min_size, *exts]).fetchall()


def _similar_candidates(conn: sqlite3.Connection, min_size: int) -> list[sqlite3.Row]:
    exts = sorted(IMAGE_HASH_EXTS)
    live = _LIVE.format(extra=", f.ext", join="")
    sql = f"""
        WITH live AS ({live} AND f.container_id = 0 AND f.ext IN ({",".join("?" * len(exts))})
                      AND f.size <= {MAX_IMAGE_BYTES})
        SELECT * FROM live
        WHERE EXISTS (SELECT 1 FROM hashes h WHERE h.file_id = live.id AND h.kind = 'img_exact')
          AND NOT EXISTS (SELECT 1 FROM hashes h WHERE h.file_id = live.id AND h.kind IN ('phash', 'sim_none'))
        ORDER BY live.disk, live.dev, live.inode
    """
    return conn.execute(sql.replace(":min", "?"), [min_size, *exts]).fetchall()


def similar_threshold(settings: dict) -> int:
    try:
        return max(0, min(int(settings["similar_threshold"]), MAX_DISTANCE))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD


def _metadata_candidates(conn: sqlite3.Connection, exts: set[str] | None = None) -> list[sqlite3.Row]:
    exts = sorted(exts if exts is not None else METADATA_EXTS)
    live = _LIVE.format(extra=", f.ext", join="")
    sql = f"""
        WITH live AS ({live} AND f.container_id = 0 AND f.ext IN ({",".join("?" * len(exts))}))
        SELECT * FROM live WHERE NOT EXISTS (SELECT 1 FROM media m WHERE m.file_id = live.id)
        ORDER BY live.disk, live.root_path, live.dir, live.name
    """
    return conn.execute(sql.replace(":min", "?"), [1, *exts]).fetchall()


def _metadata_disk(ctx: ScanContext, disk: str, phase: str, rows: list[sqlite3.Row], settings: dict) -> None:
    st, control = ctx.status, ctx.control
    conn = db.connect(ctx.db_path)
    st.disk(disk, state="reading metadata")
    try:
        with Reader() as reader:
            for i in range(0, len(rows), META_BATCH):
                control.checkpoint()
                batch = rows[i:i + META_BATCH]
                paths = {os.path.join(r["root_path"], r["dir"], r["name"]): r["id"] for r in batch}
                st.disk(disk, current=next(iter(paths)))
                found = reader.read(list(paths))
                now = time.time()
                with db.Tx(conn):
                    conn.executemany(
                        "INSERT OR REPLACE INTO media(file_id, mime, taken_at, camera, width, height, duration, "
                        "gps_lat, gps_lon, orientation, extracted_at, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(fid, m.mime, m.taken_at, m.camera, m.width, m.height, m.duration, m.gps_lat,
                          m.gps_lon, m.orientation, now, m.error)
                         for p, fid in paths.items() for m in [found[p]]],
                    )
                st.add(disk, "done_files", len(batch))
        st.disk(disk, state="done", current="")
    finally:
        conn.close()


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

    st.disk(disk, state={"hash_partial": "hashing", "hash_image": "reading photos",
                         "hash_similar": "reading photos"}.get(phase, "reading"))
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
                if phase == "hash_similar":
                    values = list(similar_hashes(read_image_bytes(path, exp, control.checkpoint)).items())
                    st.add(disk, "done_bytes", row["size"])
                elif phase == "hash_image":
                    got = read_and_hash_image(path, row["ext"], exp, checkpoint=control.checkpoint,
                                              similar=bool(settings["similar_matching"]))
                    err = got.pop("img_error", None)
                    values = list(got.items()) + ([("img_none", err)] if err else [])
                    st.add(disk, "done_bytes", row["size"])
                elif phase == "hash_partial":
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
                st.log("warn", f"{path}: changed while reading; will retry next scan")
                last_phys = None
                continue
            except NotAnImage as e:  # too large to hash as an image: exact hashes still follow
                values = [("img_none", str(e).encode())]
                pending.extend((row["id"], k, v) for k, v in values)
                last_phys = None
                st.add(disk, "done_files", 1)
                continue
            except OSError as e:
                errors.append((f"{type(e).__name__}: {e.strerror or e}", row["id"]))
                st.log("warn", f"{path}: cannot read ({e.strerror or type(e).__name__})")
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


def rebuild_exact_groups(conn: sqlite3.Connection, min_size: int = 1, members: bool = True) -> dict:
    """Recompute the ``exact`` duplicate groups from stored BLAKE3 hashes.

    Archive members join groups, but only loose files count as reclaimable:
    a copy inside a zip cannot be deleted on its own, and the last loose
    copy is never counted because an archive also holds it. With the archive
    scanner off (``members=False``), archive members are left out.
    """
    rows = conn.execute(
        f"""
        WITH live AS (
            SELECT f.id, f.size, f.ext, f.container_id, {PHYS} AS phys, h.value
            FROM files f
            JOIN roots r ON r.id = f.root_id
            JOIN hashes h ON h.file_id = f.id AND h.kind = 'blake3'
            WHERE f.state = 'present' AND r.enabled = 1 AND f.size >= :min AND (:members OR f.container_id = 0)
        ),
        keys AS (
            SELECT size, value FROM live GROUP BY size, value
            HAVING COUNT(DISTINCT phys) > 1
        )
        SELECT live.* FROM live JOIN keys USING (size, value) ORDER BY live.size, live.value, live.id
        """,
        {"min": min_size, "members": int(members)},
    ).fetchall()
    now = time.time()
    groups = wasted = files = 0
    with db.Tx(conn):
        conn.execute("DELETE FROM groups WHERE method = 'exact'")
        for (size, value), members in itertools.groupby(rows, key=lambda r: (r["size"], r["value"])):
            members = list(members)
            copies = len({m["phys"] for m in members})
            loose = len({m["phys"] for m in members if m["container_id"] == 0})
            w = size * max(loose - 1, 0)
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


def _drop_groups(conn: sqlite3.Connection, method: str) -> dict:
    conn.execute("DELETE FROM groups WHERE method = ?", (method,))
    return {"groups": 0, "wasted": 0, "files": 0}


def rebuild_image_groups(conn: sqlite3.Connection, min_size: int = 1, members: bool = True) -> dict:
    """Recompute the ``image`` groups: photos with the same image data but
    different bytes (so their metadata differs). Groups whose members are all
    byte-identical are left to the ``exact`` method.

    Reclaimable space counts each physical copy once and keeps the largest
    file, which usually carries the most metadata.
    """
    rows = conn.execute(
        f"""
        WITH live AS (
            SELECT f.id, f.size, f.ext, f.container_id, {PHYS} AS phys, i.value AS img, b.value AS full
            FROM files f
            JOIN roots r ON r.id = f.root_id
            JOIN hashes i ON i.file_id = f.id AND i.kind = 'img_exact'
            JOIN hashes b ON b.file_id = f.id AND b.kind = 'blake3'
            WHERE f.state = 'present' AND r.enabled = 1 AND f.size >= :min AND (:members OR f.container_id = 0)
        ),
        keys AS (SELECT img FROM live GROUP BY img HAVING COUNT(DISTINCT full) > 1)
        SELECT live.* FROM live JOIN keys USING (img) ORDER BY live.img, live.size DESC, live.id
        """,
        {"min": min_size, "members": int(members)},
    ).fetchall()
    now = time.time()
    groups = wasted = files = 0
    with db.Tx(conn):
        conn.execute("DELETE FROM groups WHERE method = 'image'")
        for img, members in itertools.groupby(rows, key=lambda r: r["img"]):
            members = list(members)
            phys: dict[str, int] = {}
            loose: dict[str, int] = {}
            for m in members:
                phys[m["phys"]] = m["size"]
                if m["container_id"] == 0:
                    loose[m["phys"]] = m["size"]
            sizes = sorted(phys.values(), reverse=True)
            ls = sorted(loose.values(), reverse=True)
            w = sum(ls[1:])
            cur = conn.execute(
                "INSERT INTO groups(method, key, size, file_count, copies, wasted, category, built_at) "
                "VALUES ('image', ?, ?, ?, ?, ?, 'image', ?)",
                (img.hex(), sizes[0], len(members), len(phys), w, now),
            )
            conn.executemany(
                "INSERT INTO group_members(group_id, file_id) VALUES (?, ?)",
                [(cur.lastrowid, m["id"]) for m in members],
            )
            groups += 1
            wasted += w
            files += len(members)
    return {"groups": groups, "wasted": wasted, "files": files}

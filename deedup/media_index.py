"""Scan phases and groups for video and audio (milestone 7).

1. ``probe``: ffprobe reads the headers of every video and song once (codecs,
   duration, tags) into the ``av`` table.
2. ``hash_stream``: files that share a stream layout and a length with
   another file, and are not already plain exact copies of it, get a stream
   hash (the compressed packets, tags and container left out).
3. Groups of the ``media`` method ("Same recording, other tags"): files with
   the same stream hash whose bytes differ. Byte-identical files stay in the
   ``exact`` method. The suggested keeper has the most complete tags.
"""

from __future__ import annotations

import itertools
import os
import sqlite3
import time
from collections import defaultdict

from . import av, db, scanners
from .categories import group_category

FLUSH = 50
DURATION_TOLERANCE = 1.0  # seconds: a remux can round the container's duration differently


# -- hooks called by the engine through the scanner registry ------------------------


def index(scan) -> None:
    if scan.once("media_index"):
        return
    scan.run_phase("probe", probe_candidates(scan.conn, scanners.media_exts(scan.settings)), probe_disk)


def after_hashes(scan) -> None:
    if scan.once("media_after_hashes"):
        return
    scan.run_phase("hash_stream", stream_candidates(scan.conn, scanners.media_exts(scan.settings), scan.min_size),
                   stream_disk)


def group(scan) -> dict:
    if scan.once("media_group"):
        return {}
    res = rebuild_media_groups(scan.conn, scanners.media_exts(scan.settings), scan.min_size)
    _drop_stale_marks(scan.conn)
    return {"media_groups": res["groups"], "media_wasted": res["wasted"]}


def drop(scan) -> None:
    if not scanners.media_kinds(scan.settings):
        scan.conn.execute("DELETE FROM groups WHERE method = 'media'")
        _drop_stale_marks(scan.conn)


def _drop_stale_marks(conn: sqlite3.Connection) -> None:
    """A copy marked as "same recording" whose group is gone (its scanner was
    turned off, or a file changed) is no longer marked."""
    with db.Tx(conn):
        conn.execute("""DELETE FROM marks WHERE method = 'media' AND NOT EXISTS (
                            SELECT 1 FROM group_members m JOIN groups g ON g.id = m.group_id
                            WHERE g.method = 'media' AND m.file_id = marks.file_id)""")


# -- 1. headers -------------------------------------------------------------------

_LOOSE = """
    FROM files f JOIN roots r ON r.id = f.root_id
    WHERE f.state = 'present' AND r.enabled = 1 AND f.container_id = 0
"""


def _marks(n: int) -> str:
    return ",".join("?" * n)


def probe_candidates(conn: sqlite3.Connection, exts: list[str]) -> list[sqlite3.Row]:
    if not exts:
        return []
    return conn.execute(
        f"""SELECT f.id, f.size, f.mtime_ns, f.dev, f.inode, f.dir, f.name, f.ext, r.disk, r.path AS root_path
            {_LOOSE} AND f.ext IN ({_marks(len(exts))})
              AND NOT EXISTS (SELECT 1 FROM av WHERE av.file_id = f.id)
            ORDER BY r.disk, r.path, f.dir, f.name""",  # SC5: placeholders only
        exts,
    ).fetchall()


def _av_row(fid: int, p: av.Probe, now: float) -> tuple:
    t = p.tags
    return (fid, p.kind, p.container, p.duration, p.bitrate, p.vcodec, p.width, p.height, p.fps, p.acodec,
            p.channels, p.sample_rate, p.layout, t.get("artist"), t.get("album"), t.get("album_artist"),
            t.get("title"), p.number("track"), p.number("disc"), p.year, t.get("genre"), int(p.has_cover),
            p.tag_count, now, p.error)


AV_INSERT = ("INSERT OR REPLACE INTO av(file_id, kind, container, duration, bitrate, vcodec, width, height, fps, "
             "acodec, channels, sample_rate, layout, artist, album, album_artist, title, track, disc, year, genre, "
             "has_cover, tags, probed_at, error) VALUES (" + _marks(25) + ")")


def probe_disk(ctx, disk: str, phase: str, rows: list[sqlite3.Row], settings: dict) -> None:
    st, control = ctx.status, ctx.control
    conn = db.connect(ctx.db_path)
    pending: list[tuple] = []

    def flush() -> None:
        nonlocal pending
        if pending:
            with db.Tx(conn):
                conn.executemany(AV_INSERT, pending)
        pending = []

    st.disk(disk, state="reading media headers")
    try:
        for row in rows:
            control.checkpoint()
            path = os.path.join(row["root_path"], row["dir"], row["name"])
            st.disk(disk, current=path)
            pending.append(_av_row(row["id"], av.probe(path), time.time()))
            st.add(disk, "done_files", 1)
            if len(pending) >= FLUSH:
                flush()
        flush()
        st.disk(disk, state="done", current="")
    finally:
        try:
            flush()
        finally:
            conn.close()


# -- 2. stream hashes -----------------------------------------------------------------


def stream_candidates(conn: sqlite3.Connection, exts: list[str], min_size: int = 1) -> list[sqlite3.Row]:
    """Files worth a stream hash: they share a stream layout and (within a
    second) a duration with another physical file, and the files of that
    cluster are not all byte-identical. Files already hashed are left out,
    but still count as partners for new files."""
    if not exts:
        return []
    rows = conn.execute(
        f"""SELECT f.id, f.size, f.mtime_ns, f.dev, f.inode, f.dir, f.name, f.ext, r.disk, r.path AS root_path,
                   a.layout, a.duration, b.value AS full,
                   EXISTS (SELECT 1 FROM hashes h WHERE h.file_id = f.id
                           AND h.kind IN ('stream', 'stream_none')) AS done
            FROM files f JOIN roots r ON r.id = f.root_id
            JOIN av a ON a.file_id = f.id
            LEFT JOIN hashes b ON b.file_id = f.id AND b.kind = 'blake3'
            WHERE f.state = 'present' AND r.enabled = 1 AND f.container_id = 0 AND f.size >= ?
              AND f.ext IN ({_marks(len(exts))})
              AND a.error IS NULL AND a.layout != '' AND a.duration IS NOT NULL
            ORDER BY a.layout, a.duration""",  # SC5: placeholders only
        [min_size, *exts],
    ).fetchall()
    out: list[sqlite3.Row] = []
    for _layout, items in itertools.groupby(rows, key=lambda r: r["layout"]):
        cluster: list[sqlite3.Row] = []
        for r in items:
            if cluster and r["duration"] - cluster[-1]["duration"] > DURATION_TOLERANCE:
                out.extend(_worth_hashing(cluster))
                cluster = []
            cluster.append(r)
        out.extend(_worth_hashing(cluster))
    return sorted(out, key=lambda r: (r["disk"], r["dev"], r["inode"]))


def _identity(r) -> object:
    # Byte-identical files share a BLAKE3 hash (the exact phases hash every
    # file that shares a size with another). A file without one has a size
    # or quick hash no other file has, so it is unique.
    return r["full"] if r["full"] is not None else ("phys", r["dev"], r["inode"])


def _worth_hashing(cluster: list[sqlite3.Row]) -> list[sqlite3.Row]:
    if len({(r["dev"], r["inode"]) for r in cluster}) < 2 or len({_identity(r) for r in cluster}) < 2:
        return []
    return [r for r in cluster if not r["done"]]


def stream_disk(ctx, disk: str, phase: str, rows: list[sqlite3.Row], settings: dict) -> None:
    st, control = ctx.status, ctx.control
    conn = db.connect(ctx.db_path)
    pending: list[tuple] = []

    def flush() -> None:
        nonlocal pending
        if pending:
            with db.Tx(conn):
                conn.executemany("INSERT OR REPLACE INTO hashes(file_id, kind, value) VALUES (?, ?, ?)", pending)
        pending = []

    st.disk(disk, state="hashing media streams")
    last: tuple | None = None
    last_vals: list[tuple[str, bytes]] = []
    try:
        for row in rows:
            control.checkpoint()
            phys = (row["dev"], row["inode"], row["size"], row["mtime_ns"])
            if phys == last:  # a hardlink of the file just hashed
                pending.extend((row["id"], k, v) for k, v in last_vals)
                st.add(disk, "done_files", 1)
                continue
            path = os.path.join(row["root_path"], row["dir"], row["name"])
            st.disk(disk, current=path)
            vals = [hash_one(path, row["size"], row["mtime_ns"])]
            pending.extend((row["id"], k, v) for k, v in vals)
            last, last_vals = phys, vals
            st.add(disk, "done_files", 1)
            st.add(disk, "done_bytes", row["size"])
            if len(pending) >= FLUSH:
                flush()
        flush()
        st.disk(disk, state="done", current="")
    finally:
        try:
            flush()
        finally:
            conn.close()


def hash_one(path: str, size: int, mtime_ns: int) -> tuple[str, bytes]:
    """``('stream', hash)``, or ``('stream_none', reason)`` when the file
    cannot be read or changed while it was read."""
    try:
        before = os.stat(path)
        if (before.st_size, before.st_mtime_ns) != (size, mtime_ns):
            return "stream_none", b"changed since the scan; will retry next scan"
        value = av.stream_hash(path, size)
        after = os.stat(path)
        if (after.st_size, after.st_mtime_ns) != (size, mtime_ns):
            return "stream_none", b"changed while reading; will retry next scan"
        return "stream", value
    except (RuntimeError, ValueError) as e:
        return "stream_none", str(e).encode()[:300]
    except OSError as e:
        return "stream_none", f"{type(e).__name__}: {e.strerror or e}".encode()[:300]


# -- 3. groups --------------------------------------------------------------------------


def media_rank(m) -> tuple:
    """The copy to keep when the recording is identical: the most complete
    tags, then the largest file (usually more tags or cover art), then the
    shortest path."""
    return (m["tag_count"] or 0, m["size"])


def rebuild_media_groups(conn: sqlite3.Connection, exts: list[str], min_size: int = 1) -> dict:
    """Recompute the ``media`` groups: same stream hash, different bytes."""
    if not exts:
        conn.execute("DELETE FROM groups WHERE method = 'media'")
        return {"groups": 0, "wasted": 0, "files": 0}
    rows = conn.execute(
        f"""SELECT f.id, f.size, f.ext, f.dev, f.inode, s.value AS sh, b.value AS full, a.tags AS tag_count
            FROM files f JOIN roots r ON r.id = f.root_id
            JOIN hashes s ON s.file_id = f.id AND s.kind = 'stream'
            JOIN av a ON a.file_id = f.id
            LEFT JOIN hashes b ON b.file_id = f.id AND b.kind = 'blake3'
            WHERE f.state = 'present' AND r.enabled = 1 AND f.container_id = 0 AND f.size >= ?
              AND f.ext IN ({_marks(len(exts))})
            ORDER BY s.value, f.id""",  # SC5: placeholders only
        [min_size, *exts],
    ).fetchall()
    now = time.time()
    groups = wasted = files = 0
    with db.Tx(conn):
        conn.execute("DELETE FROM groups WHERE method = 'media'")
        for sh, members in itertools.groupby(rows, key=lambda r: r["sh"]):
            members = list(members)
            if len({_identity(m) for m in members}) < 2:
                continue  # all byte-identical: an exact group already
            phys: dict[tuple, sqlite3.Row] = {}
            for m in members:
                key = (m["dev"], m["inode"])
                if key not in phys or media_rank(m) > media_rank(phys[key]):
                    phys[key] = m
            if len(phys) < 2:
                continue  # hardlinks of one file
            keeper = max(phys.values(), key=media_rank)
            w = sum(m["size"] for m in phys.values()) - keeper["size"]
            cur = conn.execute(
                "INSERT INTO groups(method, key, size, file_count, copies, wasted, category, built_at) "
                "VALUES ('media', ?, ?, ?, ?, ?, ?, ?)",
                (sh.hex(), keeper["size"], len(members), len(phys), w,
                 group_category([m["ext"] for m in members]), now),
            )
            conn.executemany("INSERT INTO group_members(group_id, file_id) VALUES (?, ?)",
                             [(cur.lastrowid, m["id"]) for m in members])
            groups += 1
            wasted += w
            files += len(members)
    return {"groups": groups, "wasted": wasted, "files": files}


def counts_by_kind(conn: sqlite3.Connection) -> dict:
    out = defaultdict(int)
    for r in conn.execute("SELECT kind, COUNT(*) FROM av WHERE error IS NULL GROUP BY kind"):
        out[r[0]] = r[1]
    return dict(out)

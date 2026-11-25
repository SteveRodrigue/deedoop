"""Scan phases for milestone 3: index archives, pair Takeout sidecars and
compute how much of each archive is already on disk."""

from __future__ import annotations

import os
import sqlite3
import time
from collections import defaultdict

from . import db
from .archives import ARCHIVE_EXTS, JSON_LIMIT, Member, archive_kind, parse_sidecar, read_archive, takeout_part
from .hashing import Expected, FileChanged, _open
from .metadata import METADATA_EXTS
from .scanner import ext_of

# Version 2 (milestone 4) adds perceptual hashes for photos inside archives;
# archives indexed by an older version are read once more.
ARCHIVE_VERSION = 2


def in_scope(paths: list[str], *candidates: str) -> bool:
    for p in paths:
        p = p.rstrip("/")
        if p and any(c == p or c.startswith(p + "/") for c in candidates):
            return True
    return False


def archive_candidates(conn: sqlite3.Connection, settings: dict) -> list[sqlite3.Row]:
    exts = sorted(ARCHIVE_EXTS)
    rows = conn.execute(
        f"""SELECT f.id, f.size, f.dev, f.inode, f.mtime_ns, f.dir, f.name, r.disk, r.path AS root_path,
                   r.user_path, r.id AS root_id
            FROM files f JOIN roots r ON r.id = f.root_id
            WHERE f.state = 'present' AND r.enabled = 1 AND f.container_id = 0
              AND f.ext IN ({",".join("?" * len(exts))})
              AND NOT EXISTS (SELECT 1 FROM archives a WHERE a.file_id = f.id AND a.version >= ?)
            ORDER BY r.disk, r.path, f.dir, f.name""",
        [*exts, ARCHIVE_VERSION if settings.get("similar_matching") else 1],
    ).fetchall()
    paths = settings["takeout_paths"]
    out = []
    for r in rows:
        if not archive_kind(r["name"]):
            continue
        rel = f"{r['dir']}/{r['name']}" if r["dir"] else r["name"]
        if settings["archives_everywhere"] or in_scope(
            paths, f"{r['user_path']}/{rel}", f"{r['root_path']}/{rel}"
        ):
            out.append(r)
    return out


def loose_json_candidates(conn: sqlite3.Connection, settings: dict) -> list[sqlite3.Row]:
    """Takeout folders someone already extracted: read their JSON sidecars."""
    paths = settings["takeout_paths"]
    if not paths:
        return []
    rows = conn.execute(
        f"""SELECT f.id, f.size, f.mtime_ns, f.dir, f.name, r.disk, r.path AS root_path, r.user_path
            FROM files f JOIN roots r ON r.id = f.root_id
            WHERE f.state = 'present' AND r.enabled = 1 AND f.container_id = 0 AND f.ext = 'json'
              AND f.size <= {JSON_LIMIT}
              AND NOT EXISTS (SELECT 1 FROM sidecar_json s WHERE s.file_id = f.id)
              AND NOT EXISTS (SELECT 1 FROM hashes h WHERE h.file_id = f.id AND h.kind = 'json_none')"""
    ).fetchall()
    out = []
    for r in rows:
        rel = f"{r['dir']}/{r['name']}" if r["dir"] else r["name"]
        if in_scope(paths, f"{r['user_path']}/{rel}", f"{r['root_path']}/{rel}"):
            out.append(r)
    return out


def archive_disk(ctx, disk: str, phase: str, rows: list[sqlite3.Row], settings: dict) -> None:
    st, control = ctx.status, ctx.control
    conn = db.connect(ctx.db_path)
    st.disk(disk, state="opening archives")
    try:
        for row in rows:
            control.checkpoint()
            path = os.path.join(row["root_path"], row["dir"], row["name"])
            st.disk(disk, current=path)
            error = None
            try:
                members = read_archive(path, Expected(row["size"], row["mtime_ns"]), control.checkpoint)
            except FileChanged:
                st.add(disk, "done_bytes", row["size"])
                continue  # the next scan will see the change
            except Exception as e:  # noqa: BLE001 - a damaged archive must not stop the scan
                members, error = [], f"{type(e).__name__}: {e}"[:300]
            store_archive(conn, row, members, ctx.job_id, error)
            st.add(disk, "done_bytes", row["size"])
            st.add(disk, "done_files", 1)
        st.disk(disk, state="done", current="")
    finally:
        conn.close()


def json_disk(ctx, disk: str, phase: str, rows: list[sqlite3.Row], settings: dict) -> None:
    conn = db.connect(ctx.db_path)
    try:
        for row in rows:
            ctx.control.checkpoint()
            path = os.path.join(row["root_path"], row["dir"], row["name"])
            try:
                fd = _open(path)
                with os.fdopen(fd, "rb") as f:
                    data = f.read(JSON_LIMIT + 1)
            except OSError:
                continue
            side = parse_sidecar(data)
            with db.Tx(conn):
                if side:
                    conn.execute(
                        "INSERT OR REPLACE INTO sidecar_json(file_id, title, taken_at, lat, lon, description) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (row["id"], side["title"], side["taken_at"], side["lat"], side["lon"], side["description"]),
                    )
                else:  # not a Google Photos sidecar: remember, so it is not read again
                    conn.execute("INSERT OR REPLACE INTO hashes(file_id, kind, value) VALUES (?, 'json_none', x'')",
                                 (row["id"],))
            ctx.status.add(disk, "done_files", 1)
    finally:
        conn.close()


def store_archive(conn: sqlite3.Connection, row: sqlite3.Row, members: list[Member], run_id: int,
                  error: str | None) -> None:
    kind = archive_kind(row["name"])
    tp = takeout_part(row["name"])
    takeout_set = f"{row['root_id']}:{row['dir']}/{tp[0]}" if tp else None
    if takeout_set is None and any(m.path.startswith("Takeout/") for m in members[:50]):
        takeout_set = f"{row['root_id']}:{row['dir']}/{row['name']}"  # a single-part Takeout
    now = time.time()
    with db.Tx(conn):
        conn.execute("DELETE FROM files WHERE container_id = ?", (row["id"],))  # a retry after a crash
        _insert_members(conn, row, row["id"], members, run_id)
        conn.execute(
            "INSERT OR REPLACE INTO archives(file_id, kind, takeout_set, part, members, unpacked, indexed_at, error, "
            "version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["id"], kind, takeout_set, tp[1] if tp else None, _count(members),
             sum(m.size for m in members), now, error, ARCHIVE_VERSION),
        )


def _count(members: list[Member]) -> int:
    return sum(1 + len(m.children) for m in members)


def _insert_members(conn, row, container_id: int, members: list[Member], run_id: int) -> None:
    for m in members:
        cur = conn.execute(
            "INSERT OR IGNORE INTO files(root_id, dir, name, container_id, size, mtime_ns, dev, inode, nlink, ext, "
            "last_seen_run, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (row["root_id"], m.dir, m.name, container_id, m.size, m.mtime_ns, row["dev"], row["inode"],
             ext_of(m.name), run_id, m.error),
        )
        if not cur.rowcount:
            continue  # the same path twice in one archive: keep the first
        fid = cur.lastrowid
        if m.hashes and not m.error:
            conn.executemany("INSERT INTO hashes(file_id, kind, value) VALUES (?, ?, ?)",
                             [(fid, k, v) for k, v in m.hashes.items()])
        if m.media is not None:
            md = m.media
            conn.execute(
                "INSERT INTO media(file_id, mime, taken_at, camera, width, height, duration, gps_lat, gps_lon, "
                "orientation, extracted_at, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (fid, md.mime, md.taken_at, md.camera, md.width, md.height, md.duration, md.gps_lat, md.gps_lon,
                 md.orientation, time.time(), md.error),
            )
        if m.sidecar:
            sd = m.sidecar
            conn.execute(
                "INSERT INTO sidecar_json(file_id, title, taken_at, lat, lon, description) VALUES (?,?,?,?,?,?)",
                (fid, sd["title"], sd["taken_at"], sd["lat"], sd["lon"], sd["description"]),
            )
        if m.children:
            _insert_members(conn, row, fid, m.children, run_id)


# -- sidecar pairing ---------------------------------------------------------------


def pair_sidecars(conn: sqlite3.Connection) -> int:
    """Match every parsed sidecar with its photo in the same folder.

    Members of a multi-part Takeout are matched across all its parts, because
    Google often puts a photo and its JSON in different zips.
    """
    from .archives import match_sidecars

    def scope_of(r) -> tuple:
        if r["container_id"] == 0:
            return ("loose", r["root_share"] or r["root_id"], r["dir"])
        return ("arch", r["takeout_set"] or r["top_id"], r["dir"])

    media_exts = METADATA_EXTS
    rows = conn.execute(
        """
        WITH scoped AS (
            SELECT f.id, f.dir, f.name, f.ext, f.container_id, f.root_id, r.share AS root_share,
                   COALESCE(a.file_id, a2.file_id) AS top_id, COALESCE(a.takeout_set, a2.takeout_set) AS takeout_set
            FROM files f JOIN roots r ON r.id = f.root_id
            LEFT JOIN archives a ON a.file_id = f.container_id
            LEFT JOIN files p ON p.id = f.container_id
            LEFT JOIN archives a2 ON a2.file_id = p.container_id
            WHERE f.state = 'present' AND r.enabled = 1
        )
        SELECT s.*, sj.title, sj.file_id IS NOT NULL AS is_json FROM scoped s
        LEFT JOIN sidecar_json sj ON sj.file_id = s.id
        WHERE sj.file_id IS NOT NULL
           OR (s.dir || '/') IN (SELECT DISTINCT f2.dir || '/' FROM sidecar_json j JOIN files f2 ON f2.id = j.file_id)
        """
    ).fetchall()
    names: dict[tuple, dict[str, int]] = defaultdict(dict)
    jsons: dict[tuple, dict[str, tuple[int, dict]]] = defaultdict(dict)
    for r in rows:
        key = scope_of(r)
        if r["is_json"]:
            jsons[key][r["name"]] = (r["id"], {"title": r["title"]})
        elif r["ext"] in media_exts:
            names[key].setdefault(r["name"], r["id"])
    pairs: dict[int, int] = {}
    for key, js in jsons.items():
        if names.get(key):
            pairs.update(match_sidecars(names[key], js))
    with db.Tx(conn):
        conn.execute("DELETE FROM sidecars")
        conn.executemany("INSERT INTO sidecars(file_id, json_file_id) VALUES (?, ?)", list(pairs.items()))
    return len(pairs)


# -- coverage --------------------------------------------------------------------

_LOOSE_HAS = """EXISTS (SELECT 1 FROM hashes h2 JOIN files f2 ON f2.id = h2.file_id JOIN roots r2 ON r2.id = f2.root_id
                        WHERE h2.kind = '{kind}' AND h2.value = {val} AND f2.container_id = 0
                          AND f2.state = 'present' AND r2.enabled = 1)"""
_OTHER_ARCHIVE_HAS = """EXISTS (SELECT 1 FROM hashes h3 JOIN files f3 ON f3.id = h3.file_id
                        WHERE h3.kind = 'blake3' AND h3.value = hb.value AND f3.container_id != 0
                          AND f3.state = 'present' AND f3.id != m.id AND COALESCE(
                            (SELECT p.container_id FROM files p WHERE p.id = f3.container_id AND p.container_id != 0),
                            f3.container_id) != {top})"""

# A loose photo that looks the same (within the similar threshold) and that
# nobody marked as a different photo.
_LOOSE_SIMILAR = """EXISTS (SELECT 1 FROM similar_pairs sp JOIN files f4 ON f4.id = (CASE WHEN sp.a = m.id THEN sp.b ELSE sp.a END)
                    JOIN roots r4 ON r4.id = f4.root_id
                    WHERE (sp.a = m.id OR sp.b = m.id) AND sp.pdist <= {threshold}
                      AND f4.container_id = 0 AND f4.state = 'present' AND r4.enabled = 1
                      AND NOT EXISTS (SELECT 1 FROM reviews rv JOIN hashes x ON x.file_id = m.id AND x.kind = 'img_exact'
                                      JOIN hashes y ON y.file_id = f4.id AND y.kind = 'img_exact'
                                      WHERE rv.decision = 'not_dup'
                                        AND rv.key_a = MIN(x.value, y.value) AND rv.key_b = MAX(x.value, y.value)))"""

MEMBER_STATUS = f"""
    CASE
      WHEN m.error IS NOT NULL OR hb.value IS NULL THEN 'unreadable'
      WHEN {_LOOSE_HAS.format(kind='blake3', val='hb.value')} THEN 'exact'
      WHEN hi.value IS NOT NULL AND {_LOOSE_HAS.format(kind='img_exact', val='hi.value')} THEN 'image'
      WHEN hi.value IS NOT NULL AND {{similar}} THEN 'similar'
      WHEN {_OTHER_ARCHIVE_HAS} THEN 'archive'
      ELSE 'new'
    END
"""


def member_status_sql(top_expr: str, threshold: int | None = None) -> str:
    """SQL for a member's status. ``threshold`` None leaves similar photos out."""
    similar = "0" if threshold is None else _LOOSE_SIMILAR.replace("{threshold}", str(int(threshold)))
    return MEMBER_STATUS.replace("{similar}", similar).replace("{top}", top_expr)


def refresh_coverage(conn: sqlite3.Connection, threshold: int | None = None) -> None:
    """For every archive: how many members are already on disk (exact or same
    photo), only in another archive, or new. Takeout JSON sidecars and
    folders' metadata.json are counted apart."""
    archives = [r[0] for r in conn.execute("SELECT file_id FROM archives")]
    status_sql = member_status_sql(":top", threshold)
    with db.Tx(conn):
        for aid in archives:
            counts = defaultdict(int)
            new_bytes = 0
            for r in conn.execute(
                f"""SELECT m.size, m.ext, sj.file_id IS NOT NULL AS is_sidecar, {status_sql} AS status
                    FROM files m
                    LEFT JOIN hashes hb ON hb.file_id = m.id AND hb.kind = 'blake3'
                    LEFT JOIN hashes hi ON hi.file_id = m.id AND hi.kind = 'img_exact'
                    LEFT JOIN sidecar_json sj ON sj.file_id = m.id
                    WHERE (m.container_id = :top
                           OR m.container_id IN (SELECT id FROM files WHERE container_id = :top))
                      AND NOT EXISTS (SELECT 1 FROM files c WHERE c.container_id = m.id)""",
                {"top": aid},
            ):
                if r["is_sidecar"] or (r["ext"] == "json" and r["size"] < 64 * 1024):
                    continue
                counts[r["status"]] += 1
                if r["status"] == "new":
                    new_bytes += r["size"]
            conn.execute(
                "UPDATE archives SET media_members = ?, cov_exact = ?, cov_image = ?, cov_similar = ?, cov_archive = ?, "
                "cov_new = ?, new_bytes = ? WHERE file_id = ?",
                (sum(counts.values()), counts["exact"], counts["image"], counts["similar"], counts["archive"],
                 counts["new"] + counts["unreadable"], new_bytes, aid),
            )

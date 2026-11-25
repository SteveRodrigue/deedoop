"""Read-only queries behind the dashboard, the duplicates report and exports."""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


def index_stats(conn: sqlite3.Connection, db_path: Path) -> dict:
    row = conn.execute(
        """SELECT COUNT(*) AS files, COALESCE(SUM(f.size), 0) AS bytes
           FROM files f JOIN roots r ON r.id = f.root_id
           WHERE f.state = 'present' AND r.enabled = 1"""
    ).fetchone()
    g = conn.execute(
        "SELECT COUNT(*) AS groups, COALESCE(SUM(wasted), 0) AS wasted, COALESCE(SUM(file_count), 0) AS dup_files, "
        "MAX(built_at) AS built_at FROM groups WHERE method = 'exact'"
    ).fetchone()
    hashed = conn.execute("SELECT COUNT(*) FROM hashes WHERE kind = 'blake3'").fetchone()[0]
    errors = conn.execute("SELECT COUNT(*) FROM files WHERE error IS NOT NULL AND state = 'present'").fetchone()[0]
    size = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            size += os.path.getsize(str(db_path) + suffix)
        except OSError:
            pass
    return {
        "files": row["files"], "bytes": row["bytes"], "groups": g["groups"], "wasted": g["wasted"],
        "dup_files": g["dup_files"], "built_at": g["built_at"], "full_hashed": hashed,
        "errors": errors, "index_bytes": size,
    }


def wasted_by_category(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT category, COUNT(*) AS groups, SUM(wasted) AS wasted FROM groups "
        "WHERE method = 'exact' GROUP BY category ORDER BY wasted DESC"
    ).fetchall()


def per_disk(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT r.disk, COUNT(f.id) AS files, COALESCE(SUM(f.size), 0) AS bytes,
                  MAX(r.last_scan_at) AS last_scan_at, COUNT(DISTINCT r.id) AS roots
           FROM roots r LEFT JOIN files f ON f.root_id = r.id AND f.state = 'present'
           WHERE r.enabled = 1 GROUP BY r.disk"""
    ).fetchall()


def root_counts(conn: sqlite3.Connection) -> dict[int, int]:
    return {
        r[0]: r[1]
        for r in conn.execute("SELECT root_id, COUNT(*) FROM files WHERE state = 'present' GROUP BY root_id")
    }


@dataclass
class GroupFilter:
    category: str = ""
    q: str = ""
    min_size: int = 0
    disk: str = ""
    page: int = 1
    per_page: int = 50

    def where(self) -> tuple[str, list]:
        clauses, args = ["g.method = 'exact'"], []
        if self.category:
            clauses.append("g.category = ?")
            args.append(self.category)
        if self.min_size:
            clauses.append("g.size >= ?")
            args.append(self.min_size)
        if self.q or self.disk:
            sub, sargs = [], []
            if self.q:
                sub.append("(r.user_path || '/' || f.dir || '/' || f.name) LIKE ? ESCAPE '\\'")
                sargs.append("%" + _like_escape(self.q) + "%")
            if self.disk:
                sub.append("r.disk = ?")
                sargs.append(self.disk)
            clauses.append(
                "EXISTS (SELECT 1 FROM group_members m JOIN files f ON f.id = m.file_id "
                "JOIN roots r ON r.id = f.root_id WHERE m.group_id = g.id AND " + " AND ".join(sub) + ")"
            )
            args += sargs
        return " AND ".join(clauses), args


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_groups(conn: sqlite3.Connection, flt: GroupFilter) -> tuple[list[sqlite3.Row], int, int]:
    where, args = flt.where()
    total, wasted = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(wasted), 0) FROM groups g WHERE {where}", args
    ).fetchone()
    rows = conn.execute(
        f"SELECT g.* FROM groups g WHERE {where} ORDER BY g.wasted DESC, g.id LIMIT ? OFFSET ?",
        args + [flt.per_page, (max(flt.page, 1) - 1) * flt.per_page],
    ).fetchall()
    return rows, total, wasted


def group_members(conn: sqlite3.Connection, group_ids: list[int]) -> dict[int, list[dict]]:
    if not group_ids:
        return {}
    marks = ",".join("?" * len(group_ids))
    out: dict[int, list[dict]] = {g: [] for g in group_ids}
    rows = conn.execute(
        f"""SELECT m.group_id, f.id, f.dir, f.name, f.size, f.mtime_ns, f.dev, f.inode, f.nlink,
                   r.disk, r.path AS root_path, r.user_path
            FROM group_members m JOIN files f ON f.id = m.file_id JOIN roots r ON r.id = f.root_id
            WHERE m.group_id IN ({marks}) ORDER BY m.group_id, r.user_path, f.dir, f.name""",
        group_ids,
    ).fetchall()
    for r in rows:
        rel = f"{r['dir']}/{r['name']}" if r["dir"] else r["name"]
        out[r["group_id"]].append({
            "file_id": r["id"],
            "disk": r["disk"],
            "path": f"{r['root_path']}/{rel}",
            "user_path": f"{r['user_path']}/{rel}",
            "mtime": r["mtime_ns"] / 1e9,
            "phys": (r["dev"], r["inode"]),
            "nlink": r["nlink"],
        })
    for members in out.values():
        seen: dict[tuple, int] = {}
        for m in members:
            seen[m["phys"]] = seen.get(m["phys"], 0) + 1
        for m in members:
            m["hardlinked"] = seen[m["phys"]] > 1
    return out


def same_path_on_two_disks(conn: sqlite3.Connection, limit: int = 500) -> list[sqlite3.Row]:
    """Files with the same share-relative path on more than one disk. Unraid
    shows only one of them under /mnt/user."""
    return conn.execute(
        """SELECT r.share, f.dir, f.name, GROUP_CONCAT(r.disk, ', ') AS disks, COUNT(*) AS n,
                  MIN(f.size) AS min_size, MAX(f.size) AS max_size
           FROM files f JOIN roots r ON r.id = f.root_id
           WHERE f.state = 'present' AND r.enabled = 1 AND r.share IS NOT NULL AND f.container_id = 0
           GROUP BY r.share, f.dir, f.name HAVING COUNT(DISTINCT r.disk) > 1
           ORDER BY r.share, f.dir, f.name LIMIT ?""",
        (limit,),
    ).fetchall()


def file_errors(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT r.path AS root_path, f.dir, f.name, f.error FROM files f JOIN roots r ON r.id = f.root_id
           WHERE f.error IS NOT NULL AND f.state = 'present' ORDER BY f.id LIMIT ?""",
        (limit,),
    ).fetchall()


EXPORT_COLUMNS = ["group_id", "method", "blake3", "size", "copies", "wasted", "category",
                  "disk", "user_path", "physical_path", "mtime", "hardlinked"]


def _export_rows(conn: sqlite3.Connection, flt: GroupFilter) -> Iterator[dict]:
    where, args = flt.where()
    import datetime as dt

    ids = [r[0] for r in conn.execute(f"SELECT g.id FROM groups g WHERE {where} ORDER BY g.wasted DESC, g.id", args)]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        groups = {r["id"]: r for r in conn.execute(
            f"SELECT * FROM groups WHERE id IN ({','.join('?' * len(chunk))})", chunk)}
        members = group_members(conn, chunk)
        for gid in chunk:
            g = groups[gid]
            for m in members[gid]:
                yield {
                    "group_id": gid, "method": g["method"], "blake3": g["key"], "size": g["size"],
                    "copies": g["copies"], "wasted": g["wasted"], "category": g["category"],
                    "disk": m["disk"], "user_path": m["user_path"], "physical_path": m["path"],
                    "mtime": dt.datetime.fromtimestamp(m["mtime"]).isoformat(timespec="seconds"),
                    "hardlinked": m["hardlinked"],
                }


def export_csv(conn: sqlite3.Connection, flt: GroupFilter) -> Iterator[str]:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS)
    w.writeheader()
    for row in _export_rows(conn, flt):
        w.writerow(row)
        if buf.tell() > 64 * 1024:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
    yield buf.getvalue()


def export_json(conn: sqlite3.Connection, flt: GroupFilter) -> Iterator[str]:
    """One JSON document: ``{"groups": [{..., "files": [...]}, ...]}``."""
    yield '{"groups": ['
    current, first = None, True
    for row in _export_rows(conn, flt):
        gid = row["group_id"]
        f = {k: row[k] for k in ("disk", "user_path", "physical_path", "mtime", "hardlinked")}
        if current is None or current["group_id"] != gid:
            if current is not None:
                yield ("" if first else ",") + json.dumps(current)
                first = False
            current = {k: row[k] for k in ("group_id", "method", "blake3", "size", "copies", "wasted", "category")}
            current["files"] = []
        current["files"].append(f)
    if current is not None:
        yield ("" if first else ",") + json.dumps(current)
    yield "]}"

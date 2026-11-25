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

from .archives import archive_kind
from .imaging import THUMB_EXTS


def index_stats(conn: sqlite3.Connection, db_path: Path) -> dict:
    row = conn.execute(
        """SELECT COUNT(*) AS files, COALESCE(SUM(f.size), 0) AS bytes
           FROM files f JOIN roots r ON r.id = f.root_id
           WHERE f.state = 'present' AND r.enabled = 1 AND f.container_id = 0"""
    ).fetchone()
    g = conn.execute(
        "SELECT COUNT(*) AS groups, COALESCE(SUM(wasted), 0) AS wasted, COALESCE(SUM(file_count), 0) AS dup_files, "
        "MAX(built_at) AS built_at FROM groups WHERE method = 'exact' AND wasted > 0"
    ).fetchone()
    gi = conn.execute(
        "SELECT COUNT(*) AS groups, COALESCE(SUM(wasted), 0) AS wasted FROM groups WHERE method = 'image' AND wasted > 0"
    ).fetchone()
    total = conn.execute(f"WITH eff AS ({EFFECTIVE_GROUPS}) SELECT COALESCE(SUM(wasted), 0) FROM eff").fetchone()[0]
    media = conn.execute("SELECT COUNT(*), COUNT(taken_at) FROM media").fetchone()
    hashed = conn.execute("SELECT COUNT(*) FROM hashes WHERE kind = 'blake3'").fetchone()[0]
    errors = conn.execute("SELECT COUNT(*) FROM files WHERE error IS NOT NULL AND state = 'present'").fetchone()[0]
    arch = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(a.members), 0), COALESCE(SUM(a.cov_new), 0) FROM archives a "
        "JOIN files f ON f.id = a.file_id WHERE f.state = 'present' AND f.container_id = 0"
    ).fetchone()
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
        "image_groups": gi["groups"], "image_wasted": gi["wasted"], "total_wasted": total,
        "media": media[0], "media_dated": media[1],
        "archives": arch[0], "archive_members": arch[1], "archive_new": arch[2],
    }


# Exact groups that an image group already covers would count the same
# space twice; the "effective" set keeps image groups and the other exact ones.
EFFECTIVE_GROUPS = """
    SELECT g.* FROM groups g
    WHERE g.method = 'image'
       OR (g.method = 'exact' AND NOT EXISTS (
            SELECT 1 FROM group_members m
            JOIN group_members m2 ON m2.file_id = m.file_id
            JOIN groups gi ON gi.id = m2.group_id AND gi.method = 'image'
            WHERE m.group_id = g.id))
"""


def wasted_by_category(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        f"WITH eff AS ({EFFECTIVE_GROUPS}) SELECT category, COUNT(*) AS groups, SUM(wasted) AS wasted FROM eff "
        "GROUP BY category ORDER BY wasted DESC"
    ).fetchall()


def per_disk(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT r.disk, COUNT(f.id) AS files, COALESCE(SUM(f.size), 0) AS bytes,
                  MAX(r.last_scan_at) AS last_scan_at, COUNT(DISTINCT r.id) AS roots
           FROM roots r LEFT JOIN files f ON f.root_id = r.id AND f.state = 'present' AND f.container_id = 0
           WHERE r.enabled = 1 GROUP BY r.disk"""
    ).fetchall()


def root_counts(conn: sqlite3.Connection) -> dict[int, int]:
    return {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT root_id, COUNT(*) FROM files WHERE state = 'present' AND container_id = 0 GROUP BY root_id")
    }


METHODS = {"exact": "Exact copies", "image": "Same photo, different metadata"}

# Archive members are shown as "archive path › member path". Members are
# nested one level at most, so two joins reach the top-level archive.
CONTAINER_JOINS = """LEFT JOIN files c1 ON c1.id = f.container_id
                     LEFT JOIN files c2 ON c2.id = c1.container_id"""
CONTAINER_COLS = "f.container_id, c1.dir AS c1_dir, c1.name AS c1_name, c2.dir AS c2_dir, c2.name AS c2_name"
SEP = " › "


def _join(d: str, n: str) -> str:
    return f"{d}/{n}" if d else n


def rel_path(r) -> str:
    """Root-relative path of a file; for an archive member, the archive's
    path and the path inside it."""
    rel = _join(r["dir"], r["name"])
    if not r["container_id"]:
        return rel
    inner = _join(r["c1_dir"], r["c1_name"])
    if r["c2_name"] is not None:
        return _join(r["c2_dir"], r["c2_name"]) + SEP + inner + SEP + rel
    return inner + SEP + rel


def can_thumb(r) -> bool:
    if r["ext"] not in THUMB_EXTS:
        return False
    if not r["container_id"]:
        return True
    # Only members of a top-level zip can be read on their own.
    return r["c2_name"] is None and archive_kind(r["c1_name"] or "") == "zip"


@dataclass
class GroupFilter:
    method: str = "exact"
    category: str = ""
    q: str = ""
    min_size: int = 0
    disk: str = ""
    group: int = 0
    archives: bool = False  # include groups whose extra copies are all inside archives
    page: int = 1
    per_page: int = 50

    def where(self) -> tuple[str, list]:
        clauses, args = ["g.method = ?"], [self.method if self.method in METHODS else "exact"]
        if self.group:
            clauses.append("g.id = ?")
            args.append(self.group)
        elif not self.archives:
            clauses.append("g.wasted > 0")
        if self.category:
            clauses.append("g.category = ?")
            args.append(self.category)
        if self.min_size:
            clauses.append("g.size >= ?")
            args.append(self.min_size)
        if self.q or self.disk:
            sub, sargs = [], []
            if self.q:
                sub.append("(r.user_path || '/' || COALESCE(c1.dir || '/' || c1.name || '/', '') || f.dir || '/' "
                           "|| f.name) LIKE ? ESCAPE '\\'")
                sargs.append("%" + _like_escape(self.q) + "%")
            if self.disk:
                sub.append("r.disk = ?")
                sargs.append(self.disk)
            clauses.append(
                "EXISTS (SELECT 1 FROM group_members m JOIN files f ON f.id = m.file_id "
                "JOIN roots r ON r.id = f.root_id LEFT JOIN files c1 ON c1.id = f.container_id WHERE m.group_id = g.id AND " + " AND ".join(sub) + ")"
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
        f"""SELECT m.group_id, f.id, f.dir, f.name, f.size, f.mtime_ns, f.dev, f.inode, f.nlink, f.ext,
                   r.disk, r.path AS root_path, r.user_path,
                   md.taken_at, md.camera, md.width, md.height, md.gps_lat, md.gps_lon, {CONTAINER_COLS},
                   (SELECT value FROM hashes h WHERE h.file_id = f.id AND h.kind = 'blake3') AS full
            FROM group_members m JOIN files f ON f.id = m.file_id JOIN roots r ON r.id = f.root_id
            LEFT JOIN media md ON md.file_id = f.id {CONTAINER_JOINS}
            WHERE m.group_id IN ({marks})
            ORDER BY m.group_id, f.container_id != 0, f.size DESC, r.user_path, f.dir, f.name""",
        group_ids,
    ).fetchall()
    for r in rows:
        rel = rel_path(r)
        member = bool(r["container_id"])
        out[r["group_id"]].append({
            "file_id": r["id"],
            "disk": r["disk"],
            "path": f"{r['root_path']}/{rel}",
            "user_path": f"{r['user_path']}/{rel}",
            "mtime": r["mtime_ns"] / 1e9,
            "phys": ("m", r["id"]) if member else (r["dev"], r["inode"]),
            "in_archive": member,
            "nlink": r["nlink"],
            "size": r["size"],
            "ext": r["ext"],
            "thumb": can_thumb(r),
            "taken_at": r["taken_at"],
            "camera": r["camera"],
            "dims": f"{r['width']}×{r['height']}" if r["width"] and r["height"] else None,
            "gps": f"{r['gps_lat']:.5f}, {r['gps_lon']:.5f}" if r["gps_lat"] is not None else None,
            "full": r["full"].hex()[:12] if r["full"] else "",
        })
    for members in out.values():
        seen: dict[tuple, int] = {}
        for m in members:
            seen[m["phys"]] = seen.get(m["phys"], 0) + 1
        for m in members:
            m["hardlinked"] = seen[m["phys"]] > 1
        # Fields that differ between members, for the metadata diff.
        diff = {k for k in DIFF_FIELDS if len({m[k] for m in members}) > 1}
        for m in members:
            m["differs"] = diff
    return out


DIFF_FIELDS = ("size", "taken_at", "camera", "dims", "gps", "full")


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


EXPORT_COLUMNS = ["group_id", "method", "key", "size", "copies", "wasted", "category",
                  "disk", "user_path", "physical_path", "file_size", "mtime", "hardlinked", "in_archive", "taken_at", "camera"]


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
                    "group_id": gid, "method": g["method"], "key": g["key"], "size": g["size"],
                    "copies": g["copies"], "wasted": g["wasted"], "category": g["category"],
                    "disk": m["disk"], "user_path": m["user_path"], "physical_path": m["path"],
                    "mtime": dt.datetime.fromtimestamp(m["mtime"]).isoformat(timespec="seconds"),
                    "hardlinked": m["hardlinked"], "in_archive": m["in_archive"], "file_size": m["size"],
                    "taken_at": m["taken_at"], "camera": m["camera"],
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
        f = {k: row[k] for k in ("disk", "user_path", "physical_path", "file_size", "mtime", "hardlinked",
                                 "in_archive", "taken_at", "camera")}
        if current is None or current["group_id"] != gid:
            if current is not None:
                yield ("" if first else ",") + json.dumps(current)
                first = False
            current = {k: row[k] for k in ("group_id", "method", "key", "size", "copies", "wasted", "category")}
            current["files"] = []
        current["files"].append(f)
    if current is not None:
        yield ("" if first else ",") + json.dumps(current)
    yield "]}"


# -- browse -------------------------------------------------------------------


@dataclass
class BrowseFilter:
    loc: str = ""  # "share:<name>" or "root:<id>"
    dir: str = ""
    q: str = ""
    kind: str = ""
    year: str = ""
    camera: str = ""
    no_gps: bool = False
    no_date: bool = False
    dups: bool = False
    page: int = 1
    per_page: int = 100

    @property
    def flat(self) -> bool:
        """Any filter turns the folder view into a search over the folder's subtree."""
        return bool(self.q or self.kind or self.year or self.camera or self.no_gps or self.no_date or self.dups)


def locations(conn: sqlite3.Connection) -> list[dict]:
    out, seen = [], set()
    for r in conn.execute("SELECT * FROM roots WHERE enabled = 1 ORDER BY source DESC, share, path"):
        if r["share"]:
            if r["share"] in seen:
                continue
            seen.add(r["share"])
            out.append({"key": f"share:{r['share']}", "label": r["share"], "user_path": r["user_path"]})
        else:
            out.append({"key": f"root:{r['id']}", "label": r["path"], "user_path": r["user_path"]})
    return out


def loc_roots(conn: sqlite3.Connection, loc: str) -> tuple[list[int], str]:
    kind, _, val = loc.partition(":")
    if kind == "share":
        rows = conn.execute("SELECT id, user_path FROM roots WHERE enabled = 1 AND share = ?", (val,)).fetchall()
    elif kind == "root" and val.isdigit():
        rows = conn.execute("SELECT id, user_path FROM roots WHERE enabled = 1 AND id = ?", (int(val),)).fetchall()
    else:
        rows = []
    return [r["id"] for r in rows], (rows[0]["user_path"] if rows else "")


def _subtree(d: str) -> tuple[str, list]:
    """SQL matching ``f.dir`` equal to ``d`` or below it, using the index range."""
    if not d:
        return "1", []
    prefix = d + "/"
    return "(f.dir = ? OR (f.dir >= ? AND f.dir < ?))", [d, prefix, d + "0"]  # '0' sorts right after '/'


FILE_COLUMNS = """
    f.id, f.dir, f.name, f.size, f.mtime_ns, f.ext, r.disk, r.user_path, r.path AS root_path, {CONTAINER_COLS},
    md.taken_at, md.camera, md.width, md.height, md.duration, md.gps_lat, md.gps_lon,
    EXISTS (SELECT 1 FROM group_members gm WHERE gm.file_id = f.id) AS in_group
""".replace("{CONTAINER_COLS}", CONTAINER_COLS)


def browse(conn: sqlite3.Connection, flt: BrowseFilter) -> dict:
    from .categories import exts_for

    roots, base = loc_roots(conn, flt.loc)
    if not roots:
        return {"subdirs": [], "files": [], "total": 0, "base": ""}
    rmarks = ",".join("?" * len(roots))
    subdirs: list[str] = []
    if not flt.flat:
        prefix = flt.dir + "/" if flt.dir else ""
        n = len(prefix) + 1
        rng, rargs = ("f.dir >= ? AND f.dir < ?", [prefix, flt.dir + "0"]) if flt.dir else ("f.dir != ''", [])
        subdirs = sorted(
            {r[0] for r in conn.execute(
                f"""SELECT DISTINCT CASE WHEN instr(substr(f.dir, {n}), '/') > 0
                          THEN substr(substr(f.dir, {n}), 1, instr(substr(f.dir, {n}), '/') - 1)
                          ELSE substr(f.dir, {n}) END
                    FROM files f WHERE f.root_id IN ({rmarks}) AND f.state = 'present' AND f.container_id = 0
                      AND {rng}""",
                roots + rargs)},
            key=str.lower,
        )
        where, args = [f"f.root_id IN ({rmarks})", "f.state = 'present'", "f.container_id = 0", "f.dir = ?"], \
            roots + [flt.dir]
    else:
        sub, sargs = _subtree(flt.dir)
        where, args = [f"f.root_id IN ({rmarks})", "f.state = 'present'", "f.container_id = 0", sub], roots + sargs
        if flt.q:
            where.append("f.name LIKE ? ESCAPE '\\'")
            args.append("%" + _like_escape(flt.q) + "%")
        if flt.kind:
            exts = exts_for(flt.kind)
            if flt.kind == "other":
                from .categories import EXT_TO_CAT

                exts = sorted(EXT_TO_CAT)
                where.append(f"f.ext NOT IN ({','.join('?' * len(exts))})")
            else:
                where.append(f"f.ext IN ({','.join('?' * len(exts))})")
            args += exts
        if flt.year:
            where.append("substr(md.taken_at, 1, 4) = ?")
            args.append(flt.year)
        if flt.camera:
            where.append("md.camera = ?")
            args.append(flt.camera)
        if flt.no_gps:
            where.append("md.file_id IS NOT NULL AND md.gps_lat IS NULL")
        if flt.no_date:
            where.append("md.file_id IS NOT NULL AND md.taken_at IS NULL")
        if flt.dups:
            where.append("EXISTS (SELECT 1 FROM group_members gm WHERE gm.file_id = f.id)")
    w = " AND ".join(where)
    joins = f"FROM files f JOIN roots r ON r.id = f.root_id LEFT JOIN media md ON md.file_id = f.id {CONTAINER_JOINS}"
    total = conn.execute(f"SELECT COUNT(*) {joins} WHERE {w}", args).fetchone()[0]
    order = "md.taken_at IS NULL, md.taken_at, f.dir, f.name" if flt.year or flt.camera else "f.dir, f.name"
    files = conn.execute(
        f"SELECT {FILE_COLUMNS} {joins} WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?",
        args + [flt.per_page, (max(flt.page, 1) - 1) * flt.per_page],
    ).fetchall()
    return {"subdirs": subdirs, "files": [_file_view(r) for r in files], "total": total, "base": base}


def _file_view(r: sqlite3.Row) -> dict:
    rel = rel_path(r)
    return {
        "id": r["id"], "name": r["name"], "dir": r["dir"], "size": r["size"], "mtime": r["mtime_ns"] / 1e9,
        "disk": r["disk"], "user_path": f"{r['user_path']}/{rel}", "path": f"{r['root_path']}/{rel}",
        "thumb": can_thumb(r), "container_id": r["container_id"], "taken_at": r["taken_at"], "camera": r["camera"],
        "dims": f"{r['width']}×{r['height']}" if r["width"] and r["height"] else None,
        "duration": r["duration"], "gps": r["gps_lat"] is not None, "in_group": bool(r["in_group"]),
    }


def facets(conn: sqlite3.Connection) -> dict:
    years = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(taken_at, 1, 4) FROM media WHERE taken_at IS NOT NULL ORDER BY 1 DESC")]
    cameras = [r[0] for r in conn.execute(
        "SELECT camera FROM media WHERE camera IS NOT NULL GROUP BY camera ORDER BY COUNT(*) DESC LIMIT 200")]
    return {"years": years, "cameras": cameras}


def file_detail(conn: sqlite3.Connection, file_id: int) -> dict | None:
    r = conn.execute(
        f"SELECT {FILE_COLUMNS}, f.inode, f.nlink, f.error, md.mime, md.orientation "
        f"FROM files f JOIN roots r ON r.id = f.root_id LEFT JOIN media md ON md.file_id = f.id {CONTAINER_JOINS} "
        "WHERE f.id = ?",
        (file_id,),
    ).fetchone()
    if r is None:
        return None
    v = _file_view(r)
    v.update({"inode": r["inode"], "nlink": r["nlink"], "error": r["error"], "mime": r["mime"],
              "orientation": r["orientation"], "gps_lat": r["gps_lat"], "gps_lon": r["gps_lon"]})
    v["hashes"] = {h["kind"]: (h["value"].hex() if h["kind"] != "img_none" else h["value"].decode(errors="replace"))
                   for h in conn.execute("SELECT kind, value FROM hashes WHERE file_id = ?", (file_id,))}
    v["groups"] = [dict(g) for g in conn.execute(
        "SELECT g.id, g.method, g.copies, g.wasted FROM group_members m JOIN groups g ON g.id = m.group_id "
        "WHERE m.file_id = ?", (file_id,))]
    # Archive context: the archive a member lives in, or what an archive holds.
    v["container"] = None
    if r["container_id"]:
        top = conn.execute("SELECT id, container_id FROM files WHERE id = ?", (r["container_id"],)).fetchone()
        top_id = top["container_id"] or top["id"]
        v["container"] = {"id": r["container_id"], "top_id": top_id,
                          "path": f"{r['user_path']}/{_join(r['c1_dir'], r['c1_name'])}"}
    a = conn.execute("SELECT * FROM archives WHERE file_id = ?", (file_id,)).fetchone()
    v["archive"] = dict(a) if a else None
    sc = conn.execute(
        "SELECT j.*, s.json_file_id FROM sidecars s JOIN sidecar_json j ON j.file_id = s.json_file_id "
        "WHERE s.file_id = ?", (file_id,)).fetchone()
    v["sidecar"] = dict(sc) if sc else None
    return v


# -- archives -------------------------------------------------------------------

STATUSES = {"exact": "Already on disk", "image": "Same photo on disk", "archive": "Only in another archive",
            "new": "Not on disk yet", "unreadable": "Could not be read"}


def _set_label(row) -> str:
    from .archives import takeout_part

    tp = takeout_part(row["name"])
    return row["name"][:len(tp[0])] if tp else row["name"]  # the set's name as written


def archive_list(conn: sqlite3.Connection) -> list[dict]:
    """Archives on disk, with the parts of a multi-part Takeout folded into one entry."""
    rows = conn.execute(
        """SELECT a.*, f.dir, f.name, f.size, r.disk, r.user_path
           FROM archives a JOIN files f ON f.id = a.file_id JOIN roots r ON r.id = f.root_id
           WHERE f.state = 'present' AND f.container_id = 0 AND r.enabled = 1
           ORDER BY r.user_path, f.dir, f.name"""
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        key = r["takeout_set"] or f"file:{r['file_id']}"
        e = out.get(key)
        if e is None:
            e = out[key] = {
                "id": r["file_id"], "label": _set_label(r) if r["takeout_set"] else r["name"],
                "takeout": bool(r["takeout_set"]), "kind": r["kind"], "folder": r["user_path"] + ("/" + r["dir"] if r["dir"] else ""),
                "disks": [], "parts": 0, "size": 0, "members": 0, "exact": 0, "image": 0, "archive": 0,
                "new": 0, "new_bytes": 0, "errors": 0, "indexed": True,
            }
        e["parts"] += 1
        e["size"] += r["size"]
        if r["disk"] not in e["disks"]:
            e["disks"].append(r["disk"])
        for k_out, k_in in (("members", "media_members"), ("exact", "cov_exact"), ("image", "cov_image"),
                            ("archive", "cov_archive"), ("new", "cov_new"), ("new_bytes", "new_bytes")):
            e[k_out] += r[k_in] or 0
        e["errors"] += 1 if r["error"] else 0
        e["indexed"] = e["indexed"] and r["indexed_at"] is not None
    for e in out.values():
        e["covered"] = (e["exact"] + e["image"]) / e["members"] if e["members"] else 0.0
    return sorted(out.values(), key=lambda e: (-e["new_bytes"], e["label"].lower()))


def archive_parts(conn: sqlite3.Connection, file_id: int) -> list[sqlite3.Row]:
    a = conn.execute("SELECT takeout_set FROM archives WHERE file_id = ?", (file_id,)).fetchone()
    if a is None:
        return []
    where, arg = ("a.takeout_set = ?", a["takeout_set"]) if a["takeout_set"] else ("a.file_id = ?", file_id)
    return conn.execute(
        f"""SELECT a.*, f.dir, f.name, f.size, r.disk, r.user_path, r.path AS root_path
            FROM archives a JOIN files f ON f.id = a.file_id JOIN roots r ON r.id = f.root_id
            WHERE {where} AND f.state = 'present' ORDER BY a.part, f.name""",
        (arg,),
    ).fetchall()


def archive_members(conn: sqlite3.Connection, parts: list[sqlite3.Row]) -> list[dict]:
    """Every member of the given archives with its status, skipping Takeout
    JSON sidecars and nested archives (their own members are listed)."""
    from .archive_index import member_status_sql

    sql = member_status_sql(":top")
    out = []
    for p in parts:
        for r in conn.execute(
            f"""SELECT m.id, m.dir, m.name, m.size, m.ext, m.container_id, m.error,
                       md.taken_at, sj2.taken_at AS json_taken, c1.dir AS c1_dir, c1.name AS c1_name,
                       c2.dir AS c2_dir, c2.name AS c2_name, {sql} AS status
                FROM files m
                LEFT JOIN hashes hb ON hb.file_id = m.id AND hb.kind = 'blake3'
                LEFT JOIN hashes hi ON hi.file_id = m.id AND hi.kind = 'img_exact'
                LEFT JOIN sidecar_json sj ON sj.file_id = m.id
                LEFT JOIN media md ON md.file_id = m.id
                LEFT JOIN sidecars sc ON sc.file_id = m.id
                LEFT JOIN sidecar_json sj2 ON sj2.file_id = sc.json_file_id
                LEFT JOIN files c1 ON c1.id = m.container_id
                LEFT JOIN files c2 ON c2.id = c1.container_id
                WHERE (m.container_id = :top OR m.container_id IN (SELECT id FROM files WHERE container_id = :top))
                  AND sj.file_id IS NULL AND NOT (m.ext = 'json' AND m.size < 65536)
                  AND NOT EXISTS (SELECT 1 FROM files c WHERE c.container_id = m.id)
                ORDER BY m.dir, m.name""",
            {"top": p["file_id"]},
        ):
            nested = r["c2_name"] is not None
            out.append({
                "id": r["id"], "dir": (_join(r["c1_dir"], r["c1_name"]) + SEP + r["dir"]) if nested else r["dir"],
                "name": r["name"], "size": r["size"], "status": r["status"], "part": p["name"],
                "taken_at": r["taken_at"] or r["json_taken"],
                "thumb": r["ext"] in THUMB_EXTS and not nested and p["kind"] == "zip",
            })
    return out


def album_of(d: str) -> str:
    """Google Takeout keeps albums at Takeout/Google Photos/<album>; elsewhere
    the folder itself is the album."""
    parts = d.split("/")
    if len(parts) >= 3 and parts[0] == "Takeout" and parts[1] == "Google Photos":
        return parts[2]
    return d or "(top folder)"


def albums(members: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for m in members:
        a = album_of(m["dir"])
        e = out.setdefault(a, {"name": a, "members": 0, "new_bytes": 0, **{k: 0 for k in STATUSES}})
        e["members"] += 1
        e[m["status"]] += 1
        if m["status"] in ("new", "unreadable"):
            e["new_bytes"] += m["size"]
    for e in out.values():
        e["covered"] = (e["exact"] + e["image"]) / e["members"] if e["members"] else 0.0
    return sorted(out.values(), key=lambda e: (e["covered"], e["name"].lower()))

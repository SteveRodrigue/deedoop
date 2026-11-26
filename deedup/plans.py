"""Reorganisation plans: where each file would go under a move rule.

Milestone 5 only plans and previews. Nothing here writes to /mnt; a saved
plan is a list of (source, destination) rows in the index that milestone 6
can apply after you confirm it.

A rule is a path template whose first part is the destination share, such as
``Photos/{year}/{month}/{name}``. Tokens:

    {name} {stem} {ext}           the file name, without and only its extension
    {year} {month} {day} {date}   from the date taken (EXIF, or the Takeout JSON)
    {camera}                      "Canon EOS 80D", or "Unknown camera"
    {folder} {parent}             the current folder inside the share, and its last part
    {share}                       the current share

On Unraid a file stays on the disk it is on: a move from
``/mnt/disk2/Backups/x.jpg`` to the Photos share goes to ``/mnt/disk2/Photos/...``,
a rename that copies nothing and does not fill another disk. A file outside
the shares (an unassigned device, for example) would have to be copied, and
is marked so.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import db
from .queries import can_thumb

TOKENS = {
    "name": "file name with extension",
    "stem": "file name without extension",
    "ext": "extension, lower case",
    "year": "year taken, e.g. 2019",
    "month": "month taken, 01 to 12",
    "day": "day taken, 01 to 31",
    "date": "date taken, e.g. 2019-07-14",
    "camera": "camera make and model",
    "folder": "current folder inside the share",
    "parent": "last part of the current folder",
    "share": "current share",
}
DATE_TOKENS = {"year", "month", "day", "date"}
DEFAULT_TEMPLATE = "{share}/{year}/{month}/{name}"
PRESETS = [
    ("Same share, by year and month", DEFAULT_TEMPLATE),
    ("By year and month", "Photos/{year}/{month}/{name}"),
    ("By date", "Photos/{year}/{date}/{name}"),
    ("By year and camera", "Photos/{year}/{camera}/{name}"),
    ("Same share, by year", "{share}/{year}/{folder}/{name}"),
]
FALLBACKS = {
    "mtime": "use the file's modified date",
    "undated": "put them in an “Undated” folder",
    "skip": "leave them where they are",
}
CONFLICTS = {
    "rename": "add a number: “name (1).jpg”",
    "skip": "leave the file where it is",
}
STATUS = {
    "move": "Will move",
    "copy": "Will copy (other device)",
    "renamed": "Will move, renamed",
    "in_place": "Already in place",
    "duplicate": "Already there (same content)",
    "conflict": "Conflict",
    "skipped": "Skipped",
}
_TOKEN_RE = re.compile(r"\{([a-z_]+)\}")
_BAD = re.compile(r'[\x00-\x1f/\\]')


class RuleError(ValueError):
    pass


@dataclass
class Rule:
    template: str
    fallback: str = "undated"
    conflicts: str = "rename"

    def check(self, shares: set[str]) -> tuple[str, list[str]]:
        """The destination share and the template's parts after it."""
        t = self.template.strip()
        for prefix in ("/mnt/user/", "/mnt/user0/"):
            if t.startswith(prefix):
                t = t[len(prefix):]
        parts = [p for p in t.strip("/").split("/") if p]
        if len(parts) < 2:
            raise RuleError("A rule needs a share and a file name, for example Photos/{year}/{name}")
        for tok in _TOKEN_RE.findall(t):
            if tok not in TOKENS:
                raise RuleError(f"Unknown token {{{tok}}}. Tokens: " + ", ".join(f"{{{k}}}" for k in TOKENS))
        if "{" in parts[-1] and not _TOKEN_RE.search(parts[-1]):
            raise RuleError("Unbalanced braces in the file name part")
        if any(p in (".", "..") for p in parts):
            raise RuleError("“.” and “..” are not allowed in a rule")
        share = parts[0]
        if share != "{share}" and share not in shares:
            raise RuleError(f"There is no share called {share}. Shares: " + ", ".join(sorted(shares)))
        if self.fallback not in FALLBACKS or self.conflicts not in CONFLICTS:
            raise RuleError("Unknown option")
        return share, parts[1:]


@dataclass
class Item:
    file_id: int
    src_user: str
    src_path: str
    size: int
    mtime_ns: int
    status: str
    dst_user: str = ""
    dst_path: str = ""
    note: str = ""
    thumb: bool = False
    taken_at: str | None = None
    extra: dict = field(default_factory=dict)


def _clean(v: str) -> str:
    v = _BAD.sub("_", str(v)).strip().strip(".")
    return v[:120] or "_"


def _date_of(r) -> tuple[dt.date | None, str]:
    for key, label in (("taken_at", "date taken"), ("json_taken", "Takeout date")):
        v = r[key]
        if v:
            try:
                return dt.date.fromisoformat(v[:10]), label
            except ValueError:
                continue
    return None, ""


def render(parts: list[str], share: str, r, fallback: str) -> tuple[list[str] | None, str]:
    """The destination parts for one file, or None and why it is skipped."""
    name = r["name"]
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    d, _src = _date_of(r)
    note = ""
    needs_date = any(t in DATE_TOKENS for p in parts for t in _TOKEN_RE.findall(p))
    if d is None and needs_date:
        if fallback == "skip":
            return None, "no date taken"
        if fallback == "mtime":
            d = dt.datetime.fromtimestamp(r["mtime_ns"] / 1e9).date()
            note = "date from the file's modified time"
    values = {
        "name": name, "stem": stem, "ext": ext.lower(),
        "year": f"{d.year:04d}" if d else "Undated",
        "month": f"{d.month:02d}" if d else "Undated",
        "day": f"{d.day:02d}" if d else "Undated",
        "date": d.isoformat() if d else "Undated",
        "camera": r["camera"] or "Unknown camera",
        "share": r["share"] or "",
        "parent": r["dir"].rsplit("/", 1)[-1] if r["dir"] else "",
    }
    out = []
    undated = False
    for p in parts:
        if d is None and _TOKEN_RE.search(p) and set(_TOKEN_RE.findall(p)) <= DATE_TOKENS:
            if undated:
                continue  # one "Undated" folder, not Undated/Undated
            undated = True
        if p == "{folder}":
            out.extend(_clean(x) for x in r["dir"].split("/") if x)  # keeps the subfolders
            continue
        v = _TOKEN_RE.sub(lambda m: _clean(values[m.group(1)]) if values[m.group(1)] else "", p)
        if v:
            out.append(v)
    if not out:
        return None, "the rule gives an empty path"
    if d is None and needs_date:
        note = "no date taken"
    return out, note


def _numbered(name: str, n: int) -> str:
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        return f"{name} ({n})"
    return f"{stem} ({n}).{ext}"


def preview(conn: sqlite3.Connection, mnt: Path, rows: list[sqlite3.Row], rule: Rule) -> list[Item]:
    """Where every selected file would go, and what stands in the way."""
    from .discovery import list_disks, list_shares

    disks = {d.name: d for d in list_disks(mnt)}
    shares = {s.name: s for s in list_shares(mnt, list(disks.values()))}
    share_tmpl, parts = rule.check(set(shares))

    # Files already in the index, by (share, path inside the share): every
    # disk counts, because /mnt/user shows only one of two same-path files.
    def occupant(share: str, rel: str) -> sqlite3.Row | None:
        d, _, n = rel.rpartition("/")
        return conn.execute(
            """SELECT f.id, f.size, (SELECT value FROM hashes h WHERE h.file_id = f.id AND h.kind = 'blake3') AS b3
               FROM files f JOIN roots r ON r.id = f.root_id
               WHERE r.share = ? AND f.dir = ? AND f.name = ? AND f.container_id = 0 AND f.state = 'present'
               LIMIT 1""",
            (share, d, n),
        ).fetchone()

    b3 = {}
    if rows:
        ids = [r["id"] for r in rows]
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            for h in conn.execute(
                f"SELECT file_id, value FROM hashes WHERE kind = 'blake3' AND file_id IN ({','.join('?' * len(chunk))})",
                chunk,
            ):
                b3[h[0]] = h[1]

    items: list[Item] = []
    claimed: dict[tuple[str, str], int] = {}  # destination -> file id that takes it
    for r in rows:
        rel_src = f"{r['dir']}/{r['name']}" if r["dir"] else r["name"]
        it = Item(r["id"], f"{r['user_path']}/{rel_src}", f"{r['root_path']}/{rel_src}", r["size"],
                  r["mtime_ns"], "skipped", thumb=can_thumb(r), taken_at=r["taken_at"] or r["json_taken"])
        items.append(it)
        share = r["share"] if share_tmpl == "{share}" else share_tmpl
        if not share or share not in shares:
            it.note = "not in a share" if not r["share"] else f"no share {share}"
            continue
        out, note = render(parts, share, r, rule.fallback)
        if out is None:
            it.note = note
            continue
        it.note = note
        rel = "/".join(out)
        user_root = str(shares[share].user_path)
        same_disk = r["share"] is not None and r["disk"] in disks
        op = "move" if same_disk else "copy"
        disk_root = str(disks[r["disk"]].path / share) if same_disk else user_root

        if r["share"] == share and rel == rel_src:
            it.status, it.dst_user, it.dst_path = "in_place", it.src_user, it.src_path
            claimed[(share, rel)] = r["id"]
            continue
        n = 0
        cand = rel
        while True:
            taken_by = claimed.get((share, cand))
            occ = None if taken_by else occupant(share, cand)
            if occ is not None and occ["id"] == r["id"]:
                occ = None
            if taken_by is None and occ is None and not os.path.lexists(os.path.join(disk_root, cand)) \
                    and not os.path.lexists(os.path.join(user_root, cand)):  # not indexed, e.g. excluded
                break
            other_b3 = b3.get(taken_by) if taken_by else (occ["b3"] if occ is not None else None)
            mine = b3.get(r["id"])
            if mine is not None and other_b3 == mine:
                it.status = "duplicate"
                it.dst_user, it.dst_path = f"{user_root}/{cand}", os.path.join(disk_root, cand)
                it.note = "an identical copy is already there"
                break
            if rule.conflicts == "skip":
                it.status = "conflict"
                it.dst_user, it.dst_path = f"{user_root}/{cand}", os.path.join(disk_root, cand)
                it.note = "another file " + ("in this plan goes there" if taken_by else "is already there")
                break
            n += 1
            d, _, base = rel.rpartition("/")
            cand = (d + "/" if d else "") + _numbered(base, n)
            if n > 999:
                it.status, it.note = "conflict", "too many files with this name"
                break
        if it.status in ("duplicate", "conflict"):
            continue
        claimed[(share, cand)] = r["id"]
        it.status = "renamed" if n else op
        if n and op == "copy":
            it.note = (it.note + "; " if it.note else "") + "copied from another device"
        elif op == "copy":
            it.note = it.note or "on another device: copied, not renamed"
        it.dst_user, it.dst_path = f"{user_root}/{cand}", os.path.join(disk_root, cand)
    return items


def _share_folder(dst_user: str) -> str:
    """/mnt/user/Photos/2019/07/a.jpg -> Photos/2019/07"""
    return dst_user.partition("/user/")[2].rpartition("/")[0]


def summary(items: list[Item]) -> dict:
    c = Counter(i.status for i in items)
    moving = [i for i in items if i.status in ("move", "copy", "renamed")]
    folders = Counter()
    for i in moving:
        folders[_share_folder(i.dst_user)] += 1
    return {"counts": {k: c.get(k, 0) for k in STATUS}, "total": len(items), "moving": len(moving),
            "bytes": sum(i.size for i in moving), "folders": folders.most_common(40),
            "folder_count": len(folders)}


# -- saved plans -------------------------------------------------------------------


def save(conn: sqlite3.Connection, name: str, rule: Rule, source: dict, items: list[Item]) -> int:
    s = summary(items)
    with db.Tx(conn):
        cur = conn.execute(
            "INSERT INTO plans(name, kind, rule, source, counts, created_at, checked_at) VALUES (?, 'move', ?, ?, ?, ?, ?)",
            (name.strip()[:120] or rule.template, json.dumps(asdict(rule)), json.dumps(source),
             json.dumps(s["counts"]), time.time(), time.time()),
        )
        pid = cur.lastrowid
        conn.executemany(
            "INSERT INTO plan_items(plan_id, file_id, src_path, src_user, dst_path, dst_user, size, mtime_ns, status, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(pid, i.file_id, i.src_path, i.src_user, i.dst_path, i.dst_user, i.size, i.mtime_ns, i.status, i.note)
             for i in items],
        )
    return pid


def recheck(conn: sqlite3.Connection, plan_id: int) -> dict:
    """Mark items whose file changed or disappeared since the plan was made."""
    rows = conn.execute(
        """SELECT pi.file_id, pi.size, pi.mtime_ns, f.size AS fsize, f.mtime_ns AS fmtime, f.state
           FROM plan_items pi LEFT JOIN files f ON f.id = pi.file_id WHERE pi.plan_id = ?""",
        (plan_id,),
    ).fetchall()
    stale = [(r["file_id"],) for r in rows
             if r["state"] != "present" or (r["fsize"], r["fmtime"]) != (r["size"], r["mtime_ns"])]
    with db.Tx(conn):
        conn.execute("UPDATE plan_items SET stale = 0 WHERE plan_id = ?", (plan_id,))
        conn.executemany(f"UPDATE plan_items SET stale = 1 WHERE plan_id = {int(plan_id)} AND file_id = ?", stale)
        conn.execute("UPDATE plans SET checked_at = ? WHERE id = ?", (time.time(), plan_id))
    return {"items": len(rows), "stale": len(stale)}


def _plan_row(r) -> dict:
    p = dict(r)
    p["rule"] = json.loads(p["rule"])
    p["source"] = json.loads(p["source"])
    p["counts"] = json.loads(p["counts"])
    p["total"] = sum(p["counts"].values())
    p["moving"] = sum(p["counts"].get(k, 0) for k in ("move", "copy", "renamed"))
    return p


def list_plans(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT p.*, (SELECT COALESCE(SUM(size), 0) FROM plan_items i WHERE i.plan_id = p.id
                        AND i.status IN ('move', 'copy', 'renamed')) AS bytes,
                  (SELECT COUNT(*) FROM plan_items i WHERE i.plan_id = p.id AND i.stale = 1) AS stale
           FROM plans p ORDER BY p.created_at DESC""").fetchall()
    return [_plan_row(r) for r in rows]


def get_plan(conn: sqlite3.Connection, plan_id: int) -> dict | None:
    r = conn.execute(
        """SELECT p.*, (SELECT COALESCE(SUM(size), 0) FROM plan_items i WHERE i.plan_id = p.id
                        AND i.status IN ('move', 'copy', 'renamed')) AS bytes,
                  (SELECT COUNT(*) FROM plan_items i WHERE i.plan_id = p.id AND i.stale = 1) AS stale
           FROM plans p WHERE p.id = ?""", (plan_id,)).fetchone()
    return _plan_row(r) if r else None


def plan_items(conn: sqlite3.Connection, plan_id: int, status: str = "", page: int = 1,
               per_page: int = 200) -> tuple[list[dict], int]:
    where, args = "i.plan_id = ?", [plan_id]
    if status == "stale":
        where += " AND i.stale = 1"
    elif status:
        where += " AND i.status = ?"
        args.append(status)
    total = conn.execute(f"SELECT COUNT(*) FROM plan_items i WHERE {where}", args).fetchone()[0]
    rows = conn.execute(
        f"""SELECT i.*, f.ext, f.container_id, NULL AS c1_name, NULL AS c2_name FROM plan_items i
            LEFT JOIN files f ON f.id = i.file_id AND f.state = 'present'
            WHERE {where}
            ORDER BY i.src_user LIMIT ? OFFSET ?""",
        args + [per_page, (max(page, 1) - 1) * per_page]).fetchall()
    out = []
    for r in rows:
        v = dict(r)
        v["thumb"] = r["ext"] is not None and can_thumb(r)
        out.append(v)
    return out, total


def plan_folders(conn: sqlite3.Connection, plan_id: int, limit: int = 40) -> list[tuple[str, int]]:
    """Destination folders, with how many files go to each."""
    out: Counter = Counter()
    for (dst,) in conn.execute(
            "SELECT dst_user FROM plan_items WHERE plan_id = ? AND status IN ('move', 'copy', 'renamed')", (plan_id,)):
        out[_share_folder(dst)] += 1
    return out.most_common(limit)


EXPORT_COLUMNS = ["status", "src_user", "dst_user", "src_path", "dst_path", "size", "note", "stale"]


def export_csv(conn: sqlite3.Connection, plan_id: int):
    import csv
    import io

    from .queries import csv_cell

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_COLUMNS)
    for r in conn.execute(f"SELECT {', '.join(EXPORT_COLUMNS)} FROM plan_items WHERE plan_id = ? ORDER BY src_user",
                          (plan_id,)):
        w.writerow([csv_cell(v) for v in r])
        if buf.tell() > 65536:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
    yield buf.getvalue()

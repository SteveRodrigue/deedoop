"""Changes to your files (milestone 6): remove duplicates, apply plans, undo.

Nothing here runs until you turn on "Allow changes" in Setup *and* the
container sees ``/mnt`` read-write. Every change is confirmed on a screen
first, runs as a job, and is written to a journal (``action_items``) so it
can be checked and undone.

Removals go to a quarantine by default: the file is renamed into
``<root>/.deedoop-quarantine/<action>/…`` inside the same share folder on the
same disk. That is a rename, so it is instant, copies nothing, and on Unraid it
creates no new share. Quarantined files are deleted after ``quarantine_days``
or when you empty the quarantine. Direct delete is opt-in and cannot be undone.

Before touching a file deedoop checks, for every file:

* the file is still the one in the index (size and modified time),
* the copy you keep still exists, is a regular file, is not the same file
  (a hardlink) and, for exact duplicates, still has the same content (both
  are read again and hashed); for "same photo, other metadata" both images
  are decoded and compared again,
* a move's destination is still free.

Anything that fails a check is skipped with the reason, never forced.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path

import blake3

from . import db, safepath
from .control import Control, Status, Stopped
from .hashing import Expected, FileChanged, full_hash
from .scanner import QUARANTINE_DIR, QUARANTINE_DIRS

log = logging.getLogger(__name__)

MODES = {
    "quarantine": "Move to quarantine (can be undone)",
    "hardlink": "Replace with a hardlink to the kept copy (exact copies on the same disk; can be undone)",
    "delete": "Delete permanently (cannot be undone)",
}
ITEM_STATUS = {
    "pending": "Waiting",
    "done": "Done",
    "skipped": "Skipped",
    "failed": "Failed",
    "undone": "Undone",
    "purged": "Deleted from quarantine",
}
KEEPER_RULES = {
    "suggested": "the suggested copy (photos: highest resolution; music and video: most complete tags)",
    "oldest": "the oldest modified copy",
    "newest": "the newest modified copy",
    "shortest": "the copy with the shortest path",
    "path": "a copy whose path contains…",
}


class ChangeError(RuntimeError):
    pass


# -- the gate -------------------------------------------------------------------


def mnt_writable(mnt: Path) -> bool:
    """True when the container sees ``mnt`` read-write."""
    try:
        return not (os.statvfs(mnt).f_flag & os.ST_RDONLY) and os.access(mnt, os.W_OK)
    except OSError:
        return False


def gate(conn: sqlite3.Connection, mnt: Path) -> dict:
    s = db.get_settings(conn)
    rw = mnt_writable(mnt)
    return {"allowed": bool(s["allow_changes"]), "writable": rw, "open": bool(s["allow_changes"]) and rw,
            "direct_delete": bool(s["allow_direct_delete"]), "quarantine_days": int(s["quarantine_days"] or 0)}


def require_open(conn: sqlite3.Connection, mnt: Path) -> None:
    g = gate(conn, mnt)
    if not g["allowed"]:
        raise ChangeError("Changes are turned off. Turn on “Allow changes” on the Setup screen.")
    if not g["writable"]:
        raise ChangeError("/mnt is mounted read-only in the container. Change the /mnt path to read-write "
                          "in the container settings, then try again.")


# -- marks: what you chose to remove ------------------------------------------------


MEMBER_SQL = """
    SELECT m.group_id, f.id, f.size, f.mtime_ns, f.dev, f.inode, f.container_id, f.dir, f.name,
           r.path AS root_path, r.user_path, COALESCE(md.width, av.width) AS width,
           COALESCE(md.height, av.height) AS height, md.taken_at, g.method, av.tags AS tag_count,
           av.kind, av.vcodec, av.acodec, av.bitrate, av.duration AS seconds
    FROM group_members m JOIN groups g ON g.id = m.group_id
    JOIN files f ON f.id = m.file_id JOIN roots r ON r.id = f.root_id
    LEFT JOIN media md ON md.file_id = f.id LEFT JOIN av ON av.file_id = f.id
    WHERE m.group_id IN ({marks}) AND f.state = 'present'
    ORDER BY m.group_id, f.id
"""


def _members(conn: sqlite3.Connection, group_ids: list[int]) -> dict[int, list[sqlite3.Row]]:
    out: dict[int, list[sqlite3.Row]] = {}
    for i in range(0, len(group_ids), 500):
        chunk = group_ids[i:i + 500]
        for r in conn.execute(MEMBER_SQL.format(marks=",".join("?" * len(chunk))), chunk):
            out.setdefault(r["group_id"], []).append(r)
    return out


def _user_path(r) -> str:
    return f"{r['user_path']}/{r['dir']}/{r['name']}" if r["dir"] else f"{r['user_path']}/{r['name']}"


def pick_keeper(members: list, rule: str, text: str = ""):
    """The copy to keep under one of ``KEEPER_RULES``. Only loose files can be kept."""
    from .queries import suggested_rank as keeper_rank

    loose = [m for m in members if not m["container_id"]]
    if not loose:
        return None
    if rule == "oldest":
        return min(loose, key=lambda m: (m["mtime_ns"], _user_path(m)))
    if rule == "newest":
        return max(loose, key=lambda m: (m["mtime_ns"], _user_path(m)))
    if rule == "shortest":
        return min(loose, key=lambda m: (len(_user_path(m)), _user_path(m)))
    if rule == "path":
        hits = [m for m in loose if text and text.lower() in _user_path(m).lower()]
        return max(hits, key=keeper_rank) if hits else None
    return max(loose, key=keeper_rank)


def _mark(conn: sqlite3.Connection, members: list, keeper, method: str) -> int:
    """Mark every other loose copy in a group for removal, keeping ``keeper``."""
    now = time.time()
    conn.execute("DELETE FROM marks WHERE file_id = ?", (keeper["id"],))  # you keep it now
    n = 0
    for m in members:
        if m["id"] == keeper["id"] or m["container_id"]:
            continue
        if (m["dev"], m["inode"]) == (keeper["dev"], keeper["inode"]):
            continue  # a hardlink of the kept copy: removing it frees nothing
        if conn.execute("SELECT 1 FROM marks WHERE keep_id = ?", (m["id"],)).fetchone():
            continue  # it is the kept copy of another mark
        conn.execute("INSERT OR REPLACE INTO marks(file_id, keep_id, method, marked_at) VALUES (?, ?, ?, ?)",
                     (m["id"], keeper["id"], method, now))
        n += 1
    return n


def mark_group(conn: sqlite3.Connection, group_id: int, keep_id: int) -> int:
    members = _members(conn, [group_id]).get(group_id, [])
    keeper = next((m for m in members if m["id"] == keep_id and not m["container_id"]), None)
    if keeper is None:
        return 0
    with db.Tx(conn):
        conn.execute(f"DELETE FROM marks WHERE file_id IN ({','.join(str(m['id']) for m in members)})")
        return _mark(conn, members, keeper, members[0]["method"])


def unmark_group(conn: sqlite3.Connection, group_id: int) -> None:
    with db.Tx(conn):
        conn.execute("DELETE FROM marks WHERE file_id IN (SELECT file_id FROM group_members WHERE group_id = ?)",
                     (group_id,))


def mark_groups(conn: sqlite3.Connection, group_ids: list[int], rule: str, text: str = "") -> dict:
    """Mark many groups at once with one keeper rule. Groups where the rule
    finds no copy to keep are left alone."""
    groups = _members(conn, group_ids)
    marked = kept = left = 0
    with db.Tx(conn):
        for _gid, members in groups.items():
            if members[0]["method"] in ("similar", "media_similar"):
                left += 1
                continue  # similar photos, songs and videos are decided one by one in Review
            keeper = pick_keeper(members, rule, text)
            if keeper is None:
                left += 1
                continue
            conn.execute(f"DELETE FROM marks WHERE file_id IN ({','.join(str(m['id']) for m in members)})")
            n = _mark(conn, members, keeper, members[0]["method"])
            marked += n
            kept += 1 if n else 0
    return {"files": marked, "groups": kept, "left": left}


def review_removals(conn: sqlite3.Connection, threshold: int) -> list[tuple[int, int]]:
    """(remove, keep) pairs from "Keep left" / "Keep right" decisions in Review."""
    from .similar import PAIRS_WITH_DECISION

    out = []
    for r in conn.execute(PAIRS_WITH_DECISION + " AND rv.decision IN ('keep_a', 'keep_b') "
                          "AND fa.state = 'present' AND fb.state = 'present'", {"t": threshold}):
        if r["keep"] == r["ka"] and r["keep"] != r["kb"]:
            out.append((r["b"], r["a"]))
        elif r["keep"] == r["kb"] and r["keep"] != r["ka"]:
            out.append((r["a"], r["b"]))
    return out


def all_review_removals(conn: sqlite3.Connection, settings: dict, threshold: int) -> list[tuple[int, int, str]]:
    """(remove, keep, method) from Review: similar photos and, with milestone 8,
    re-encoded songs and videos, each while its matching is on."""
    out: list[tuple[int, int, str]] = []
    if settings.get("similar_matching"):
        out += [(rm, keep, "similar") for rm, keep in review_removals(conn, threshold)]
    from . import scanners

    if "media_similar" in scanners.methods(settings):
        from .media_similar import removals

        out += [(rm, keep, "media_similar") for rm, keep in removals(conn)]
    return out


def mark_reviewed(conn: sqlite3.Connection, threshold: int, settings: dict | None = None) -> int:
    settings = settings or {"similar_matching": True}
    pairs = all_review_removals(conn, settings, threshold)
    keepers = {k for _, k, _ in pairs}
    now = time.time()
    n = 0
    with db.Tx(conn):
        for rm, keep, method in pairs:
            if rm in keepers:
                continue  # kept in another decision: never remove a copy you chose to keep
            conn.execute("DELETE FROM marks WHERE file_id = ?", (keep,))
            conn.execute("INSERT OR REPLACE INTO marks(file_id, keep_id, method, marked_at) VALUES (?, ?, ?, ?)",
                         (rm, keep, method, now))
            n += 1
    return n


def clear_marks(conn: sqlite3.Connection, file_ids: list[int] | None = None) -> None:
    with db.Tx(conn):
        if file_ids is None:
            conn.execute("DELETE FROM marks")
        else:
            conn.executemany("DELETE FROM marks WHERE file_id = ?", [(i,) for i in file_ids])


MARKED_SQL = """
    SELECT mk.file_id, mk.keep_id, mk.method, mk.marked_at,
           f.size, f.mtime_ns, f.dir, f.name, f.ext, f.dev, f.state, r.disk, r.path AS root_path, r.user_path,
           k.size AS keep_size, k.mtime_ns AS keep_mtime, k.dir AS keep_dir, k.name AS keep_name, k.dev AS keep_dev,
           k.state AS keep_state, kr.path AS keep_root, kr.user_path AS keep_user, kr.disk AS keep_disk
    FROM marks mk JOIN files f ON f.id = mk.file_id JOIN roots r ON r.id = f.root_id
    JOIN files k ON k.id = mk.keep_id JOIN roots kr ON kr.id = k.root_id
"""


def _join(base: str, d: str, n: str) -> str:
    return f"{base}/{d}/{n}" if d else f"{base}/{n}"


def marked(conn: sqlite3.Connection, limit: int = 0, offset: int = 0) -> list[dict]:
    q = MARKED_SQL + " ORDER BY f.size DESC, mk.file_id" + (f" LIMIT {int(limit)} OFFSET {int(offset)}" if limit else "")
    out = []
    for r in conn.execute(q):
        out.append({
            "file_id": r["file_id"], "keep_id": r["keep_id"], "method": r["method"], "size": r["size"],
            "ext": r["ext"], "disk": r["disk"], "keep_disk": r["keep_disk"],
            "path": _join(r["root_path"], r["dir"], r["name"]), "user": _join(r["user_path"], r["dir"], r["name"]),
            "keep_path": _join(r["keep_root"], r["keep_dir"], r["keep_name"]),
            "keep_user": _join(r["keep_user"], r["keep_dir"], r["keep_name"]),
            "same_disk": r["dev"] == r["keep_dev"],
            "ok": r["state"] == "present" and r["keep_state"] == "present",
        })
    return out


def marks_summary(conn: sqlite3.Connection) -> dict:
    r = conn.execute("""SELECT COUNT(*) AS n, COALESCE(SUM(f.size), 0) AS bytes,
                               SUM(mk.method = 'exact') AS exact, SUM(mk.method = 'image') AS image,
                               SUM(mk.method = 'similar') AS similar, SUM(mk.method = 'media') AS media,
                               SUM(mk.method = 'media_similar') AS media_similar
                        FROM marks mk JOIN files f ON f.id = mk.file_id""").fetchone()
    return {"files": r["n"], "bytes": r["bytes"], "exact": r["exact"] or 0, "image": r["image"] or 0,
            "similar": r["similar"] or 0, "media": r["media"] or 0, "media_similar": r["media_similar"] or 0}


def marked_in_groups(conn: sqlite3.Connection, group_ids: list[int]) -> dict[int, set[int]]:
    """Per group, the member file ids marked for removal (for the Duplicates page)."""
    if not group_ids:
        return {}
    out: dict[int, set[int]] = {}
    marks = ",".join("?" * len(group_ids))
    for r in conn.execute(f"SELECT gm.group_id, mk.file_id FROM group_members gm JOIN marks mk ON mk.file_id = gm.file_id "
                          f"WHERE gm.group_id IN ({marks})", group_ids):
        out.setdefault(r[0], set()).add(r[1])
    return out


# -- actions ------------------------------------------------------------------------


def _qpath(root_path: str, action_id: int, d: str, n: str) -> str:
    return _join(f"{root_path}/{QUARANTINE_DIR}/{action_id}", d, n)


def _new_action(conn, kind: str, mode: str, label: str, plan_id: int | None = None) -> int:
    return conn.execute("INSERT INTO actions(kind, mode, label, plan_id, created_at) VALUES (?, ?, ?, ?, ?)",
                        (kind, mode, label, plan_id, time.time())).lastrowid


def create_removal(conn: sqlite3.Connection, mnt: Path, mode: str, file_ids: list[int] | None = None) -> int:
    """Turn the marks (or some of them) into a confirmed removal job."""
    require_open(conn, mnt)
    if mode not in MODES:
        raise ChangeError("Unknown mode")
    if mode == "delete" and not db.get_settings(conn)["allow_direct_delete"]:
        raise ChangeError("Direct delete is turned off on the Setup screen.")
    rows = conn.execute(MARKED_SQL + " WHERE f.state = 'present' AND k.state = 'present' "
                        "AND f.container_id = 0 AND k.container_id = 0 ORDER BY f.size DESC, mk.file_id").fetchall()
    if file_ids is not None:
        wanted = set(file_ids)
        rows = [r for r in rows if r["file_id"] in wanted]
    removing = {r["file_id"] for r in rows}
    rows = [r for r in rows if r["keep_id"] not in removing]  # never remove a kept copy
    if not rows:
        raise ChangeError("Nothing is marked for removal.")
    n = len(rows)
    with db.Tx(conn):
        aid = _new_action(conn, "remove", mode, f"Remove {n:,} {'copy' if n == 1 else 'copies'}")
        conn.executemany(
            """INSERT INTO action_items(action_id, seq, file_id, keep_id, method, src_path, src_user, dst_path,
                   keep_path, size, mtime_ns, keep_size, keep_mtime) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(aid, i, r["file_id"], r["keep_id"], r["method"], _join(r["root_path"], r["dir"], r["name"]),
              _join(r["user_path"], r["dir"], r["name"]),
              "" if mode == "delete" else _qpath(r["root_path"], aid, r["dir"], r["name"]),
              _join(r["keep_root"], r["keep_dir"], r["keep_name"]), r["size"], r["mtime_ns"], r["keep_size"],
              r["keep_mtime"]) for i, r in enumerate(rows)])
        conn.executemany("DELETE FROM marks WHERE file_id = ?", [(r["file_id"],) for r in rows])
        _enqueue(conn, aid, "action")
    return aid


def create_move(conn: sqlite3.Connection, mnt: Path, plan_id: int) -> int:
    """Apply a saved plan: every item that would move, still unchanged."""
    from . import plans

    require_open(conn, mnt)
    plans.recheck(conn, plan_id)
    p = plans.get_plan(conn, plan_id)
    if p is None:
        raise ChangeError("No such plan")
    rows = conn.execute(
        """SELECT pi.*, f.dir, f.name, r.path AS root_path FROM plan_items pi
           JOIN files f ON f.id = pi.file_id JOIN roots r ON r.id = f.root_id
           WHERE pi.plan_id = ? AND pi.stale = 0 AND pi.status IN ('move', 'renamed', 'copy')
           ORDER BY pi.src_user""", (plan_id,)).fetchall()
    if not rows:
        raise ChangeError("Nothing in this plan can be moved: no file would move, or all of them changed.")
    with db.Tx(conn):
        aid = _new_action(conn, "move", "move", f"Apply plan “{p['name']}”", plan_id)
        conn.executemany(
            """INSERT INTO action_items(action_id, seq, file_id, src_path, src_user, dst_path, dst_user, quarantine,
                   size, mtime_ns, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(aid, i, r["file_id"], r["src_path"], r["src_user"], r["dst_path"], r["dst_user"],
              _qpath(r["root_path"], aid, r["dir"], r["name"]) if r["status"] == "copy" else "",
              r["size"], r["mtime_ns"], "copy" if r["status"] == "copy" else "") for i, r in enumerate(rows)])
        _enqueue(conn, aid, "action")
    return aid


def _enqueue(conn, action_id: int, job_type: str) -> int:
    jid = conn.execute("INSERT INTO jobs(type, params, created_at) VALUES (?, ?, ?)",
                       (job_type, json.dumps({"action_id": action_id}), time.time())).lastrowid
    if job_type == "action":
        conn.execute("UPDATE actions SET job_id = ? WHERE id = ?", (jid, action_id))
    return jid


def request_undo(conn: sqlite3.Connection, mnt: Path, action_id: int) -> int:
    require_open(conn, mnt)
    a = get_action(conn, action_id)
    if a is None or not a["can_undo"]:
        raise ChangeError("This change cannot be undone.")
    with db.Tx(conn):
        return _enqueue(conn, action_id, "undo")


def request_purge(conn: sqlite3.Connection, mnt: Path, action_id: int) -> int:
    require_open(conn, mnt)
    with db.Tx(conn):
        return _enqueue(conn, action_id, "purge")


def enqueue_due_purges(conn: sqlite3.Connection, days: int) -> int:
    """Queue a purge for quarantines older than ``days`` (0 keeps them)."""
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    due = conn.execute(
        """SELECT a.id FROM actions a WHERE a.purged_at IS NULL AND a.finished_at < ? AND a.state IN ('done', 'stopped')
           AND EXISTS (SELECT 1 FROM action_items i WHERE i.action_id = a.id AND i.status = 'done'
                       AND (i.quarantine != '' OR (a.kind = 'remove' AND i.dst_path != '')))
           AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.state IN ('queued', 'running') AND j.type = 'purge'
                           AND json_extract(j.params, '$.action_id') = a.id)""", (cutoff,)).fetchall()
    with db.Tx(conn):
        for r in due:
            _enqueue(conn, r["id"], "purge")
    return len(due)


# -- reading actions ---------------------------------------------------------------


def _action_row(conn, r) -> dict:
    a = dict(r)
    a["counts"] = json.loads(a["counts"] or "{}")
    q = conn.execute(
        """SELECT COUNT(*), COALESCE(SUM(size), 0) FROM action_items
           WHERE action_id = ? AND status = 'done' AND (quarantine != '' OR (? = 'remove' AND dst_path != ''))""",
        (a["id"], a["kind"])).fetchone()
    a["quarantined"], a["quarantined_bytes"] = q[0], q[1]
    a["total"] = conn.execute("SELECT COUNT(*) FROM action_items WHERE action_id = ?", (a["id"],)).fetchone()[0]
    a["done_bytes"] = conn.execute("SELECT COALESCE(SUM(size), 0) FROM action_items WHERE action_id = ? "
                                   "AND status IN ('done', 'purged')", (a["id"],)).fetchone()[0]
    has_done = conn.execute("SELECT 1 FROM action_items WHERE action_id = ? AND status = 'done' LIMIT 1",
                            (a["id"],)).fetchone() is not None
    a["can_undo"] = has_done and a["mode"] != "delete" and a["state"] in ("done", "stopped", "failed")
    a["busy"] = conn.execute(
        "SELECT 1 FROM jobs WHERE state IN ('queued', 'running') AND type IN ('action', 'undo', 'purge') "
        "AND json_extract(params, '$.action_id') = ?", (a["id"],)).fetchone() is not None
    return a


def list_actions(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    return [_action_row(conn, r) for r in conn.execute("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,))]


def get_action(conn: sqlite3.Connection, action_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    return _action_row(conn, r) if r else None


def action_items(conn: sqlite3.Connection, action_id: int, status: str = "", page: int = 1,
                 per_page: int = 200) -> tuple[list[dict], int, dict]:
    counts = {r[0]: r[1] for r in conn.execute(
        "SELECT status, COUNT(*) FROM action_items WHERE action_id = ? GROUP BY status", (action_id,))}
    where, args = "action_id = ?", [action_id]
    if status:
        where += " AND status = ?"
        args.append(status)
    total = conn.execute(f"SELECT COUNT(*) FROM action_items WHERE {where}", args).fetchone()[0]
    rows = conn.execute(f"SELECT * FROM action_items WHERE {where} ORDER BY seq LIMIT ? OFFSET ?",
                        args + [per_page, (max(page, 1) - 1) * per_page]).fetchall()
    return [dict(r) for r in rows], total, counts


# -- running -------------------------------------------------------------------------


@dataclass
class RunContext:
    db_path: Path
    mnt: Path
    action_id: int
    control: Control
    status: Status


class Skip(Exception):
    """The item is left alone; the message says why."""


def _matches(path: str, size: int, mtime_ns: int) -> os.stat_result:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise Skip("the file is no longer there") from None
    if not stat.S_ISREG(st.st_mode):
        raise Skip("not a regular file any more")
    if st.st_size != size or st.st_mtime_ns != mtime_ns:
        raise Skip("the file changed since the last scan")
    return st


def _inside(path: str, mnt: Path) -> None:
    try:
        safepath.check(path)
    except safepath.UnsafePath:
        raise Skip("unsafe path") from None
    if not safepath.inside(os.path.dirname(path), str(mnt)):
        raise Skip(f"outside {mnt}")


# Every rename, link and delete below goes through deedoop.safepath (rule SC2),
# which refuses to follow a symbolic link anywhere in the path.


def _fs(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except safepath.Exists:
        raise Skip("something is already at the destination") from None
    except safepath.UnsafePath as e:
        raise Skip(str(e)) from None


def _makedirs(path: str) -> list[str]:
    """Create the missing folders above ``path``; return them, top first."""
    made: list[str] = []
    d = os.path.dirname(path)
    missing = []
    while d and not os.path.isdir(d):
        missing.append(d)
        d = os.path.dirname(d)
    for m in reversed(missing):
        _fs(safepath.mkdir, m)
        made.append(m)
    return made


def _rmdirs(dirs: list[str]) -> None:
    for d in reversed(dirs):
        try:
            safepath.rmdir(d)
        except OSError:
            pass  # not empty, or already gone


def _rename_noclobber(src: str, dst: str) -> None:
    _fs(safepath.rename_noclobber, src, dst)


def _unlink(path: str, missing_ok: bool = False) -> None:
    _fs(safepath.unlink, path, missing_ok=missing_ok)


def _hash_file(path: str, size: int, mtime_ns: int, checkpoint) -> bytes:
    try:
        return full_hash(path, Expected(size, mtime_ns), checkpoint=checkpoint)
    except FileChanged:
        raise Skip("the file changed while it was being checked") from None


def _verify_same(item: sqlite3.Row, ctl: Control) -> None:
    method = item["method"]
    if method == "exact":
        a = _hash_file(item["src_path"], item["size"], item["mtime_ns"], ctl.checkpoint)
        b = _hash_file(item["keep_path"], item["keep_size"], item["keep_mtime"], ctl.checkpoint)
        if a != b:
            raise Skip("the two copies are no longer identical")
    elif method == "image":
        from .imaging import NotAnImage, read_and_hash_image
        from .scanner import ext_of

        try:
            ha = read_and_hash_image(item["src_path"], ext_of(item["src_path"]), Expected(item["size"], item["mtime_ns"]),
                                     ctl.checkpoint)
            hb = read_and_hash_image(item["keep_path"], ext_of(item["keep_path"]),
                                     Expected(item["keep_size"], item["keep_mtime"]), ctl.checkpoint)
        except (FileChanged, NotAnImage):
            raise Skip("could not check the two photos again") from None
        if not ha.get("img_exact") or ha.get("img_exact") != hb.get("img_exact"):
            raise Skip("the two photos no longer have the same image data")
    elif method == "media":
        # Milestone 7: both files are read again and must still hold the same
        # audio and video streams.
        from .media_index import hash_one

        ka, va = hash_one(item["src_path"], item["size"], item["mtime_ns"])
        ctl.checkpoint()
        kb, vb = hash_one(item["keep_path"], item["keep_size"], item["keep_mtime"])
        if ka != "stream" or kb != "stream":
            raise Skip("could not check the two recordings again")
        if va != vb:
            raise Skip("the two files no longer hold the same recording")
    # similar, media_similar: you looked at or listened to both copies in Review;
    # the checks above (unchanged, kept copy present) still apply.


def _remove(item: sqlite3.Row, mode: str, mnt: Path, ctl: Control) -> dict:
    src, keep = item["src_path"], item["keep_path"]
    _inside(src, mnt)
    st = _matches(src, item["size"], item["mtime_ns"])
    try:
        ks = _matches(keep, item["keep_size"], item["keep_mtime"])
    except Skip as e:
        raise Skip(f"the copy to keep: {e}") from None
    if (st.st_dev, st.st_ino) == (ks.st_dev, ks.st_ino):
        raise Skip("it is the same file as the copy to keep (a hardlink)")
    if mode == "hardlink":
        if item["method"] != "exact":
            raise Skip("only exact copies can become hardlinks")
        if st.st_dev != ks.st_dev:
            raise Skip("on another disk: a hardlink needs both copies on the same disk")
    _verify_same(item, ctl)
    ctl.checkpoint()
    if mode == "delete":
        _unlink(src)
        return {"note": "deleted"}
    q = item["dst_path"]
    _inside(q, mnt)
    made = _makedirs(q)
    _rename_noclobber(src, q)
    if mode == "hardlink":
        try:
            _fs(safepath.link, keep, src)
        except (OSError, Skip) as e:
            _rename_noclobber(q, src)  # put it back
            _rmdirs(made)
            raise Skip(f"could not make the hardlink: {getattr(e, 'strerror', None) or e}") from None
        return {"note": "now a hardlink to the kept copy; original in quarantine", "made_dirs": made}
    return {"note": "", "made_dirs": made}


def _copy_verified(src: str, dst: str, size: int, mtime_ns: int, ctl: Control) -> None:
    tmp = dst + ".deedoop-partial"
    if os.path.lexists(tmp):
        raise Skip("a leftover partial copy is in the way")
    want = _hash_file(src, size, mtime_ns, ctl.checkpoint)
    try:
        with open(src, "rb") as fi, _fs(safepath.open_new, tmp) as fo:
            h = blake3.blake3()
            while True:
                ctl.checkpoint()
                b = fi.read(4 << 20)
                if not b:
                    break
                fo.write(b)
            fo.flush()
            os.fsync(fo.fileno())
        shutil.copystat(src, tmp)
        with open(tmp, "rb") as f:
            while b := f.read(4 << 20):
                h.update(b)
        if h.digest() != want:
            raise Skip("the copy did not match the original")
        _rename_noclobber(tmp, dst)
    finally:
        if os.path.lexists(tmp):
            _unlink(tmp, missing_ok=True)


def _move(item: sqlite3.Row, mnt: Path, ctl: Control) -> dict:
    src, dst = item["src_path"], item["dst_path"]
    _inside(src, mnt)
    _inside(dst, mnt)
    _matches(src, item["size"], item["mtime_ns"])
    if os.path.lexists(dst) or (item["dst_user"] and os.path.lexists(item["dst_user"])):
        raise Skip("something is already at the destination")
    made = _makedirs(dst)
    try:
        if item["note"] == "copy":
            _copy_verified(src, dst, item["size"], item["mtime_ns"], ctl)
            q = item["quarantine"]
            made_q = _makedirs(q)
            _rename_noclobber(src, q)
            return {"note": "copied and checked; original in quarantine", "made_dirs": made + made_q}
        try:
            _rename_noclobber(src, dst)
        except OSError as e:
            raise Skip(f"could not move: {e.strerror}") from None
    except BaseException:
        _rmdirs(made)
        raise
    return {"note": "", "made_dirs": made}


def _locate(conn, path: str) -> tuple[int, str, str] | None:
    """The index root, folder and name of a physical path."""
    best = None
    for r in conn.execute("SELECT id, path FROM roots WHERE enabled = 1"):
        p = r["path"].rstrip("/") + "/"
        if path.startswith(p) and (best is None or len(p) > len(best[1])):
            best = (r["id"], p)
    if best is None:
        return None
    rel = path[len(best[1]):]
    d, _, n = rel.rpartition("/")
    return best[0], d, n


def _index_moved(conn, file_id: int, path: str) -> None:
    loc = _locate(conn, path)
    if loc is None:
        conn.execute("UPDATE files SET state = 'gone' WHERE id = ?", (file_id,))
        return
    conn.execute("DELETE FROM files WHERE root_id = ? AND dir = ? AND name = ? AND container_id = 0 AND id != ?",
                 (*loc, file_id))
    conn.execute("UPDATE files SET root_id = ?, dir = ?, name = ?, state = 'present' WHERE id = ?", (*loc, file_id))


def _index_stat(conn, file_id: int, path: str) -> None:
    try:
        st = os.lstat(path)
    except OSError:
        return
    conn.execute("UPDATE files SET dev = ?, inode = ?, nlink = ?, mtime_ns = ?, size = ? WHERE id = ?",
                 (st.st_dev, st.st_ino, st.st_nlink, st.st_mtime_ns, st.st_size, file_id))


def _refresh_groups(conn) -> None:
    from .engine import regroup

    regroup(conn, db.get_settings(conn), pairs=False)


def _finish(conn, action_id: int, state: str | None = None) -> dict:
    counts = {r[0]: r[1] for r in conn.execute(
        "SELECT status, COUNT(*) FROM action_items WHERE action_id = ? GROUP BY status", (action_id,))}
    sets, args = ["counts = ?"], [json.dumps(counts)]
    if state:
        sets.append("state = ?")
        args.append(state)
    conn.execute(f"UPDATE actions SET {', '.join(sets)} WHERE id = ?", args + [action_id])
    return counts


def run_action(ctx: RunContext) -> dict:
    conn = db.connect(ctx.db_path)
    try:
        a = conn.execute("SELECT * FROM actions WHERE id = ?", (ctx.action_id,)).fetchone()
        if a is None:
            raise ChangeError("No such action")
        require_open(conn, ctx.mnt)
        conn.execute("UPDATE actions SET state = 'running' WHERE id = ?", (a["id"],))
        items = conn.execute("SELECT * FROM action_items WHERE action_id = ? AND status = 'pending' ORDER BY seq",
                             (a["id"],)).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM action_items WHERE action_id = ?", (a["id"],)).fetchone()[0]
        done = total - len(items)
        ctx.status.set(phase="changes", action=a["id"], todo=total, done=done, current="")
        state = "done"
        try:
            for it in items:
                ctx.control.checkpoint()
                ctx.status.set(current=it["src_user"], done=done)
                try:
                    res = _move(it, ctx.mnt, ctx.control) if a["kind"] == "move" else \
                        _remove(it, a["mode"], ctx.mnt, ctx.control)
                    status, note = "done", res.get("note", "")
                    with db.Tx(conn):
                        conn.execute("UPDATE action_items SET status = ?, note = ?, made_dirs = ?, done_at = ? "
                                     "WHERE action_id = ? AND seq = ?",
                                     (status, note, json.dumps(res.get("made_dirs", [])), time.time(), a["id"], it["seq"]))
                        if a["kind"] == "move":
                            _index_moved(conn, it["file_id"], it["dst_path"])
                            if it["note"] == "copy":
                                _index_stat(conn, it["file_id"], it["dst_path"])
                        elif a["mode"] == "hardlink":
                            _index_stat(conn, it["file_id"], it["src_path"])
                        else:
                            conn.execute("UPDATE files SET state = 'gone' WHERE id = ?", (it["file_id"],))
                except Skip as e:
                    conn.execute("UPDATE action_items SET status = 'skipped', note = ? WHERE action_id = ? AND seq = ?",
                                 (str(e), a["id"], it["seq"]))
                except OSError as e:
                    log.warning("change failed for %s: %s", it["src_path"], e)
                    conn.execute("UPDATE action_items SET status = 'failed', note = ? WHERE action_id = ? AND seq = ?",
                                 (f"{e.strerror or e}", a["id"], it["seq"]))
                done += 1
                ctx.status.set(done=done)
        except Stopped:
            state = "stopped"
            raise
        except Exception:
            state = "failed"
            raise
        finally:
            ctx.status.set(phase="group", current="")
            _refresh_groups(conn)
            conn.execute("UPDATE actions SET finished_at = ? WHERE id = ?", (time.time(), a["id"]))
            counts = _finish(conn, a["id"], state)
            if a["plan_id"]:
                from . import plans

                plans.recheck(conn, a["plan_id"])
        return {"action": a["id"], **counts}
    finally:
        conn.close()


def run_undo(ctx: RunContext) -> dict:
    conn = db.connect(ctx.db_path)
    try:
        a = conn.execute("SELECT * FROM actions WHERE id = ?", (ctx.action_id,)).fetchone()
        require_open(conn, ctx.mnt)
        if a["mode"] == "delete":
            raise ChangeError("Deleted files cannot be restored")
        items = conn.execute("SELECT * FROM action_items WHERE action_id = ? AND status = 'done' ORDER BY seq DESC",
                             (a["id"],)).fetchall()
        ctx.status.set(phase="undo", action=a["id"], todo=len(items), done=0)
        undone = failed = 0
        try:
            for n, it in enumerate(items, 1):
                ctx.control.checkpoint()
                ctx.status.set(current=it["src_user"], done=n - 1)
                try:
                    _undo_item(a, it, ctx.mnt)
                except (Skip, OSError) as e:
                    failed += 1
                    conn.execute("UPDATE action_items SET note = ? WHERE action_id = ? AND seq = ?",
                                 (f"undo failed: {e}", a["id"], it["seq"]))
                    continue
                undone += 1
                with db.Tx(conn):
                    conn.execute("UPDATE action_items SET status = 'undone', note = 'restored' WHERE action_id = ? "
                                 "AND seq = ?", (a["id"], it["seq"]))
                    if a["kind"] == "move":
                        _index_moved(conn, it["file_id"], it["src_path"])
                        if it["note"].startswith("copied"):
                            _index_stat(conn, it["file_id"], it["src_path"])
                    else:
                        conn.execute("UPDATE files SET state = 'present' WHERE id = ?", (it["file_id"],))
                        if a["mode"] == "hardlink":
                            _index_stat(conn, it["file_id"], it["src_path"])
        finally:
            _refresh_groups(conn)
            left = conn.execute("SELECT COUNT(*) FROM action_items WHERE action_id = ? AND status = 'done'",
                                (a["id"],)).fetchone()[0]
            if left == 0:
                conn.execute("UPDATE actions SET undone_at = ? WHERE id = ?", (time.time(), a["id"]))
            _finish(conn, a["id"], "undone" if left == 0 else None)
            if a["plan_id"]:
                from . import plans

                plans.recheck(conn, a["plan_id"])
        return {"undone": undone, "failed": failed}
    finally:
        conn.close()


def _undo_item(a, it, mnt: Path) -> None:
    src = it["src_path"]
    _inside(src, mnt)
    if a["kind"] == "move":
        dst = it["dst_path"]
        _matches(dst, it["size"], it["mtime_ns"])
        if it["note"].startswith("copied"):
            q = it["quarantine"]
            if not os.path.lexists(q):
                raise Skip("the original is no longer in quarantine")
            _makedirs(src)
            _rename_noclobber(q, src)
            _unlink(dst)
        else:
            _makedirs(src)
            _rename_noclobber(dst, src)
        _rmdirs(json.loads(it["made_dirs"] or "[]"))
        return
    q = it["dst_path"]
    if not os.path.lexists(q):
        raise Skip("the file is no longer in quarantine")
    if a["mode"] == "hardlink":
        try:
            ls, ks = os.lstat(src), os.lstat(it["keep_path"])
        except FileNotFoundError:
            ls = ks = None
        if ls is not None:
            if (ls.st_dev, ls.st_ino) != (ks.st_dev, ks.st_ino):
                raise Skip("the hardlink was replaced by another file")
            _unlink(src)
    _makedirs(src)
    _rename_noclobber(q, src)
    _rmdirs(json.loads(it["made_dirs"] or "[]"))


def run_purge(ctx: RunContext) -> dict:
    """Delete an action's quarantined files for good."""
    conn = db.connect(ctx.db_path)
    try:
        a = conn.execute("SELECT * FROM actions WHERE id = ?", (ctx.action_id,)).fetchone()
        require_open(conn, ctx.mnt)
        items = conn.execute("SELECT * FROM action_items WHERE action_id = ? AND status = 'done'",
                             (a["id"],)).fetchall()
        purged = 0
        roots = set()
        for it in items:
            ctx.control.checkpoint()
            q = it["quarantine"] or (it["dst_path"] if a["kind"] == "remove" else "")
            qdir = next((d for d in QUARANTINE_DIRS if f"/{d}/{a['id']}/" in q), None) if q else None
            if qdir is None:
                continue
            _inside(q, ctx.mnt)
            try:
                _unlink(q, missing_ok=True)
            except Skip:
                continue
            purged += 1
            roots.add(q.split(f"/{qdir}/")[0] + f"/{qdir}")
            conn.execute("UPDATE action_items SET status = 'purged' WHERE action_id = ? AND seq = ?",
                         (a["id"], it["seq"]))
        for r in roots:  # remove the emptied folders, never anything with files left in it
            top = f"{r}/{a['id']}"
            for d, _subdirs, _files in sorted(os.walk(top, topdown=False), key=lambda x: -len(x[0])):
                try:
                    safepath.rmdir(d)
                except OSError:
                    pass
            try:
                safepath.rmdir(r)
            except OSError:
                pass
        conn.execute("UPDATE actions SET purged_at = ? WHERE id = ?", (time.time(), a["id"]))
        _finish(conn, a["id"])
        return {"purged": purged}
    finally:
        conn.close()

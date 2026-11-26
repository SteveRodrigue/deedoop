import os
import time
from pathlib import Path

import pytest

from deedoop import changes, db, plans, queries
from deedoop.control import Control, Status
from deedoop.discovery import add_custom_root
from tests.conftest import scan, write
from tests.test_similar import photos  # noqa: F401  (fixture)

DATA = os.urandom(300_000)


def run(cfg, aid, fn=changes.run_action):
    return fn(changes.RunContext(cfg.db_path, cfg.mnt, aid, Control(), Status()))


def fid(conn, name, dir_like="%"):
    return conn.execute("SELECT id FROM files WHERE name = ? AND dir LIKE ? AND container_id = 0",
                        (name, dir_like)).fetchone()[0]


def exact_groups(conn):
    return conn.execute("SELECT COUNT(*) FROM groups WHERE method = 'exact'").fetchone()[0]


@pytest.fixture
def dups(unraid, conn):
    m = unraid.mnt
    write(m / "disk1/Photos/keep/a.bin", DATA)
    write(m / "disk2/Photos/old/a copy.bin", DATA)
    db.set_settings(conn, {"shares": ["Photos"]})
    scan(unraid)
    return m


def allow(conn, **kw):
    db.set_settings(conn, {"allow_changes": True, **kw})


def test_gate_is_closed_by_default(unraid, conn, dups):
    g = changes.gate(conn, unraid.mnt)
    assert g["writable"] and not g["allowed"] and not g["open"]
    gid = conn.execute("SELECT id FROM groups WHERE method = 'exact'").fetchone()[0]
    assert changes.mark_group(conn, gid, fid(conn, "a.bin")) == 1  # marking is allowed: it changes nothing
    with pytest.raises(changes.ChangeError, match="turned off"):
        changes.create_removal(conn, unraid.mnt, "quarantine")
    allow(conn)
    with pytest.raises(changes.ChangeError, match="Direct delete"):
        changes.create_removal(conn, unraid.mnt, "delete")


def test_quarantine_undo_and_purge(unraid, conn, dups):
    m = dups
    allow(conn)
    gid = conn.execute("SELECT id FROM groups WHERE method = 'exact'").fetchone()[0]
    keep = fid(conn, "a.bin")
    assert changes.mark_group(conn, gid, keep) == 1
    assert changes.marks_summary(conn)["bytes"] == len(DATA)
    aid = changes.create_removal(conn, m, "quarantine")
    assert changes.marks_summary(conn)["files"] == 0  # the marks became the action
    res = run(unraid, aid)
    assert res["done"] == 1
    q = m / f"disk2/Photos/.deedoop-quarantine/{aid}/old/a copy.bin"
    assert q.read_bytes() == DATA and not (m / "disk2/Photos/old/a copy.bin").exists()
    assert (m / "disk1/Photos/keep/a.bin").read_bytes() == DATA
    assert exact_groups(conn) == 0
    scan(unraid)  # the quarantine is never indexed
    assert exact_groups(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM files WHERE dir LIKE '%deedoop-quarantine%'").fetchone()[0] == 0

    a = changes.get_action(conn, aid)
    assert a["can_undo"] and a["quarantined"] == 1
    assert run(unraid, aid, changes.run_undo) == {"undone": 1, "failed": 0}
    assert (m / "disk2/Photos/old/a copy.bin").read_bytes() == DATA and not q.exists()
    assert changes.get_action(conn, aid)["state"] == "undone"
    assert exact_groups(conn) == 1

    # again, then empty the quarantine for good
    changes.mark_group(conn, conn.execute("SELECT id FROM groups WHERE method = 'exact'").fetchone()[0], keep)
    aid = changes.create_removal(conn, m, "quarantine")
    run(unraid, aid)
    run(unraid, aid, changes.run_purge)
    assert not (m / "disk2/Photos/.deedoop-quarantine").exists()
    assert changes.get_action(conn, aid)["can_undo"] is False


def test_every_file_is_checked_again(unraid, conn, dups):
    m = dups
    allow(conn)
    write(m / "disk1/Photos/b.bin", DATA[:200_000])
    write(m / "disk2/Photos/b.bin", DATA[:200_000])
    write(m / "disk1/Photos/c.bin", DATA[:100_000])
    write(m / "disk2/Photos/c.bin", DATA[:100_000])
    scan(unraid)
    changes.mark_groups(conn, [g["id"] for g in conn.execute("SELECT id FROM groups WHERE method = 'exact'")],
                        "path", "/disk1/")
    assert changes.marks_summary(conn)["files"] == 0  # user paths never contain the disk
    n = changes.mark_groups(conn, [g["id"] for g in conn.execute("SELECT id FROM groups WHERE method = 'exact'")],
                            "shortest")["files"]
    assert n == 3
    # after the scan: one removal candidate changed, one kept copy vanished, one pair differs but looks the same
    marks = {r["user"].rsplit("/", 1)[1]: r for r in changes.marked(conn)}
    changed = marks["b.bin"]["path"]
    write(Path(changed), DATA[:200_000] + b"x")
    os.unlink(marks["c.bin"]["keep_path"])
    forged = marks["a copy.bin"]["path"] if "a copy.bin" in marks else marks["a.bin"]["path"]
    st = os.stat(forged)
    write(Path(forged), os.urandom(len(DATA)))
    os.utime(forged, ns=(st.st_atime_ns, st.st_mtime_ns))  # same size and time, other content
    aid = changes.create_removal(conn, m, "quarantine")
    res = run(unraid, aid)
    assert res.get("done", 0) == 0 and res["skipped"] == 3
    notes = {i["note"] for i in changes.action_items(conn, aid)[0]}
    assert notes == {"the file changed since the last scan", "the copy to keep: the file is no longer there",
                     "the two copies are no longer identical"}
    assert Path(forged).exists() and Path(changed).exists()


def test_hardlink_and_direct_delete(unraid, conn, dups):
    m = dups
    allow(conn, allow_direct_delete=True)
    write(m / "disk1/Photos/again/a.bin", DATA)  # a second copy on disk1, same disk as the keeper
    scan(unraid)
    gid = conn.execute("SELECT id FROM groups WHERE method = 'exact'").fetchone()[0]
    changes.mark_group(conn, gid, fid(conn, "a.bin", "keep"))
    aid = changes.create_removal(conn, m, "hardlink")
    res = run(unraid, aid)
    assert res["done"] == 2  # the fake disks share one filesystem here; on Unraid disk2 is skipped
    k, h = os.stat(m / "disk1/Photos/keep/a.bin"), os.stat(m / "disk1/Photos/again/a.bin")
    assert (k.st_dev, k.st_ino) == (h.st_dev, h.st_ino)
    run(unraid, aid, changes.run_undo)
    h = os.stat(m / "disk1/Photos/again/a.bin")
    assert h.st_ino != k.st_ino and (m / "disk1/Photos/again/a.bin").read_bytes() == DATA

    scan(unraid)
    gid = conn.execute("SELECT id FROM groups WHERE method = 'exact'").fetchone()[0]
    changes.mark_group(conn, gid, fid(conn, "a.bin", "keep"))
    aid = changes.create_removal(conn, m, "delete")
    assert run(unraid, aid)["done"] == 2
    assert not (m / "disk2/Photos/old/a copy.bin").exists() and not (m / "disk1/Photos/again/a.bin").exists()
    assert not changes.get_action(conn, aid)["can_undo"]
    assert (m / "disk1/Photos/keep/a.bin").read_bytes() == DATA


def test_apply_plan_and_undo(unraid, conn):
    m = unraid.mnt
    for d in ("disk2/Backups", "user/Backups"):
        (m / d).mkdir(parents=True, exist_ok=True)
    write(m / "disk2/Backups/phone/a.jpg", b"a" * 5000)
    write(m / "disks/usb/Backup/e.jpg", b"e" * 3000)
    db.set_settings(conn, {"shares": ["Photos", "Backups"]})
    add_custom_root(conn, m, str(m / "disks/usb/Backup"))
    scan(unraid)
    rule = plans.Rule("Photos/{year}/{name}", fallback="mtime")
    pids = []
    for loc in ("share:Backups", f"root:{conn.execute('SELECT id FROM roots WHERE share IS NULL').fetchone()[0]}"):
        rows = queries.selection(conn, queries.BrowseFilter(loc=loc))
        pids.append(plans.save(conn, loc, rule, {"loc": loc}, plans.preview(conn, m, rows, rule)))
    year = time.localtime().tm_year
    with pytest.raises(changes.ChangeError):
        changes.create_move(conn, m, pids[0])  # changes are off
    allow(conn)

    aid = changes.create_move(conn, m, pids[0])
    assert run(unraid, aid)["done"] == 1
    moved = m / f"disk2/Photos/{year}/a.jpg"  # same disk: a rename
    assert moved.read_bytes() == b"a" * 5000 and not (m / "disk2/Backups/phone/a.jpg").exists()
    f = conn.execute("SELECT f.dir, f.name, r.share FROM files f JOIN roots r ON r.id = f.root_id "
                     "WHERE f.name = 'a.jpg'").fetchone()
    assert (f["share"], f["dir"]) == ("Photos", str(year))
    assert plans.get_plan(conn, pids[0])["stale"] == 0  # the plan is checked again after it runs
    run(unraid, aid, changes.run_undo)
    assert (m / "disk2/Backups/phone/a.jpg").exists() and not (m / f"disk2/Photos/{year}").exists()

    aid = changes.create_move(conn, m, pids[1])  # another device: copied, checked, original quarantined
    assert run(unraid, aid)["done"] == 1
    assert (m / f"user/Photos/{year}/e.jpg").read_bytes() == b"e" * 3000
    assert not (m / "disks/usb/Backup/e.jpg").exists()
    assert (m / f"disks/usb/Backup/.deedoop-quarantine/{aid}/e.jpg").exists()
    run(unraid, aid, changes.run_undo)
    assert (m / "disks/usb/Backup/e.jpg").exists() and not (m / f"user/Photos/{year}/e.jpg").exists()


def test_destination_taken_at_apply_time(unraid, conn):
    m = unraid.mnt
    for d in ("disk2/Backups", "user/Backups"):
        (m / d).mkdir(parents=True, exist_ok=True)
    write(m / "disk2/Backups/a.jpg", b"a" * 5000)
    db.set_settings(conn, {"shares": ["Photos", "Backups"]})
    scan(unraid)
    rule = plans.Rule("Photos/{name}")
    rows = queries.selection(conn, queries.BrowseFilter(loc="share:Backups"))
    pid = plans.save(conn, "x", rule, {}, plans.preview(conn, m, rows, rule))
    write(m / "disk1/Photos/a.jpg", b"other")  # appears after the plan; /mnt/user would show it
    write(m / "user/Photos/a.jpg", b"other")
    allow(conn)
    aid = changes.create_move(conn, m, pid)
    assert run(unraid, aid)["skipped"] == 1
    assert (m / "disk2/Backups/a.jpg").exists()


def test_pages(unraid, conn, dups):
    from tests.test_web import make_client

    with make_client(unraid) as client:
        r = client.get("/duplicates")
        assert "Keep the selected copy" in r.text and "read-only" in r.text
        gid = conn.execute("SELECT id FROM groups WHERE method = 'exact'").fetchone()[0]
        r = client.post("/marks/group", data={"group_id": gid, "keep": fid(conn, "a.bin"), "back": "/duplicates"})
        assert "1 marked for removal" in r.text and "Review and confirm" in r.text
        r = client.get("/changes")
        assert "Changes are off" in r.text and "a copy.bin" in r.text
        r = client.post("/changes/remove", data={"mode": "quarantine"})
        assert "turned off" in r.text
        r = client.post("/setup/changes", data={"allow_changes": "1", "quarantine_days": "7"})
        assert "read-write" in r.text and "changes allowed" in r.text
        r = client.post("/changes/remove", data={"mode": "delete"})
        assert "Type DELETE" in r.text
        r = client.post("/changes/remove", data={"mode": "quarantine"})
        assert r.status_code == 200 and "Remove 1 copy" in r.text
        aid = conn.execute("SELECT id FROM actions").fetchone()[0]
        for _ in range(100):
            if changes.get_action(conn, aid)["state"] == "done":
                break
            time.sleep(0.1)
        r = client.get(f"/changes/{aid}")
        assert "Undo this change" in r.text and "Empty quarantine" in r.text
        r = client.post(f"/changes/{aid}/purge", data={})
        assert "Tick the box" in r.text
        assert client.get("/setup").status_code == 200




def test_review_decisions_become_marks(unraid, conn, photos):  # noqa: F811
    from deedoop import similar

    scan(unraid)
    pair = similar.queue(conn, 8)[0]
    similar.decide(conn, pair["a"], pair["b"], "keep_b")
    assert changes.review_removals(conn, 8) == [(pair["a"], pair["b"])]
    assert changes.mark_reviewed(conn, 8) == 1
    (r,) = changes.marked(conn)
    assert (r["file_id"], r["keep_id"], r["method"]) == (pair["a"], pair["b"], "similar")
    # bulk marking leaves similar groups to Review
    gids = [g["id"] for g in conn.execute("SELECT id FROM groups WHERE method = 'similar'")]
    assert changes.mark_groups(conn, gids, "suggested")["files"] == 0

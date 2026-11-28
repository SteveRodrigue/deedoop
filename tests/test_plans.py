import os

import pytest

from deedoop import db, plans, queries
from deedoop.discovery import add_custom_root
from tests.conftest import scan, write


def date(conn, name, taken, camera=None, dir_like="%"):
    fid = conn.execute("SELECT id FROM files WHERE name = ? AND dir LIKE ? AND container_id = 0",
                       (name, dir_like)).fetchone()[0]
    conn.execute("INSERT OR REPLACE INTO media(file_id, taken_at, camera, extracted_at) VALUES (?, ?, ?, 0)",
                 (fid, taken, camera))
    return fid


@pytest.fixture
def library(unraid, conn):
    m = unraid.mnt
    for d in ("disk2/Backups", "user/Backups"):
        (m / d).mkdir(parents=True, exist_ok=True)
    same = os.urandom(5000)
    write(m / "disk2/Backups/phone/a.jpg", os.urandom(4000))
    write(m / "disk1/Photos/2019/07/b.jpg", same)
    write(m / "disk2/Backups/phone/b.jpg", same)  # identical to what is already in place
    write(m / "disk1/Photos/2019/07/c.jpg", os.urandom(3000))
    write(m / "disk2/Backups/old/c.jpg", os.urandom(3100))  # same name, other content
    write(m / "disk2/Backups/x/d.jpg", os.urandom(2000))
    write(m / "disk2/Backups/y/d.jpg", os.urandom(2100))  # both of these want the same name
    write(m / "disk2/Backups/notes.txt", b"no date here", mtime=1262347200)  # 2010-01-01
    write(m / "disks/usb/Backup/e.jpg", os.urandom(1500))
    db.set_settings(conn, {"shares": ["Photos", "Backups"]})
    add_custom_root(conn, m, str(m / "disks/usb/Backup"))
    scan(unraid)
    date(conn, "a.jpg", "2019-07-14 10:00:00", "Google Pixel 3")
    date(conn, "b.jpg", "2019-07-20 10:00:00", dir_like="2019/07")
    date(conn, "b.jpg", "2019-07-20 10:00:00", dir_like="phone")
    date(conn, "c.jpg", "2019-07-02 10:00:00", dir_like="2019/07")
    date(conn, "c.jpg", "2019-07-03 10:00:00", dir_like="old")
    date(conn, "d.jpg", "2019-07-05 10:00:00", dir_like="x")
    date(conn, "d.jpg", "2019-07-05 10:00:00", dir_like="y")
    date(conn, "e.jpg", "2021-03-04 10:00:00")
    return m


def run(conn, mnt, template, loc="share:Backups", dir="", **kw):
    rows = queries.selection(conn, queries.BrowseFilter(loc=loc, dir=dir))
    return {i.src_user.split("/mnt/", 1)[1]: i for i in plans.preview(conn, mnt, rows, plans.Rule(template, **kw))}


def test_rule_checks(library):
    shares = {"Photos", "Backups"}
    assert plans.Rule("/mnt/user/Photos/{year}/{name}").check(shares) == ("Photos", ["{year}", "{name}"])
    for bad in ("Photos", "Nope/{name}", "Photos/{bogus}/{name}", "Photos/../{name}"):
        with pytest.raises(plans.RuleError):
            plans.Rule(bad).check(shares)


def test_preview_moves_on_the_same_disk(conn, library):
    m = library
    got = run(conn, m, "Photos/{year}/{month}/{name}")
    a = got["user/Backups/phone/a.jpg"]
    assert a.status == "move"
    assert a.dst_user == f"{m}/user/Photos/2019/07/a.jpg"
    assert a.dst_path == f"{m}/disk2/Photos/2019/07/a.jpg"  # stays on disk2
    assert got["user/Backups/phone/b.jpg"].status == "duplicate"  # same content already on disk1
    c = got["user/Backups/old/c.jpg"]
    assert c.status == "renamed" and c.dst_user.endswith("/Photos/2019/07/c (1).jpg")
    d = sorted((got["user/Backups/x/d.jpg"], got["user/Backups/y/d.jpg"]), key=lambda i: i.src_user)
    assert d[0].status == "move" and d[1].status == "renamed" and d[1].dst_user.endswith("d (1).jpg")
    n = got["user/Backups/notes.txt"]
    assert n.status == "move" and n.dst_user.endswith("/Photos/Undated/notes.txt")
    s = plans.summary(list(got.values()))
    assert s["counts"]["duplicate"] == 1 and s["moving"] == 5
    assert dict(s["folders"]) == {"Photos/2019/07": 4, "Photos/Undated": 1}


def test_fallbacks_and_conflict_policy(conn, library):
    m = library
    got = run(conn, m, "Photos/{year}/{name}", fallback="mtime", conflicts="skip")
    assert got["user/Backups/notes.txt"].dst_user.endswith("/Photos/2010/notes.txt")
    assert "modified" in got["user/Backups/notes.txt"].note
    got = run(conn, m, "Photos/{year}/{month}/{name}", fallback="skip", conflicts="skip")
    assert got["user/Backups/notes.txt"].status == "skipped"
    assert got["user/Backups/old/c.jpg"].status == "conflict"
    assert sorted(got[k].status for k in ("user/Backups/x/d.jpg", "user/Backups/y/d.jpg")) == ["conflict", "move"]


def test_in_place_camera_and_folder_tokens(conn, library):
    m = library
    got = run(conn, m, "Photos/{year}/{month}/{name}", loc="share:Photos")
    assert {i.status for i in got.values()} == {"in_place"}
    got = run(conn, m, "{share}/{camera}/{folder}/{name}", dir="phone")
    assert got["user/Backups/phone/a.jpg"].dst_user == f"{m}/user/Backups/Google Pixel 3/phone/a.jpg"
    assert got["user/Backups/phone/b.jpg"].dst_user.endswith("/Backups/Unknown camera/phone/b.jpg")


def test_files_outside_the_shares_are_copied(conn, library):
    m = library
    rid = conn.execute("SELECT id FROM roots WHERE share IS NULL").fetchone()[0]
    got = run(conn, m, "Photos/{year}/{name}", loc=f"root:{rid}")
    (e,) = got.values()
    assert e.status == "copy" and e.dst_path == f"{m}/user/Photos/2021/e.jpg"


def test_save_and_recheck(unraid, conn, library):
    m = library
    rows = queries.selection(conn, queries.BrowseFilter(loc="share:Backups"))
    rule = plans.Rule("Photos/{year}/{month}/{name}")
    pid = plans.save(conn, "Backups into Photos", rule, {"loc": "share:Backups"}, plans.preview(conn, m, rows, rule))
    p = plans.get_plan(conn, pid)
    assert p["moving"] == 5 and p["total"] == 6 and p["rule"]["template"] == rule.template
    items, total = plans.plan_items(conn, pid, "renamed")
    assert total == 2 and all("(1)" in i["dst_user"] for i in items)
    assert plans.recheck(conn, pid) == {"items": 6, "stale": 0}
    write(m / "disk2/Backups/phone/a.jpg", os.urandom(4100))  # changed after the plan
    scan(unraid)
    assert plans.recheck(conn, pid)["stale"] == 1
    csv = "".join(plans.export_csv(conn, pid))
    assert csv.startswith("status,src_user,dst_user") and csv.count("\n") == 7
    # nothing on disk moved
    assert (m / "disk2/Backups/phone/a.jpg").exists() and not (m / "disk2/Photos/2019").exists()


def test_pages(unraid, conn, library):
    from tests.test_web import make_client

    with make_client(unraid) as client:
        r = client.get("/browse?loc=share:Backups")
        assert "Organize this folder" in r.text and 'href="/plans" class="on">Organize</a>' not in r.text
        r = client.get("/reorganize?loc=share:Backups&template=Photos/{year}/{month}/{name}")
        assert r.status_code == 200 and "Would move" in r.text and "c (1).jpg" in r.text
        r = client.get("/reorganize?loc=share:Backups&template=Nope/{name}")
        assert "There is no share called Nope" in r.text
        r = client.get("/reorganize?loc=share:Backups&template=Photos/{year}/{month}/{name}&status=duplicate")
        assert "phone/b.jpg" in r.text and "phone/a.jpg" not in r.text
        r = client.post("/reorganize/save", data={"loc": "share:Backups", "template": "Photos/{year}/{name}",
                                                   "name": "Test plan"})
        assert r.status_code == 200 and "Plan saved" in r.text and "Test plan" in r.text
        pid = conn.execute("SELECT id FROM plans").fetchone()[0]
        r = client.get("/plans")
        assert "Test plan" in r.text and 'class="on">Organize</a>' in r.text  # the tab is called Organize, not Plans
        r = client.post(f"/plans/{pid}/recheck")
        assert "every file is unchanged" in r.text
        r = client.get(f"/plans/{pid}/export.csv")
        assert r.headers["content-type"].startswith("text/csv") and "a.jpg" in r.text
        r = client.post(f"/plans/{pid}/delete")
        assert "No saved plans" in r.text

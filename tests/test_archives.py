import io
import json
import os
import tarfile
import time
import zipfile

import pytest

from deedoop import db
from deedoop.archives import match_sidecars, parse_sidecar, sidecar_base, takeout_part
from tests.conftest import scan, write
from tests.test_images import jpeg, photo


def sidecar(title, ts=1546300800, lat=45.5, lon=-73.6):
    return json.dumps({"title": title, "photoTakenTime": {"timestamp": str(ts)},
                       "geoData": {"latitude": lat, "longitude": lon}}).encode()


def make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def make_tgz(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in entries.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mtime = 1_600_000_000
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


# -- unit: Takeout naming --------------------------------------------------------


def test_takeout_part_names():
    assert takeout_part("takeout-20240301T101010Z-001.zip") == ("takeout-20240301t101010z", 1)
    assert takeout_part("takeout-20240301T101010Z-012.tgz") == ("takeout-20240301t101010z", 12)
    assert takeout_part("holiday.zip") is None


@pytest.mark.parametrize("name,base,counter", [
    ("IMG_1.JPG.json", "IMG_1.JPG", ""),
    ("IMG_1.JPG.supplemental-metadata.json", "IMG_1.JPG", ""),
    ("IMG_1.JPG.supplemental-metad.json", "IMG_1.JPG", ""),
    ("IMG_1.JPG.suppl.json", "IMG_1.JPG", ""),
    ("IMG_1.JPG(1).json", "IMG_1.JPG", "(1)"),
    ("IMG_1.JPG.supplemental-metadata(2).json", "IMG_1.JPG", "(2)"),
])
def test_sidecar_base(name, base, counter):
    assert sidecar_base(name) == (base, counter)


def test_match_sidecars_google_rules():
    names = {"IMG_1.JPG": 1, "IMG_1(1).JPG": 2, "IMG_1-edited.JPG": 3,
             "Screenshot_20190101-123456_A_very_long_application_name.png": 4, "VID_9.MP4": 5}
    jsons = {
        "IMG_1.JPG.supplemental-metadata.json": (10, {"title": "IMG_1.JPG"}),
        "IMG_1.JPG(1).json": (11, {"title": "IMG_1.JPG"}),
        "Screenshot_20190101-123456_A_very_long_applicat.json": (12, {"title": "Screenshot_20190101-123456_A_very_long_application_name.png"}),
        "VID_9.MP4.json": (13, {"title": "VID_9.MP4"}),
        "unrelated.jpg.json": (14, {"title": "unrelated.jpg"}),
    }
    assert match_sidecars(names, jsons) == {1: 10, 2: 11, 3: 10, 4: 12, 5: 13}


def test_parse_sidecar():
    s = parse_sidecar(sidecar("a.jpg", ts=0 + 1546300800))
    assert s["title"] == "a.jpg" and s["taken_at"] == "2019-01-01 00:00:00" and s["lat"] == 45.5
    assert parse_sidecar(json.dumps({"title": "Trip", "albumData": {}}).encode()) is None  # album metadata.json
    assert parse_sidecar(b"not json") is None
    no_geo = parse_sidecar(json.dumps({"title": "x", "photoTakenTime": {"timestamp": "1"},
                                       "geoData": {"latitude": 0.0, "longitude": 0.0}}).encode())
    assert no_geo["lat"] is None


# -- scanning ----------------------------------------------------------------------


@pytest.fixture
def takeout(unraid, conn):
    """Loose photos on disk1, a two-part Takeout, a tar.gz with a nested zip, on disk2."""
    m = unraid.mnt
    a, b_img, c = jpeg(photo(31), exif={0x010F: "Canon"}), photo(32), jpeg(photo(33))
    b_disk = jpeg(b_img, exif={0x010F: "Canon", 0x0132: "2019:05:05 10:00:00"})
    b_takeout = jpeg(b_img, exif={0x010F: "Canon"})  # Google rewrote the metadata
    doc = os.urandom(70_000)
    write(m / "disk1/Photos/2019/IMG_0001.JPG", a)
    write(m / "disk1/Photos/2019/IMG_0002.JPG", b_disk)
    write(m / "disk1/Docs/report.pdf", doc)
    (m / "disk1/Docs").mkdir(exist_ok=True)
    gp = "Takeout/Google Photos/Photos from 2019/"
    write(m / "disk2/Backups/Takeout/takeout-20240301T101010Z-001.zip", make_zip({
        gp + "IMG_0001.JPG": a,
        gp + "IMG_0001.JPG.supplemental-metadata.json": sidecar("IMG_0001.JPG"),
        gp + "IMG_0002.JPG": b_takeout,
        gp + "IMG_0002(1).JPG": b_takeout,
        gp + "IMG_0002.JPG(1).json": sidecar("IMG_0002.JPG"),
        gp + "IMG_0003.JPG": c,
        "Takeout/Google Photos/Trip/metadata.json": json.dumps({"title": "Trip"}).encode(),
    }))
    write(m / "disk2/Backups/Takeout/takeout-20240301T101010Z-002.zip", make_zip({
        gp + "IMG_0003.JPG.json": sidecar("IMG_0003.JPG", ts=1546387200),
    }))
    inner = make_zip({"old/e.bin": os.urandom(5000), "old/a.jpg": a})
    write(m / "disk2/Backups/Takeout/laptop.tar.gz", make_tgz({"docs/report.pdf": doc, "inner.zip": inner}))
    write(m / "disk2/Backups/elsewhere.zip", make_zip({"x.jpg": a}))
    db.set_settings(conn, {"shares": ["Photos", "Docs", "Backups"],
                            "takeout_paths": [str(m / "user/Backups/Takeout")]})
    (m / "user/Backups").mkdir(exist_ok=True)
    return m


def test_archives_are_indexed_in_place(unraid, conn, takeout):
    s = scan(unraid)
    arcs = {r["name"]: r for r in conn.execute(
        "SELECT f.name, a.* FROM archives a JOIN files f ON f.id = a.file_id")}
    assert set(arcs) == {"takeout-20240301T101010Z-001.zip", "takeout-20240301T101010Z-002.zip", "laptop.tar.gz"}
    assert "elsewhere.zip" not in arcs  # outside the Takeout folders
    p1, p2 = arcs["takeout-20240301T101010Z-001.zip"], arcs["takeout-20240301T101010Z-002.zip"]
    assert p1["takeout_set"] == p2["takeout_set"] and (p1["part"], p2["part"]) == (1, 2)
    assert arcs["laptop.tar.gz"]["members"] == 4  # report.pdf, inner.zip and its 2 members
    assert s["archives"] == 3
    # nothing was extracted: the folder holds exactly what we wrote
    assert sorted(p.name for p in (takeout / "disk2/Backups/Takeout").iterdir()) == [
        "laptop.tar.gz", "takeout-20240301T101010Z-001.zip", "takeout-20240301T101010Z-002.zip"]


def test_members_join_duplicate_groups_without_inflating_waste(unraid, conn, takeout):
    scan(unraid)
    rows = conn.execute(
        """SELECT g.method, g.copies, g.wasted, COUNT(*) AS n, SUM(f.container_id != 0) AS members
           FROM groups g JOIN group_members gm ON gm.group_id = g.id JOIN files f ON f.id = gm.file_id
           GROUP BY g.id ORDER BY g.method, n DESC""").fetchall()
    exact = [tuple(r)[1:] for r in rows if r["method"] == "exact"]
    # photo A: 1 loose + Takeout + nested zip; one loose copy, so nothing to reclaim
    assert (3, 0, 3, 2) in exact
    # report.pdf: loose + tar member
    assert (2, 0, 2, 1) in exact
    image = [tuple(r)[1:] for r in rows if r["method"] == "image"]
    assert image and image[0][2] == 3 and image[0][1] == 0  # B on disk + 2 Takeout copies


def test_coverage_report(unraid, conn, takeout):
    scan(unraid)
    cov = {r["name"]: r for r in conn.execute(
        "SELECT f.name, a.* FROM archives a JOIN files f ON f.id = a.file_id")}
    p1 = cov["takeout-20240301T101010Z-001.zip"]
    # IMG_0001 exact, IMG_0002 and IMG_0002(1) same photo, IMG_0003 new; JSON sidecars not counted
    assert (p1["media_members"], p1["cov_exact"], p1["cov_image"], p1["cov_new"]) == (4, 1, 2, 1)
    lap = cov["laptop.tar.gz"]
    # report.pdf exact, inner a.jpg exact, e.bin new (inner.zip itself is not counted)
    assert (lap["media_members"], lap["cov_exact"], lap["cov_new"]) == (3, 2, 1)


def test_sidecars_pair_across_takeout_parts(unraid, conn, takeout):
    s = scan(unraid)
    pairs = {r[0]: r[1] for r in conn.execute(
        "SELECT f.name, j.name FROM sidecars s JOIN files f ON f.id = s.file_id JOIN files j ON j.id = s.json_file_id")}
    assert pairs == {
        "IMG_0001.JPG": "IMG_0001.JPG.supplemental-metadata.json",
        "IMG_0002(1).JPG": "IMG_0002.JPG(1).json",
        "IMG_0003.JPG": "IMG_0003.JPG.json",  # the JSON is in part 2
    }
    assert s["sidecars"] == 3


def test_changed_and_deleted_archives(unraid, conn, takeout):
    scan(unraid)
    tgz = takeout / "disk2/Backups/Takeout/laptop.tar.gz"
    write(tgz, make_tgz({"only.txt": b"hello there"}), mtime=time.time() + 5)
    scan(unraid)
    names = {r[0] for r in conn.execute(
        "SELECT m.name FROM files m JOIN files a ON a.id = m.container_id WHERE a.name = 'laptop.tar.gz'")}
    assert names == {"only.txt"}
    assert conn.execute("SELECT COUNT(*) FROM files WHERE name = 'e.bin'").fetchone()[0] == 0  # nested ones too
    tgz.unlink()
    scan(unraid)
    assert conn.execute("SELECT state FROM files WHERE name = 'only.txt'").fetchone()[0] == "gone"


def test_archives_everywhere_and_broken_archives(unraid, conn, takeout):
    write(takeout / "disk2/Backups/broken.zip", b"PK\x03\x04 this is not really a zip")
    db.set_settings(conn, {"archives_everywhere": True})
    scan(unraid)
    errs = {r[0]: r[1] for r in conn.execute(
        "SELECT f.name, a.error FROM archives a JOIN files f ON f.id = a.file_id")}
    assert "elsewhere.zip" in errs and errs["elsewhere.zip"] is None
    assert errs["broken.zip"] and "BadZipFile" in errs["broken.zip"]


def test_7z_members(unraid, conn, tmp_path):
    py7zr = pytest.importorskip("py7zr")
    m = unraid.mnt
    data = os.urandom(90_000)
    write(m / "disk1/Photos/x.bin", data)
    src = tmp_path / "src"
    (src / "d").mkdir(parents=True)
    (src / "d/x-copy.bin").write_bytes(data)
    (src / "empty.txt").write_bytes(b"")
    (m / "disk2/Photos").mkdir(parents=True, exist_ok=True)
    with py7zr.SevenZipFile(m / "disk2/Photos/pack.7z", "w") as z:
        z.writeall(src, arcname="")
    db.set_settings(conn, {"shares": ["Photos"], "archives_everywhere": True})
    scan(unraid)
    names = {r[0] for r in conn.execute("SELECT name FROM files WHERE container_id != 0")}
    assert {"x-copy.bin", "empty.txt"} <= names
    assert conn.execute("SELECT cov_exact FROM archives").fetchone()[0] == 1


def test_archive_pages(unraid, conn, takeout):
    from tests.test_web import make_client

    scan(unraid)
    with make_client(unraid) as client:
        r = client.get("/archives")
        assert r.status_code == 200
        assert "takeout-20240301T101010Z" in r.text and "2 parts" in r.text and "laptop.tar.gz" in r.text
        pid = conn.execute("SELECT id FROM files WHERE name = 'takeout-20240301T101010Z-002.zip'").fetchone()[0]
        r = client.get(f"/archives/{pid}")  # any part opens the whole set
        assert r.status_code == 200
        assert "Photos from 2019" in r.text and "IMG_0003.JPG" in r.text  # the one photo not on disk
        assert "2019-01-02" in r.text  # its date comes from the JSON in the other part
        assert "IMG_0001.JPG" not in r.text.split('id="files"')[1]
        r = client.get(f"/archives/{pid}?status=all&album=Photos+from+2019")
        assert "IMG_0001.JPG" in r.text and "Already on disk" in r.text
        # a member's thumbnail is read from the zip without extracting it
        mid = conn.execute("SELECT id FROM files WHERE name = 'IMG_0003.JPG' AND container_id != 0").fetchone()[0]
        r = client.get(f"/thumb/{mid}")
        assert r.headers["content-type"] == "image/jpeg"
        r = client.get(f"/file/{mid}")
        assert "Inside archive" in r.text and "takeout-20240301T101010Z-001.zip" in r.text
        assert "Takeout JSON" in r.text
        # duplicates: archive-only copies are hidden until asked for, and members are labelled
        r = client.get("/duplicates")
        assert "in archive" not in r.text
        r = client.get("/duplicates?archives=1")
        assert "in archive" in r.text and "laptop.tar.gz › docs/report.pdf" in r.text
        rows = client.get("/export/duplicates.csv?archives=1").text
        assert "laptop.tar.gz › inner.zip › old/a.jpg" in rows
        # Browse lists loose files only
        r = client.get("/browse?loc=share:Backups&dir=Takeout")
        assert "laptop.tar.gz" in r.text and "IMG_0003" not in r.text

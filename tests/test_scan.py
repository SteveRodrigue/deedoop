import os
import time

from deedup import db
from deedup.control import Control, Stopped
from deedup.hashing import EDGE
from tests.conftest import scan, write


def select(conn, *shares):
    db.set_settings(conn, {"shares": list(shares)})


def groups(conn):
    out = []
    for g in conn.execute("SELECT * FROM groups WHERE method = 'exact' ORDER BY wasted DESC, id"):
        paths = sorted(
            r[0] for r in conn.execute(
                "SELECT r.disk || ':' || CASE WHEN f.dir = '' THEN f.name ELSE f.dir || '/' || f.name END "
                "FROM group_members m JOIN files f ON f.id = m.file_id JOIN roots r ON r.id = f.root_id "
                "WHERE m.group_id = ?", (g["id"],))
        )
        out.append((g["copies"], g["wasted"], paths))
    return out


def test_exact_duplicates_with_different_names_across_disks(unraid, conn):
    m = unraid.mnt
    data = os.urandom(300_000)
    write(m / "disk1/Photos/2019/IMG_0001.JPG", data)
    write(m / "disk2/Photos/backup/copy of holiday.jpg", data)
    write(m / "disk2/Photos/unique.jpg", os.urandom(300_000))
    write(m / "cache/Docs/a.txt", b"hello world\n")
    write(m / "disk2/Docs/b.txt", b"hello world\n")
    select(conn, "Photos", "Docs")
    summary = scan(unraid)
    assert summary["groups"] == 2
    assert groups(conn) == [
        (2, 300_000, ["disk1:2019/IMG_0001.JPG", "disk2:backup/copy of holiday.jpg"]),
        (2, 12, ["cache:a.txt", "disk2:b.txt"]),
    ]
    cats = {g["category"] for g in conn.execute("SELECT category FROM groups")}
    assert cats == {"image", "document"}


def test_same_edges_different_middle_is_not_a_duplicate(unraid, conn):
    m = unraid.mnt
    head, tail = os.urandom(EDGE), os.urandom(EDGE)
    write(m / "disk1/Photos/a.bin", head + b"A" * 500_000 + tail)
    write(m / "disk2/Photos/b.bin", head + b"B" * 500_000 + tail)
    select(conn, "Photos")
    assert scan(unraid)["groups"] == 0
    # Both reached the full-hash stage because their partial hashes collide.
    assert conn.execute("SELECT COUNT(*) FROM hashes WHERE kind = 'blake3'").fetchone()[0] == 2


def test_unique_sizes_are_never_read(unraid, conn):
    m = unraid.mnt
    write(m / "disk1/Photos/a.bin", os.urandom(1000))
    write(m / "disk1/Photos/b.bin", os.urandom(2000))
    select(conn, "Photos")
    scan(unraid)
    assert conn.execute("SELECT COUNT(*) FROM hashes").fetchone()[0] == 0


def test_hardlinks_are_one_copy(unraid, conn):
    m = unraid.mnt
    a = write(m / "disk1/Photos/a.jpg", os.urandom(200_000))
    os.link(a, m / "disk1/Photos/hardlink.jpg")
    select(conn, "Photos")
    assert scan(unraid)["groups"] == 0  # two paths, one copy: nothing to reclaim
    write(m / "disk2/Photos/real-copy.jpg", a.read_bytes())
    scan(unraid)
    [(copies, wasted, paths)] = groups(conn)
    assert copies == 2 and wasted == 200_000 and len(paths) == 3


def test_rescan_is_incremental(unraid, conn, monkeypatch):
    from deedup import engine

    m = unraid.mnt
    data = os.urandom(400_000)
    write(m / "disk1/Photos/a.bin", data)
    write(m / "disk2/Photos/b.bin", data)
    select(conn, "Photos")
    scan(unraid)
    before = {r[0]: r[1] for r in conn.execute("SELECT file_id, value FROM hashes WHERE kind='blake3'")}
    assert len(before) == 2

    reads = []
    real_partial, real_full = engine.partial_hash, engine.full_hash
    monkeypatch.setattr(engine, "partial_hash", lambda p, *a, **k: reads.append(p) or real_partial(p, *a, **k))
    monkeypatch.setattr(engine, "full_hash", lambda p, *a, **k: reads.append(p) or real_full(p, *a, **k))

    # Unchanged files: the rescan only walks folders and reads nothing.
    assert scan(unraid)["groups"] == 1
    assert reads == []
    after = {r[0]: r[1] for r in conn.execute("SELECT file_id, value FROM hashes WHERE kind='blake3'")}
    assert after == before

    # A changed file loses its hashes and only it is reread.
    write(m / "disk2/Photos/b.bin", os.urandom(400_000), mtime=time.time() + 5)
    assert scan(unraid)["groups"] == 0
    # Its new partial hash differs from a.bin, so no full read is needed.
    assert [os.path.basename(p) for p in reads] == ["b.bin"]


def test_deleted_files_drop_out(unraid, conn):
    m = unraid.mnt
    data = os.urandom(100_000)
    write(m / "disk1/Photos/a.jpg", data)
    b = write(m / "disk2/Photos/b.jpg", data)
    select(conn, "Photos")
    assert scan(unraid)["groups"] == 1
    b.unlink()
    assert scan(unraid)["groups"] == 0
    assert conn.execute("SELECT state FROM files WHERE name = 'b.jpg'").fetchone()[0] == "gone"


def test_exclusions(unraid, conn):
    m = unraid.mnt
    data = os.urandom(50_000)
    write(m / "disk1/Photos/a.jpg", data)
    write(m / "disk2/Photos/.Recycle.Bin/a.jpg", data)
    write(m / "disk2/Photos/thumbs/a.jpg", data)
    select(conn, "Photos")
    db.set_settings(conn, {"exclusions": [".Recycle.Bin", "thumbs"]})
    assert scan(unraid)["groups"] == 0
    names = {r[0] for r in conn.execute("SELECT dir FROM files")}
    assert names == {""}


def test_min_size_and_empty_files(unraid, conn):
    m = unraid.mnt
    write(m / "disk1/Photos/empty1", b"")
    write(m / "disk2/Photos/empty2", b"")
    write(m / "disk1/Photos/small1", b"x" * 10)
    write(m / "disk2/Photos/small2", b"x" * 10)
    select(conn, "Photos")
    assert scan(unraid)["groups"] == 1  # empty files are never duplicates
    db.set_settings(conn, {"min_size": 100})
    assert scan(unraid)["groups"] == 0


def test_parallel_mode_finds_the_same(unraid, conn):
    m = unraid.mnt
    for i in range(20):
        d = os.urandom(70_000 + i)
        write(m / f"disk1/Photos/{i}.jpg", d)
        write(m / f"disk2/Photos/{i}-copy.jpg", d)
        write(m / f"cache/Docs/{i}.jpg", d)
    select(conn, "Photos", "Docs")
    db.set_settings(conn, {"disk_mode": "parallel", "parallel_disks": 3})
    s = scan(unraid)
    assert s["groups"] == 20 and s["files"] == 60


def test_scan_never_modifies_files(unraid, conn):
    m = unraid.mnt
    data = os.urandom(500_000)
    files = [write(m / "disk1/Photos/a.jpg", data, mtime=1_000_000_000),
             write(m / "disk2/Photos/b.jpg", data, mtime=1_000_000_000)]
    before = [(f.stat().st_mtime_ns, f.stat().st_size, f.read_bytes()) for f in files]
    listing = sorted(str(p) for p in m.rglob("*"))
    select(conn, "Photos")
    scan(unraid)
    assert [(f.stat().st_mtime_ns, f.stat().st_size, f.read_bytes()) for f in files] == before
    assert sorted(str(p) for p in m.rglob("*")) == listing


def test_stop_then_resume(unraid, conn):
    m = unraid.mnt
    data = os.urandom(300_000)
    write(m / "disk1/Photos/a.jpg", data)
    write(m / "disk2/Photos/b.jpg", data)
    select(conn, "Photos")
    jid = db.enqueue_job(conn, "scan")
    ctl = Control()
    ctl.stop("test")
    try:
        scan(unraid, job_id=jid, control=ctl)
    except Stopped:
        pass
    else:
        raise AssertionError("expected Stopped")
    assert scan(unraid, job_id=jid)["groups"] == 1


def test_unreadable_folder_keeps_its_files(unraid, conn):
    if os.geteuid() == 0:
        import pytest
        pytest.skip("root can read anything")
    m = unraid.mnt
    data = os.urandom(10_000)
    write(m / "disk1/Photos/locked/a.jpg", data)
    write(m / "disk2/Photos/b.jpg", data)
    select(conn, "Photos")
    assert scan(unraid)["groups"] == 1
    os.chmod(m / "disk1/Photos/locked", 0)
    try:
        assert scan(unraid)["groups"] == 1
    finally:
        os.chmod(m / "disk1/Photos/locked", 0o755)

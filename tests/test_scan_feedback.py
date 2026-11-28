"""A scan that cannot start, or that waits behind another job, says so; the live
view refreshes every second with speeds. (Found on the first live Unraid test:
a scan with no share ticked "finished" at once and the dashboard looked as if
nothing had happened.)"""

import json
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

import deedoop.web as web
from deedoop import db, queries
from deedoop.benchmark import BenchConfig
from deedoop.control import Status, fmt_seconds, progress_view
from deedoop.web.app import create_app
from deedoop.worker import Worker
from tests.conftest import ensure_admin, sign_in, write
from tests.test_web import make_client, wait_idle


def idle_client(cfg):
    """Signed in, but the worker thread is not started, so queued jobs stay queued."""
    ensure_admin(cfg)
    client = TestClient(create_app(cfg, worker=Worker(cfg, BenchConfig())))
    sign_in(client, cfg)
    return client


def jobs(cfg):
    c = db.connect(cfg.db_path)
    try:
        return [dict(r) for r in c.execute("SELECT id, type, state, error FROM jobs ORDER BY id")]
    finally:
        c.close()


def test_start_scan_with_nothing_selected_says_so_and_queues_nothing(unraid):
    client = idle_client(unraid)
    r = client.post("/scan/start", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?notice=noshares"
    assert jobs(unraid) == []
    page = client.get("/?notice=noshares").text
    assert "nothing to scan yet" in page and 'href="/setup"' in page
    # Only known notices are shown; the query string is never echoed.
    assert "<b>" not in client.get("/?notice=<b>x").text


def test_scan_queues_behind_a_running_job_instead_of_being_dropped(unraid):
    client = idle_client(unraid)
    client.post("/setup/shares", data={"share": ["Photos"]})
    c = db.connect(unraid.db_path)
    c.execute("INSERT INTO jobs(type, params, state, created_at) VALUES ('benchmark', '{}', 'running', 0)")
    c.close()
    r = client.post("/scan/start", follow_redirects=False)
    assert r.headers["location"] == "/?notice=queued"
    assert [(j["type"], j["state"]) for j in jobs(unraid)] == [("benchmark", "running"), ("scan", "queued")]
    page = client.get("/?notice=queued").text
    assert "Scan queued" in page and "Scan #2 starts as soon as this finishes" in page
    # A second click does not queue a second scan.
    client.post("/scan/start")
    assert len(jobs(unraid)) == 2
    # Signed out, the status card says a scan is queued, not "No scan yet".
    page = TestClient(client.app).get("/login").text
    assert "A scan starts as soon as the current job finishes" in page and "No scan yet" not in page


def test_scan_with_no_folders_fails_with_a_clear_message(unraid):
    # A scheduled scan (or a share that was unticked meanwhile) reaches the engine with nothing to read.
    c = db.connect(unraid.db_path)
    db.enqueue_job(c, "scan", {"scheduled": True})
    c.close()
    with make_client(unraid) as client:
        wait_idle(client)
        (job,) = jobs(unraid)
        assert job["state"] == "failed" and job["error"].startswith("Nothing to scan")
        assert "Traceback" not in job["error"]
        page = client.get("/").text
        assert "No scan yet" not in page and "Nothing to scan: no share or folder is selected" in page
        assert "flash err" in page


def test_signed_out_button_press_explains_why_nothing_happened(unraid):
    ensure_admin(unraid)
    client = TestClient(create_app(unraid, worker=Worker(unraid, BenchConfig())))
    r = client.post("/scan/start", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    page = client.get(r.headers["location"]).text
    assert "You are signed out, so that was not done" in page
    assert jobs(unraid) == []


def test_live_status_polls_every_second_and_shows_walk_speed_and_size(unraid):
    write(unraid.mnt / "disk1/Photos/a.bin", os.urandom(5000))
    write(unraid.mnt / "disk1/Photos/sub/b.bin", os.urandom(7000))
    with make_client(unraid) as client:
        client.post("/setup/shares", data={"share": ["Photos"]})
        client.post("/scan/start")
        wait_idle(client)
        c = db.connect(unraid.db_path)
        prog = json.loads(c.execute("SELECT progress FROM jobs").fetchone()[0])
        c.close()
        d = prog["disks"]["disk1"]
        assert d["walked"] == 2 and d["walked_bytes"] == 12000 and d["walked_dirs"] == 2
    st = Status()
    st.reset(job=1, type="scan", phase="walk", plan=["walk", "group"])
    st.disk("disk1", walked=0, walked_bytes=0)
    st.snapshot()
    time.sleep(0.6)
    st.add("disk1", "walked", 300)
    st.add("disk1", "walked_bytes", 3_000_000)
    snap = st.snapshot()
    assert 200 < snap["rate_files"] < 600 and snap["rate_bytes"] > 2_000_000
    pv = progress_view(snap, time.time())
    assert pv["walked_bytes"] == 3_000_000 and pv["rate_files"] == snap["rate_files"]
    st.paused(True)
    assert st.snapshot()["rate_files"] == 0.0
    assert 'hx-trigger="every 1s"' in (Path(web.__file__).parent / "templates/_status.html").read_text()


def test_appdata_size_counts_thumbnails_too(unraid):
    write(unraid.config_dir / "thumbs/ab/cd.webp", b"x" * 4096)
    queries._APPDATA_CACHE.clear()
    total = queries.appdata_bytes(unraid.config_dir)
    assert total >= 4096 + os.path.getsize(unraid.db_path)
    with make_client(unraid) as client:
        assert "All appdata:" in client.get("/").text


# -- the UI stays quick while a scan runs (found live: pages took 10 s mid-scan) -----------


def test_jobs_run_in_their_own_process_and_report_back(unraid):
    import multiprocessing

    for i in range(30):
        write(unraid.mnt / f"disk1/Photos/f{i}.bin", b"same" if i % 2 else os.urandom(64))
    ensure_admin(unraid)
    worker = Worker(unraid, BenchConfig(), isolate=True)
    with TestClient(create_app(unraid, worker=worker)) as client:
        sign_in(client, unraid)
        client.post("/setup/shares", data={"share": ["Photos"]})
        client.post("/scan/start")
        deadline = time.time() + 60
        seen_child = False
        while time.time() < deadline and jobs(unraid)[-1]["state"] in ("queued", "running"):
            seen_child |= any(p.name.startswith("deedoop-job-") for p in multiprocessing.active_children())
            time.sleep(0.05)
        (job,) = jobs(unraid)
        assert job["state"] == "done", job
        assert seen_child
        log = client.get(f"/log?job={job['id']}").text
        assert "Walking folders: finished in" in log and "Finished in" in log  # lines from the job's process
        c = db.connect(unraid.db_path)
        prog = json.loads(c.execute("SELECT progress FROM jobs").fetchone()[0])
        groups = c.execute("SELECT COUNT(*) FROM groups WHERE method = 'exact'").fetchone()[0]
        c.close()
        assert prog["result"]["groups"] == groups == 1
        assert "walk" in prog["phase_times"]
        assert "took" in client.get("/").text


def test_a_job_process_that_dies_is_recorded_as_failed(unraid):
    import multiprocessing

    for i in range(20):
        write(unraid.mnt / f"disk1/Photos/f{i}.bin", os.urandom(64))
    ensure_admin(unraid)
    worker = Worker(unraid, BenchConfig(), isolate=True)
    with TestClient(create_app(unraid, worker=worker)) as client:
        sign_in(client, unraid)
        client.post("/setup/shares", data={"share": ["Photos"]})
        client.post("/scan/start")
        client.post("/scan/pause")  # holds the scan at its first checkpoint
        deadline = time.time() + 30
        while time.time() < deadline:
            kids = [p for p in multiprocessing.active_children() if p.name.startswith("deedoop-job-")]
            if kids:
                kids[0].kill()  # as the kernel's out-of-memory killer would
                break
            time.sleep(0.05)
        assert kids
        while time.time() < deadline and jobs(unraid)[-1]["state"] in ("queued", "running"):
            time.sleep(0.05)
        (job,) = jobs(unraid)
        assert job["state"] == "failed" and "ended unexpectedly" in job["error"]
        assert "No scan yet" not in client.get("/").text


def test_status_bar_on_other_pages_while_a_job_runs(unraid):
    client = idle_client(unraid)
    assert 'class="jobbar idle"' in client.get("/browse").text
    c = db.connect(unraid.db_path)
    c.execute("INSERT INTO jobs(type, params, state, created_at) VALUES ('scan', '{}', 'running', 0)")
    c.close()
    page = client.get("/browse").text
    assert 'hx-get="/partials/jobbar"' in page and "Scan #1" in page
    assert 'id="jobbar"' not in client.get("/").text  # the dashboard's scan card shows it already
    assert "Scan #1" in client.get("/partials/jobbar").text
    page = client.get("/duplicates").text
    assert "Scan in progress: duplicate groups appear here when the scan finishes" in page


def test_phase_times_are_kept_for_the_next_scan():
    st = Status()
    st.reset(job=1, type="scan", phase="starting", plan=["walk", "hash_partial"])
    st.set(phase="walk")
    st.set(phase="hash_partial")
    assert "walk" in st.raw()["phase_times"]
    live = {**st.snapshot(), "phase": "walk", "disks": {"disk1": {"walked": 5}}}
    assert progress_view(live, time.time(), last_times={"walk": 7200})["last_took"] == fmt_seconds(7200)
    assert progress_view(live, time.time())["last_took"] == ""


def test_first_scan_warning_on_the_dashboard(unraid):
    client = idle_client(unraid)
    assert "The first scan reads every file once" in client.get("/").text


def test_live_paths_keep_the_file_name():
    from deedoop.web.app import tail_path

    assert tail_path("/mnt/disk1/backup/Steve/DESKTOP/Data/photo.jpg") == "…/Data/photo.jpg"
    assert tail_path("/mnt/disk1") == "/mnt/disk1" and tail_path(None) == ""


def test_browse_lists_child_folders_without_reading_every_file(unraid):
    import random

    from deedoop import queries

    c = db.connect(unraid.db_path)
    c.execute("INSERT INTO roots(path, disk, share, user_path, created_at) VALUES ('/r', 'disk1', 'S', '/u', 0)")
    rnd = random.Random(1)
    names = ["a", "a b", "a-b", "a0", "ab", "b", "B", "é", "x y", "a.b", "z!", "a!"]
    dirs = {""} | {"/".join(rnd.choice(names) for _ in range(rnd.randint(1, 4))) for _ in range(500)}
    for i, d in enumerate(sorted(dirs)):
        state = "gone" if rnd.random() < 0.15 else "present"
        c.execute("INSERT INTO files(root_id, dir, name, size, mtime_ns, dev, inode, nlink, ext, last_seen_run, state) "
                  "VALUES (1, ?, ?, 1, 0, 1, ?, 1, 'x', 1, ?)", (d, f"n{i}", i, state))
    present = [r[0] for r in c.execute("SELECT dir FROM files WHERE state = 'present'")]

    def expected(prefix):
        return {d[len(prefix):].split("/")[0] for d in present if d.startswith(prefix) and len(d) > len(prefix)}

    for prefix in [""] + [d + "/" for d in sorted(dirs) if d]:
        assert queries._child_dirs(c, 1, prefix) == expected(prefix), prefix
    # it uses the index: a handful of lookups per child folder, not a pass over the files
    plan = " ".join(r[3] for r in c.execute(
        "EXPLAIN QUERY PLAN SELECT dir FROM files WHERE root_id = 1 AND dir > 'a' AND dir < 'b' "
        "AND state = 'present' AND container_id = 0 ORDER BY dir LIMIT 1"))
    assert "USING INDEX" in plan and "SCAN files" not in plan
    totals = " ".join(r[3] for r in c.execute("EXPLAIN QUERY PLAN SELECT COUNT(*) FROM roots r CROSS JOIN files f "
                                              "INDEXED BY files_root_state ON f.root_id = r.id"))
    assert "files_root_state" in totals
    c.close()


# -- appdata stays small: the write-ahead log and the thumbnail cache --------------------


def test_write_ahead_log_is_capped_and_shrunk(unraid):
    c = db.connect(unraid.db_path)
    assert c.execute("PRAGMA journal_size_limit").fetchone()[0] == db.WAL_SIZE_LIMIT
    c.execute("CREATE TABLE junk (x BLOB)")
    c.execute("BEGIN")
    c.executemany("INSERT INTO junk VALUES (?)", [(os.urandom(4000),) for _ in range(3000)])
    c.execute("COMMIT")
    wal = str(unraid.db_path) + "-wal"
    assert os.path.getsize(wal) > 5_000_000
    reader = db.connect(unraid.db_path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM junk").fetchone()  # a page reading at that moment...
    c.execute("INSERT INTO junk VALUES (x'00')")  # ...while the scan writes on
    assert db.shrink_wal(c, wait_ms=50) is False  # gives up quickly instead of stalling the scan
    reader.execute("COMMIT")
    reader.close()
    assert db.shrink_wal(c) is True and os.path.getsize(wal) == 0
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 60000
    c.close()


def test_thumbnail_cache_keeps_under_its_limit(tmp_path):
    from deedoop.worker import prune_thumbnails

    for i in range(10):
        p = tmp_path / "320" / f"{i:02x}" / f"{i}.jpg"
        write(p, b"x" * 1000)
        os.utime(p, (1000 + i, 1000 + i))
    assert prune_thumbnails(tmp_path, 4500) == 6000
    left = sorted(p.name for p in tmp_path.rglob("*.jpg"))
    assert left == ["6.jpg", "7.jpg", "8.jpg", "9.jpg"]  # the newest stay


def test_damaged_photo_metadata_goes_to_the_scan_log_with_the_path():
    import warnings

    from deedoop import engine

    st = Status()
    st.reset(job=1, type="scan", phase="hash_image")
    saved = warnings.showwarning, list(warnings.filters)
    try:
        engine._route_pillow_warnings()
        engine._reading.path, engine._reading.status = "/mnt/disk2/Photos/IMG_1.jpg", st
        for tag in (272, 513):
            warnings.warn_explicit(f"Tag data larger than file. Skipping tag {tag}", UserWarning,
                                   os.path.join(os.sep, "usr", "lib", "PIL", "TiffImagePlugin.py"),
                                   946, module="PIL.TiffImagePlugin")
        lines = [m for _, level, m in st.recent_log(10) if level == "warn"]
        assert lines == ["/mnt/disk2/Photos/IMG_1.jpg: damaged metadata (Tag data larger than file. Skipping tag 272); "
                         "the picture itself was read"]  # once per file
    finally:
        engine._reading.path = engine._reading.status = None
        warnings.showwarning = saved[0]
        warnings.filters[:] = saved[1]

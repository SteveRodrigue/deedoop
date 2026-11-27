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
from deedoop.control import Status, progress_view
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

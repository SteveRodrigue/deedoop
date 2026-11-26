"""Milestone 7.5: scan progress (phase x of y, ETA), the scan log and built-in HTTPS."""

import datetime as dt
import os
import time

import pytest

from deedoop import db, scanners, tls
from deedoop.config import AppConfig
from deedoop.control import Status, fmt_seconds, progress_view
from tests.conftest import scan, write
from tests.test_web import make_client, wait_idle

# -- progress --------------------------------------------------------------------------


def test_plan_follows_the_scanners_that_are_on():
    s = dict(db.DEFAULT_SETTINGS)
    s["scanners"] = {k: False for k in scanners.REGISTRY}
    assert scanners.plan(s) == ["walk", "hash_partial", "hash_full", "group"]
    s["scanners"] = {"photo": True, "video": False, "audio": False, "archive": True}
    s["image_matching"], s["similar_matching"], s["metadata"] = True, False, False
    plan = scanners.plan(s)
    assert plan[:2] == ["walk", "hash_image"] and "hash_similar" not in plan
    assert plan.index("archives") < plan.index("hash_partial") and plan[-1] == "group"


def test_progress_view_phase_x_of_y_and_eta():
    live = {"plan": ["walk", "hash_partial", "hash_full", "group"], "phase": "hash_full",
            "active": 100.0, "phase_active": 40.0,
            "disks": {"disk1": {"todo_bytes": 1000, "done_bytes": 200}, "disk2": {"todo_bytes": 1000, "done_bytes": 300}}}
    v = progress_view(live, time.time())
    assert (v["phase_no"], v["phase_count"], v["pct"], v["unit"]) == (3, 4, 25.0, "bytes")
    assert v["eta"] == "about 3 min left"  # 60 s for a quarter: 180 s for the rest
    assert [s["state"] for s in v["segments"]] == ["done", "done", "now", "todo"]
    # No ETA too early, and none while paused.
    assert progress_view({**live, "phase_active": 95.0}, 0)["eta"] == ""
    assert progress_view({**live, "paused_since": 1.0}, 0)["eta"] == "paused"
    # Similar-photo pairs are part of building the groups.
    assert progress_view({**live, "phase": "similar", "disks": {}}, 0)["phase_no"] == 4


def test_walk_eta_uses_the_last_scans_count():
    live = {"plan": ["walk", "group"], "phase": "walk", "active": 30.0, "phase_active": 0.0,
            "disks": {"disk1": {"walked": 1000}}}
    v = progress_view(live, 0, last_walked=4000)
    assert v["todo"] == 4000 and v["rough"] and v["eta"] == "about 1 min left"
    assert progress_view(live, 0)["todo"] == 0  # first scan: no total, no ETA


def test_eta_leaves_out_paused_time(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    st = Status()
    st.reset(job=1, phase="starting")
    st.set(phase="hash_full")
    clock[0] += 10
    st.paused(True)
    clock[0] += 3600
    st.paused(False)
    clock[0] += 10
    snap = st.snapshot()
    assert snap["active"] - snap["phase_active"] == pytest.approx(20)


def test_new_phase_starts_its_own_counts():
    st = Status()
    st.reset(job=1, phase="starting")
    st.set(phase="hash_partial")
    st.disk("disk1", todo_files=10, done_files=10, walked=50)
    st.set(phase="group")
    d = st.snapshot()["disks"]["disk1"]
    assert "todo_files" not in d and d["walked"] == 50


def test_fmt_seconds():
    assert [fmt_seconds(s) for s in (5, 125, 7500)] == ["5 s", "2 min", "2 h 05 min"]


# -- the scan log ------------------------------------------------------------------------


def test_scan_log_records_phases_and_unreadable_files(unraid, monkeypatch):
    m = unraid.mnt
    data = os.urandom(100_000)
    write(m / "disk1/Photos/a.bin", data)
    write(m / "disk2/Photos/b.bin", data)
    locked = m / "disk2/Photos/locked"
    locked.mkdir()
    write(locked / "x.bin", b"x")
    c = db.connect(unraid.db_path)
    db.set_settings(c, {"shares": ["Photos"]})
    c.close()
    os.chmod(locked, 0)
    try:
        with make_client(unraid) as client:
            client.post("/scan/start")
            wait_idle(client)
            r = client.get("/log")
            assert r.status_code == 200
            text = r.text
            assert "Walking folders" in text and "Full hashing likely duplicates: finished in" in text
            assert "Finished in" in text and "1 exact groups" in text
            if os.geteuid() != 0:  # root reads everything
                assert "cannot open folder" in text
                assert "cannot open folder" in client.get("/log?level=warn").text
                assert "Walking folders" not in client.get("/log?level=warn").text
            # the status card links to it
            assert "/log?job=" in client.get("/").text
    finally:
        os.chmod(locked, 0o700)


def test_scan_log_lines_are_one_line_and_bounded():
    st = Status()
    st.reset(job=1)
    st.log("warn", "/mnt/disk1/evil\nname\x1b[31m.jpg: cannot read")
    (_, level, msg), = st.drain_log()
    assert level == "warn" and "\n" not in msg and "\x1b" not in msg
    for i in range(2000):
        st.log("info", f"line {i}")
    assert len(st.drain_log()) < 1000 and st.snapshot()["log_dropped"] > 0




def test_old_logs_are_pruned(conn):
    for _ in range(db.LOG_KEEP_JOBS + 5):
        jid = db.enqueue_job(conn, "scan")
        db.add_log(conn, jid, [(time.time(), "info", "hello")])
    db.prune_logs(conn)
    assert conn.execute("SELECT COUNT(DISTINCT job_id) FROM scan_log").fetchone()[0] == db.LOG_KEEP_JOBS


def test_engine_logs_through_status(unraid):
    write(unraid.mnt / "disk1/Photos/a.bin", b"a" * 5000)
    write(unraid.mnt / "disk2/Photos/a.bin", b"a" * 5000)
    c = db.connect(unraid.db_path)
    db.set_settings(c, {"shares": ["Photos"]})
    c.close()
    scan(unraid)  # a scan without the worker (and its status reset) must not fail on logging


# -- HTTPS -------------------------------------------------------------------------------


def _cfg(tmp_path, **kw) -> AppConfig:
    base = {"config_dir": tmp_path / "config", "mnt": tmp_path / "mnt", "https": True,
            "tls_cert": tmp_path / "config/tls/cert.pem", "tls_key": tmp_path / "config/tls/key.pem"}
    return AppConfig(**{**base, **kw})


def test_https_off_by_default(tmp_path):
    assert tls.prepare(_cfg(tmp_path, https=False)).enabled is False
    assert not (tmp_path / "config/tls").exists()


def test_self_signed_certificate_is_made_once_and_reused(tmp_path):
    cfg = _cfg(tmp_path, tls_names=("tower.local", "192.168.1.10", "bad name!"))
    info = tls.prepare(cfg)
    assert info.enabled and info.source == "self-signed" and not (tmp_path / "config/tls/cert.pem").exists()
    assert {"tower.local", "192.168.1.10", "localhost"} <= set(info.names)
    assert any("bad name!" in p for p in info.problems)
    assert 360 < info.days_left < 400 and len(info.fingerprint) == 95
    again = tls.prepare(cfg)
    assert again.fingerprint == info.fingerprint  # same certificate after a restart
    # a new name makes a new certificate
    assert tls.prepare(_cfg(tmp_path, tls_names=("nas.lan",))).fingerprint != info.fingerprint




def test_self_signed_is_renewed_near_expiry(tmp_path):
    cfg = _cfg(tmp_path)
    tls_dir = tmp_path / "config/tls"
    tls_dir.mkdir(parents=True, mode=0o700)
    names, _ = tls.wanted_names(())
    tls.make_self_signed(tls_dir / "self-signed.crt", tls_dir / "self-signed.key", names, days=10)
    old = tls.describe(tls_dir / "self-signed.crt")[0]
    assert tls.prepare(cfg).fingerprint != old


def test_your_certificate_wins_and_a_broken_one_falls_back(tmp_path):
    cfg = _cfg(tmp_path)
    tls.make_self_signed(cfg.tls_cert, cfg.tls_key, ["nas.example.com"])
    info = tls.prepare(cfg)
    assert info.source == "yours" and info.names == ["nas.example.com"] and not info.problems
    os.chmod(cfg.tls_key, 0o640)
    assert any("chmod 600" in p for p in tls.prepare(cfg).problems)
    cfg.tls_key.write_text("not a key")
    info = tls.prepare(cfg)
    assert info.source == "self-signed" and any("cannot be used" in p for p in info.problems)
    cfg.tls_key.unlink()
    info = tls.prepare(cfg)
    assert info.source == "self-signed" and any("key.pem is missing" in p for p in info.problems)


def test_expired_certificate_is_flagged(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    tls.make_self_signed(cfg.tls_cert, cfg.tls_key, ["nas.example.com"], days=5)
    future = dt.datetime.now(dt.UTC) + dt.timedelta(days=10)
    real = tls.describe

    def later(cert):
        fp, _, names = real(cert)
        return fp, future - dt.timedelta(days=20), names

    monkeypatch.setattr(tls, "describe", later)
    assert any("expired" in p for p in tls.prepare(cfg).problems)




def test_setup_shows_https_state(unraid):
    with make_client(unraid) as client:
        r = client.get("/setup")
        assert 'id="https"' in r.text and "DEEDOOP_HTTPS" in r.text


def test_setup_shows_the_certificate_fingerprint(unraid, tmp_path):
    from deedoop.web.app import create_app
    from tests.conftest import ensure_admin
    from tests.test_web import SignedInClient

    info = tls.prepare(_cfg(tmp_path))
    ensure_admin(unraid)
    with SignedInClient(create_app(unraid, start_worker=False, tls_info=info), unraid) as client:
        r = client.get("/setup")
        assert info.fingerprint in r.text and "self-signed certificate" in r.text

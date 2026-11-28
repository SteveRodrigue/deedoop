"""The web UI answers at once after an update, even while the database is being
upgraded (found live: a 6.7 GB write-ahead log and new indexes on ~2M files kept
the server from listening for many minutes, and deedoop looked dead)."""

import logging
import os
import sqlite3
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from deedoop import db
from deedoop.web.startup import StartupGate


def test_waiting_page_until_the_database_is_ready_then_the_app():
    release, stopped = threading.Event(), []
    inner = FastAPI()

    @inner.get("/")
    def home():
        return {"ok": True}

    def prepare():
        release.wait(10)
        return inner

    gate = StartupGate(prepare, shutdown=lambda: stopped.append(True))
    with TestClient(gate) as client:
        r = client.get("/")
        assert r.status_code == 503 and "upgrading its database" in r.text
        assert 'http-equiv="refresh"' in r.text and "<script" not in r.text
        assert r.headers["content-security-policy"].startswith("default-src 'none'")
        assert client.get("/healthz").status_code == 200  # Docker: starting, not unhealthy
        assert client.post("/scan/start").status_code == 503  # nothing reaches the app yet
        logging.getLogger("deedoop.db").warning("Upgrading the database from version 10 to 11 <b>")
        logging.getLogger("deedoop.auth").warning("setup code: ABCD")  # never on this page (SC8)
        page = client.get("/").text
        assert "Now: Upgrading the database from version 10 to 11 &lt;b&gt;" in page and "ABCD" not in page
        release.set()
        assert gate.ready.wait(10)
        assert client.get("/").json() == {"ok": True}
    assert stopped == [True]


def test_a_failed_start_says_so_on_healthz():
    def prepare():
        raise sqlite3.DatabaseError("file is not a database")

    gate = StartupGate(prepare)
    with TestClient(gate) as client:
        assert gate.ready.wait(10)
        assert client.get("/healthz").status_code == 500
        assert client.get("/").status_code == 503


def test_init_folds_a_large_write_ahead_log_in_and_logs_each_step(tmp_path, caplog, monkeypatch):
    path = tmp_path / "deedoop.db"
    db.init(path)
    c = db.connect(path)
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("CREATE TABLE junk (x BLOB)")
    c.execute("BEGIN")
    c.executemany("INSERT INTO junk VALUES (?)", [(os.urandom(4000),) for _ in range(1000)])
    c.execute("COMMIT")
    monkeypatch.setattr(db, "WAL_SIZE_LIMIT", 1 << 20)
    assert os.path.getsize(str(path) + "-wal") > 1 << 20
    # keep this connection open, as a crashed container would leave the log behind
    with caplog.at_level(logging.WARNING, logger="deedoop.db"):
        c.execute("DROP INDEX files_root_state")  # one upgrade still to run
        c.execute("DROP INDEX files_errors")
        c.execute("PRAGMA user_version=10")
        db.init(path)
    text = caplog.text
    assert "Folding the" in text and "Write-ahead log folded in" in text
    assert "Upgrading the database from version 10" in text
    assert os.path.getsize(str(path) + "-wal") < 1 << 20
    c.close()

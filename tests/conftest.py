import os
from pathlib import Path

import pytest

from deedoop import db
from deedoop.config import AppConfig
from deedoop.control import Control, Status
from deedoop.engine import ScanContext, run_scan


def write(p: Path, data: bytes, mtime: float | None = None) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


@pytest.fixture
def unraid(tmp_path):
    """A fake Unraid /mnt: disk1, disk2, a cache pool and /mnt/user."""
    mnt = tmp_path / "mnt"
    for d in ("disk1/Photos", "disk2/Photos", "cache/Docs", "disk2/Docs", "disk1/appdata",
              "user/Photos", "user/Docs", "user/appdata", "user0", "disks/usb/Backup"):
        (mnt / d).mkdir(parents=True)
    cfg = AppConfig(config_dir=tmp_path / "config", mnt=mnt)
    db.init(cfg.db_path)
    return cfg


@pytest.fixture
def conn(unraid):
    c = db.connect(unraid.db_path)
    yield c
    c.close()


def scan(cfg: AppConfig, job_id: int | None = None, control: Control | None = None) -> dict:
    c = db.connect(cfg.db_path)
    try:
        jid = job_id or db.enqueue_job(c, "scan")
    finally:
        c.close()
    return run_scan(ScanContext(cfg.db_path, cfg.mnt, jid, control or Control(), Status()))


# -- signed-in web clients (milestone 6.5) ----------------------------------------

TEST_USER = "admin"
TEST_PASSWORD = "correct horse battery staple"


def ensure_admin(cfg: AppConfig) -> None:
    from deedoop import auth

    c = db.connect(cfg.db_path)
    try:
        if not auth.has_admin(c):
            auth.create_admin(c, TEST_USER, TEST_PASSWORD)
    finally:
        c.close()


def sign_in(client, cfg: AppConfig, password: str = TEST_PASSWORD) -> None:
    """Sign the TestClient in and send the session's CSRF token on every request."""
    r = client.post("/login", data={"username": TEST_USER, "password": password}, follow_redirects=False)
    assert r.status_code == 303 and "error" not in r.headers["location"], r.headers.get("location")
    c = db.connect(cfg.db_path)
    try:
        csrf = c.execute("SELECT csrf FROM sessions ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    finally:
        c.close()
    client.headers["X-CSRF-Token"] = csrf

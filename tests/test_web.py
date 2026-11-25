import csv
import io
import json
import os
import time

from fastapi.testclient import TestClient

from deedup import db
from deedup.benchmark import BenchConfig
from deedup.config import AppConfig
from deedup.web.app import create_app
from deedup.worker import Worker, in_window
from tests.conftest import write


def wait_idle(client, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get("/api/status").json()
        if s["active"] is None:
            return s
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def make_client(cfg):
    worker = Worker(cfg, BenchConfig(seconds_per_test=0.2, bytes_per_test=8 << 20, min_sample_file=1 << 20))
    return TestClient(create_app(cfg, worker=worker))


def test_end_to_end_setup_scan_report_export(unraid):
    m = unraid.mnt
    data = os.urandom(250_000)
    write(m / "disk1/Photos/IMG_1.jpg", data)
    write(m / "disk2/Photos/old backup/IMG_1 (1).jpg", data)
    with make_client(unraid) as client:
        r = client.get("/setup")
        assert r.status_code == 200
        assert "Photos" in r.text and "disk1, disk2" in r.text

        r = client.post("/setup/shares", data={"share": ["Photos"]}, follow_redirects=True)
        assert r.status_code == 200 and "checked" in r.text

        client.post("/scan/start")
        wait_idle(client)

        r = client.get("/")
        assert "250.0 KB" in r.text  # reclaimable

        r = client.get("/duplicates")
        assert "IMG_1 (1).jpg" in r.text and "/user/Photos/IMG_1.jpg" in r.text

        r = client.get("/duplicates?category=video")
        assert "No duplicate groups" in r.text

        rows = list(csv.DictReader(io.StringIO(client.get("/export/duplicates.csv").text)))
        assert len(rows) == 2 and {r["disk"] for r in rows} == {"disk1", "disk2"}
        assert rows[0]["user_path"].startswith(str(m / "user/Photos"))

        doc = json.loads(client.get("/export/duplicates.json").text)
        assert len(doc["groups"]) == 1 and len(doc["groups"][0]["files"]) == 2

        assert client.get("/same-path").status_code == 200


def test_settings_and_custom_folder(unraid):
    with make_client(unraid) as client:
        r = client.post("/setup/settings", data={
            "exclusions": "*.tmp\n  \n@eaDir", "takeout_paths": "/mnt/user/Takeout", "min_size": "1024",
            "disk_mode": "parallel", "parallel_disks": "3", "hash_threads": "1",
            "schedule_enabled": "1", "schedule_start": "01:00", "schedule_end": "07:00"}, follow_redirects=True)
        assert r.status_code == 200
        c = db.connect(unraid.db_path)
        s = db.get_settings(c)
        assert s["exclusions"] == ["*.tmp", "@eaDir"] and s["disk_mode"] == "parallel" and s["min_size"] == 1024
        assert s["schedule_enabled"] is True and s["takeout_paths"] == ["/mnt/user/Takeout"]

        r = client.post("/setup/custom", data={"path": "/etc"}, follow_redirects=True)
        assert "must be inside" in r.text
        r = client.post("/setup/custom", data={"path": str(unraid.mnt / "disks/usb/Backup")}, follow_redirects=True)
        assert "disks/usb/Backup" in r.text
        rid = c.execute("SELECT id FROM roots").fetchone()[0]
        client.post(f"/setup/root/{rid}/remove")
        assert c.execute("SELECT COUNT(*) FROM roots").fetchone()[0] == 0
        c.close()


def test_benchmark_job_and_apply(unraid):
    m = unraid.mnt
    for d in ("disk1/Photos", "disk2/Photos"):
        write(m / d / "big.bin", os.urandom(2 << 20))
    with make_client(unraid) as client:
        client.post("/setup/shares", data={"share": ["Photos"]})
        client.post("/setup/benchmark")
        wait_idle(client)
        r = client.get("/setup")
        assert "Proposal" in r.text and "disk1" in r.text
        client.post("/setup/benchmark/apply")
        c = db.connect(unraid.db_path)
        bench = json.loads(c.execute("SELECT result FROM benchmarks").fetchone()[0])
        assert {s["disk"] for s in bench["solo"]} == {"disk1", "disk2"}
        assert len(bench["combos"]) == 1
        assert db.get_settings(c)["disk_mode"] == bench["proposal"]["disk_mode"]
        c.close()


def test_basic_auth(tmp_path):
    (tmp_path / "mnt/user").mkdir(parents=True)
    cfg = AppConfig(config_dir=tmp_path / "cfg", mnt=tmp_path / "mnt", password="s3cret")
    with TestClient(create_app(cfg, start_worker=False)) as client:
        assert client.get("/").status_code == 401
        assert client.get("/healthz").status_code == 200
        assert client.get("/", auth=("deedup", "s3cret")).status_code == 200


def test_schedule_window():
    import datetime as dt

    at = lambda h, m=0: dt.datetime(2026, 1, 1, h, m)
    assert in_window(at(2), "01:00", "07:00")
    assert not in_window(at(8), "01:00", "07:00")
    assert in_window(at(23, 30), "22:00", "06:00") and in_window(at(3), "22:00", "06:00")
    assert not in_window(at(12), "22:00", "06:00")

"""Benchmark proposal: current vs proposed Disk mode, what Apply changed, and the
suggestion to run the benchmark when it never ran."""

import json

from deedoop import benchmark, db
from tests.test_web import make_client


def add_bench(path, proposal, created_at=1_700_000_000.0):
    c = db.connect(path)
    result = {"created_at": created_at, "cpus": 4, "hash_mbps_1_thread": 900, "hash_mbps_all_threads": 3000,
              "solo": [{"disk": "disk1", "mbps": 180}, {"disk": "disk2", "mbps": 175}],
              "combos": [{"disks": ["disk1", "disk2"], "aggregate_mbps": 350, "sum_of_solo_mbps": 355,
                          "efficiency": 0.99}],
              "skipped": [], "proposal": proposal, "note": "2 disks read together kept their speed."}
    c.execute("INSERT INTO benchmarks(created_at, result) VALUES (?, ?)", (created_at, json.dumps(result)))
    c.close()


PARALLEL = {"disk_mode": "parallel", "parallel_disks": 2, "hash_threads": 1}


def test_compare_shows_the_disks_actually_read():
    rows = benchmark.compare({"disk_mode": "single", "parallel_disks": 6, "hash_threads": 1}, PARALLEL)
    assert [(r["label"], r["current"], r["proposed"], r["changed"]) for r in rows] == [
        ("Disk mode", "One disk at a time", "Parallel", True),
        ("Disks read at once", 1, 2, True),          # the stored 6 is not used in single mode
        ("Hash threads per large file", 1, 1, False)]


def test_single_proposal_keeps_the_saved_parallel_count():
    assert benchmark.proposal_settings({"disk_mode": "single", "parallel_disks": 2, "hash_threads": 1}) == {
        "disk_mode": "single", "hash_threads": 1}


def test_setup_shows_now_and_proposed_then_what_apply_changed(unraid):
    add_bench(unraid.db_path, PARALLEL)
    with make_client(unraid) as client:
        page = client.get("/setup").text
        assert 'class="compare"' in page and "Compare and apply" in page
        assert "One disk at a time</td>" in page and '<span class="differs">Parallel</span>' in page
        assert "Apply 2 changes" in page
        r = client.post("/setup/benchmark/apply", follow_redirects=True)
        assert ("Proposal applied. Disk mode: One disk at a time → Parallel; Disks read at once: 1 → 2."
                in r.text)
        assert "Apply 2 changes" not in r.text and "Your Disk mode already uses these settings" in r.text
        c = db.connect(unraid.db_path)
        s = db.get_settings(c)
        c.close()
        assert (s["disk_mode"], s["parallel_disks"], s["hash_threads"]) == ("parallel", 2, 1)
        # Applying again changes nothing and says so.
        r = client.post("/setup/benchmark/apply", follow_redirects=True)
        assert "Nothing changed: Disk mode already matched the proposal." in r.text


def test_apply_without_a_benchmark_is_an_error(unraid):
    with make_client(unraid) as client:
        r = client.post("/setup/benchmark/apply", follow_redirects=True)
        assert "Run the benchmark first." in r.text and 'data-toast="err"' in r.text


def test_never_benchmarked_is_suggested_with_a_shortcut(unraid):
    with make_client(unraid) as client:
        assert 'id="bench-hint"' not in client.get("/").text  # nothing selected yet: the welcome card instead
        client.post("/setup/shares", data={"share": ["Photos"]})
        dash = client.get("/").text
        assert 'id="bench-hint"' in dash and 'action="/setup/benchmark"' in dash
        setup = client.get("/setup").text
        assert "Not benchmarked yet." in setup and 'form="bench-run"' in setup and 'id="bench-run"' in setup
        client.post("/setup/benchmark/dismiss")
        assert 'id="bench-hint"' not in client.get("/").text
        assert "Not benchmarked yet." in client.get("/setup").text  # Setup still says so


def test_hint_goes_once_a_benchmark_exists(unraid):
    add_bench(unraid.db_path, PARALLEL)
    with make_client(unraid) as client:
        client.post("/setup/shares", data={"share": ["Photos"]})
        assert 'id="bench-hint"' not in client.get("/").text


def test_benchmark_refuses_to_start_during_a_scan(unraid):
    with make_client(unraid) as client:
        c = db.connect(unraid.db_path)
        c.execute("INSERT INTO jobs(type, params, state, created_at) VALUES ('scan', '{}', 'running', 0)")
        r = client.post("/setup/benchmark", follow_redirects=True)
        assert "cannot run while a scan" in r.text and 'data-toast="err"' in r.text
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE type = 'benchmark'").fetchone()[0] == 0
        page = client.get("/setup").text
        assert "run it when the current scan finishes" in page
        c.execute("UPDATE jobs SET state = 'done'")
        c.close()

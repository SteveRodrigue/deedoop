"""Background worker: runs queued jobs one at a time and the nightly schedule.

The worker shares the process with the web server but does all disk
reading itself; the web UI only reads the index, queues jobs and flips the
pause/stop switches.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
import traceback

from . import changes, db
from .benchmark import BenchConfig, disk_paths_for_benchmark, run_benchmark
from .config import AppConfig
from .control import Control, Status, Stopped
from .discovery import list_disks
from .engine import ScanContext, regroup, run_scan

log = logging.getLogger("deedup.worker")

PERSIST_SECONDS = 5.0


def _parse_hhmm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def in_window(now: dt.datetime, start: str, end: str) -> bool:
    """True when ``now`` lies in the daily window [start, end), which may wrap midnight."""
    s, e, t = _parse_hhmm(start), _parse_hhmm(end), now.time()
    if s == e:
        return True
    return s <= t < e if s < e else (t >= s or t < e)


class Worker:
    def __init__(self, cfg: AppConfig, bench_cfg: BenchConfig | None = None):
        self.cfg = cfg
        self.bench_cfg = bench_cfg or BenchConfig()
        self.status = Status()
        self.control = Control()
        self.current_job: int | None = None
        self._wake = threading.Event()
        self._quit = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_schedule_day: dt.date | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        conn = db.connect(self.cfg.db_path)
        try:
            # A job that was running when the container stopped resumes.
            conn.execute("UPDATE jobs SET state = 'queued' WHERE state = 'running'")
        finally:
            conn.close()
        self._thread = threading.Thread(target=self._loop, name="deedup-worker", daemon=True)
        self._thread.start()

    def shutdown(self, timeout: float = 10) -> None:
        self._quit.set()
        if self.current_job is not None:
            self.control.stop("shutdown")
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def wake(self) -> None:
        self._wake.set()

    # -- controls -----------------------------------------------------------

    def pause(self) -> None:
        self.control.pause()

    def resume(self) -> None:
        self.control.resume()

    def stop(self) -> None:
        self.control.stop("stopped by user")

    # -- loop ---------------------------------------------------------------

    def _loop(self) -> None:
        while not self._quit.is_set():
            try:
                self._check_schedule()
                self._check_purges()
                ran = self._run_next()
            except Exception:  # never let the worker die
                log.exception("worker loop error")
                ran = False
            if not ran:
                self._wake.wait(15)
                self._wake.clear()

    _last_purge_check = 0.0

    def _check_purges(self) -> None:
        """Hourly: queue the deletion of quarantines older than the setting."""
        if time.time() - self._last_purge_check < 3600:
            return
        self._last_purge_check = time.time()
        conn = db.connect(self.cfg.db_path)
        try:
            if changes.gate(conn, self.cfg.mnt)["open"]:
                changes.enqueue_due_purges(conn, int(db.get_settings(conn)["quarantine_days"] or 0))
        finally:
            conn.close()

    def _check_schedule(self) -> None:
        conn = db.connect(self.cfg.db_path)
        try:
            s = db.get_settings(conn)
            if not s["schedule_enabled"]:
                return
            now = dt.datetime.now()
            inside = in_window(now, s["schedule_start"], s["schedule_end"])
            if self.current_job is not None and not inside and self._job_is_scheduled:
                self.control.stop("schedule window ended")
                return
            if inside and self._last_schedule_day != now.date() and db.active_job(conn) is None:
                self._last_schedule_day = now.date()
                db.enqueue_job(conn, "scan", {"scheduled": True})
        finally:
            conn.close()

    _job_is_scheduled = False

    def _run_next(self) -> bool:
        conn = db.connect(self.cfg.db_path)
        try:
            job = conn.execute("SELECT * FROM jobs WHERE state = 'queued' ORDER BY id LIMIT 1").fetchone()
            if job is None:
                return False
            params = json.loads(job["params"])
            self.control = Control()
            self.current_job = job["id"]
            self._job_is_scheduled = bool(params.get("scheduled"))
            self.status.reset(job=job["id"], type=job["type"], phase="starting")
            conn.execute(
                "UPDATE jobs SET state = 'running', started_at = COALESCE(started_at, ?), error = NULL "
                "WHERE id = ?",
                (time.time(), job["id"]),
            )
            stop_persist = threading.Event()
            persister = threading.Thread(target=self._persist, args=(job["id"], stop_persist), daemon=True)
            persister.start()
            state, error, result = "done", None, {}
            try:
                if job["type"] == "scan":
                    result = run_scan(ScanContext(self.cfg.db_path, self.cfg.mnt, job["id"],
                                                  self.control, self.status))
                elif job["type"] == "benchmark":
                    result = self._benchmark(conn)
                elif job["type"] == "regroup":
                    # A scanner was turned on or off in Setup: rebuild the groups
                    # from the index without reading any file.
                    self.status.set(phase="group")
                    result = regroup(conn, db.get_settings(conn))
                elif job["type"] in ("action", "undo", "purge"):
                    run = {"action": changes.run_action, "undo": changes.run_undo,
                           "purge": changes.run_purge}[job["type"]]
                    result = run(changes.RunContext(self.cfg.db_path, self.cfg.mnt, params["action_id"],
                                                    self.control, self.status))
                else:
                    raise ValueError(f"unknown job type {job['type']}")
            except Stopped as e:
                state, error = ("queued", None) if str(e) == "shutdown" else ("stopped", str(e))
            except Exception as e:
                log.exception("job %s failed", job["id"])
                state, error = "failed", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}"
            finally:
                stop_persist.set()
                persister.join(2)
            snap = self.status.snapshot()
            snap["result"] = result
            conn.execute(
                "UPDATE jobs SET state = ?, error = ?, progress = ?, finished_at = ? WHERE id = ?",
                (state, error, json.dumps(snap), None if state == "queued" else time.time(), job["id"]),
            )
            return state != "queued"
        finally:
            self.current_job = None
            self._job_is_scheduled = False
            conn.close()

    def _persist(self, job_id: int, stop: threading.Event) -> None:
        conn = db.connect(self.cfg.db_path)
        try:
            while not stop.wait(PERSIST_SECONDS):
                conn.execute("UPDATE jobs SET progress = ? WHERE id = ?",
                             (json.dumps(self.status.snapshot()), job_id))
        except Exception:
            log.exception("progress persist failed")
        finally:
            conn.close()

    def _benchmark(self, conn) -> dict:
        paths = disk_paths_for_benchmark(conn, list_disks(self.cfg.mnt))
        result = run_benchmark(paths, self.bench_cfg, self.control,
                               progress=lambda **kw: self.status.set(**kw))
        conn.execute("INSERT INTO benchmarks(created_at, result) VALUES (?, ?)",
                     (result["created_at"], json.dumps(result)))
        return {"benchmark": True}

"""Background worker: runs queued jobs one at a time and the nightly schedule.

The worker shares the process with the web server but does all disk
reading itself; the web UI only reads the index, queues jobs and flips the
pause/stop switches.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import multiprocessing
import os
import threading
import time
import traceback
from pathlib import Path

from . import changes, db
from .benchmark import BenchConfig, disk_paths_for_benchmark, run_benchmark
from .config import AppConfig
from .control import Control, JobProblem, Status, Stopped, fmt_seconds
from .discovery import list_disks
from .engine import ScanContext, regroup, run_scan

log = logging.getLogger("deedoop.worker")

PERSIST_SECONDS = 5.0
WAL_SHRINK_SECONDS = 60.0
THUMB_CACHE_MAX = 1 << 30   # thumbnails in /config/thumbs; the oldest go first beyond this


def _parse_hhmm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def in_window(now: dt.datetime, start: str, end: str) -> bool:
    """True when ``now`` lies in the daily window [start, end), which may wrap midnight."""
    s, e, t = _parse_hhmm(start), _parse_hhmm(end), now.time()
    if s == e:
        return True
    return s <= t < e if s < e else (t >= s or t < e)


JOB_NAMES = {"scan": "Scan", "benchmark": "Benchmark", "action": "Change", "undo": "Undo",
             "purge": "Emptying quarantine", "regroup": "Regrouping"}


def _outcome(state: str, error: str | None, result: dict, snap: dict) -> tuple[str, str]:
    took = fmt_seconds(snap.get("active", 0.0))
    if state == "done":
        bits = []
        if "groups" in result:
            bits.append(f"{result['groups']:,} exact groups")
        if "image_groups" in result:
            bits.append(f"{result['image_groups']:,} photos with other metadata")
        if "media_groups" in result:
            bits.append(f"{result['media_groups']:,} recordings with other tags")
        if snap.get("log_dropped"):
            bits.append(f"{snap['log_dropped']:,} more log lines not kept")
        return "info", f"Finished in {took}" + (": " + ", ".join(bits) if bits else "")
    if state == "queued":
        return "info", f"Interrupted by a restart after {took}; carries on when deedoop starts"
    if state == "stopped":
        return "warn", f"Stopped after {took}: {error}"
    return "error", f"Failed after {took}: {(error or '').splitlines()[0] if error else 'unknown error'}"


class JobLogHandler(logging.Handler):
    """Copies warnings from the scan modules into the scan log of the running
    job. Only these modules: the web and sign-in modules stay out, so the log
    never holds a setup code or a user name (SC8)."""

    SOURCES = ("deedoop.engine", "deedoop.scanner", "deedoop.archive_index", "deedoop.media_index",
               "deedoop.metadata", "deedoop.av", "deedoop.changes")

    def __init__(self, status: Status):
        super().__init__(logging.WARNING)
        self.status = status

    def emit(self, record: logging.LogRecord) -> None:
        if record.name not in self.SOURCES:
            return
        try:
            self.status.log("error" if record.levelno >= logging.ERROR else "warn", record.getMessage())
        except Exception:  # noqa: BLE001 - logging must never break a job
            self.handleError(record)


def prune_thumbnails(folder, limit: int) -> int:
    """Keep the thumbnail cache under ``limit`` bytes, removing the files used
    longest ago (their modification time: a thumbnail is written once, then
    served from the cache). Returns the bytes removed."""
    files = []
    for dirpath, _, names in os.walk(folder):
        for n in names:
            p = os.path.join(dirpath, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
    total = sum(f[1] for f in files)
    removed = 0
    for _, size, p in sorted(files):
        if total - removed <= limit:
            break
        try:
            Path(p).unlink(missing_ok=True)  # SC2 covers /mnt; this is deedoop's own cache in /config
            removed += size
        except OSError:
            continue
    return removed


def execute(cfg: AppConfig, bench_cfg: BenchConfig, job: dict, params: dict, control: Control,
            status: Status) -> tuple[str, str | None, dict]:
    """Run one job; returns (state, error, result). Called in the job's own
    process (see :func:`_job_process`), or in the worker thread in tests."""
    conn = db.connect(cfg.db_path)
    try:
        state, error, result = "done", None, {}
        try:
            if job["type"] == "scan":
                result = run_scan(ScanContext(cfg.db_path, cfg.mnt, job["id"], control, status))
            elif job["type"] == "benchmark":
                paths = disk_paths_for_benchmark(conn, list_disks(cfg.mnt))
                bench = run_benchmark(paths, bench_cfg, control, progress=lambda **kw: status.set(**kw))
                conn.execute("INSERT INTO benchmarks(created_at, result) VALUES (?, ?)",
                             (bench["created_at"], json.dumps(bench)))
                result = {"benchmark": True}
            elif job["type"] == "regroup":
                # A scanner was turned on or off in Setup: rebuild the groups
                # from the index without reading any file.
                status.set(phase="group")
                result = regroup(conn, db.get_settings(conn))
            elif job["type"] in ("action", "undo", "purge"):
                run = {"action": changes.run_action, "undo": changes.run_undo,
                       "purge": changes.run_purge}[job["type"]]
                result = run(changes.RunContext(cfg.db_path, cfg.mnt, params["action_id"], control, status))
            else:
                raise ValueError(f"unknown job type {job['type']}")
        except Stopped as e:
            state, error = ("queued", None) if str(e) == "shutdown" else ("stopped", str(e))
        except JobProblem as e:
            log.warning("job %s: %s", job["id"], e)
            state, error = "failed", str(e)
        except Exception as e:
            log.exception("job %s failed", job["id"])
            state, error = "failed", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}"
        return state, error, result
    finally:
        conn.close()


SEND_SECONDS = 0.5   # how often the job's process sends its progress to the web process
JOB_NICE = 10        # the job's process gives way to the web pages for CPU


def _job_process(cfg: AppConfig, bench_cfg: BenchConfig, job: dict, params: dict, control: Control,
                 start: dict, pipe) -> None:
    """The job's own process. A scan is mostly Python code walking folders and
    writing the index; in the web server's process it held the interpreter
    lock so often that every page waited for it. Here it can't, and at a
    lower CPU priority the pages come first."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        os.nice(JOB_NICE)
    except OSError:
        pass
    status = Status()
    status.mirror(start, [])
    logging.getLogger("deedoop").addHandler(JobLogHandler(status))
    done = threading.Event()
    sent = 0

    def send(final: tuple | None = None) -> None:
        nonlocal sent
        if control.paused != bool(status.raw().get("paused_since")):
            status.paused(control.paused)
        lines, sent = status.log_since(sent)
        pipe.send(("done", status.raw(), lines, final) if final else ("status", status.raw(), lines, None))

    def sender() -> None:
        while not done.wait(SEND_SECONDS):
            send()

    t = threading.Thread(target=sender, name="deedoop-job-progress", daemon=True)
    t.start()
    try:
        outcome = execute(cfg, bench_cfg, job, params, control, status)
    finally:
        done.set()
        t.join(2)
    send(outcome)
    pipe.close()


class Worker:
    def __init__(self, cfg: AppConfig, bench_cfg: BenchConfig | None = None, isolate: bool = False):
        """``isolate``: run each job in its own process (the app does; most
        tests run jobs in the worker thread, which is quicker to start)."""
        self.cfg = cfg
        self.bench_cfg = bench_cfg or BenchConfig()
        self.isolate = isolate
        self._mp = multiprocessing.get_context("spawn") if isolate else None
        self.status = Status()
        self.control = Control()
        self._log_handler = JobLogHandler(self.status)
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
        logging.getLogger("deedoop").addHandler(self._log_handler)
        self._thread = threading.Thread(target=self._loop, name="deedoop-worker", daemon=True)
        self._thread.start()

    def shutdown(self, timeout: float = 10) -> None:
        self._quit.set()
        if self.current_job is not None:
            self.control.stop("shutdown")
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
        logging.getLogger("deedoop").removeHandler(self._log_handler)

    def wake(self) -> None:
        self._wake.set()

    # -- controls -----------------------------------------------------------

    def pause(self) -> None:
        if self.current_job is not None and not self.control.paused:
            self.status.log("info", "Paused")
        self.control.pause()
        self.status.paused(True)

    def resume(self) -> None:
        if self.current_job is not None and self.control.paused:
            self.status.log("info", "Resumed")
        self.status.paused(False)
        self.control.resume()

    def stop(self) -> None:
        if self.current_job is not None:
            self.status.log("info", "Stop requested")
        self.status.paused(False)
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
        prune_thumbnails(self.cfg.config_dir / "thumbs", THUMB_CACHE_MAX)
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
                self.status.log("info", "Schedule window ended: stopping; the next window carries on")
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
            self.control = Control(self._mp)
            self.current_job = job["id"]
            self._job_is_scheduled = bool(params.get("scheduled"))
            self.status.reset(job=job["id"], type=job["type"], phase="starting")
            db.prune_logs(conn)
            self.status.log("info", f"{JOB_NAMES.get(job['type'], job['type'])} #{job['id']} "
                                    f"{'resumed' if job['started_at'] else 'started'}"
                                    f"{' (scheduled)' if params.get('scheduled') else ''}")
            conn.execute(
                "UPDATE jobs SET state = 'running', started_at = COALESCE(started_at, ?), error = NULL "
                "WHERE id = ?",
                (time.time(), job["id"]),
            )
            stop_persist = threading.Event()
            persister = threading.Thread(target=self._persist, args=(job["id"], stop_persist), daemon=True)
            persister.start()
            try:
                state, error, result = self._run_job(dict(job), params)
            finally:
                stop_persist.set()
                persister.join(2)
            self.status.set(phase="done")
            self.status.log(*_outcome(state, error, result, self.status.snapshot()))
            db.add_log(conn, job["id"], self.status.drain_log())
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
        last_shrink = time.monotonic()
        try:
            while not stop.wait(PERSIST_SECONDS):
                conn.execute("UPDATE jobs SET progress = ? WHERE id = ?",
                             (json.dumps(self.status.snapshot()), job_id))
                db.add_log(conn, job_id, self.status.drain_log())
                # A long scan writes constantly while pages read: the automatic
                # checkpoints rarely find the log free, so it kept growing.
                if time.monotonic() - last_shrink > WAL_SHRINK_SECONDS:
                    last_shrink = time.monotonic()
                    db.shrink_wal(conn)
        except Exception:
            log.exception("progress persist failed")
        finally:
            conn.close()

    def _run_job(self, job: dict, params: dict) -> tuple[str, str | None, dict]:
        if not self.isolate:
            return execute(self.cfg, self.bench_cfg, job, params, self.control, self.status)
        parent, child = self._mp.Pipe(duplex=False)
        proc = self._mp.Process(target=_job_process, name=f"deedoop-job-{job['id']}", daemon=True,
                                args=(self.cfg, self.bench_cfg, job, params, self.control, self.status.raw(), child))
        proc.start()
        child.close()
        outcome = None
        try:
            while outcome is None:
                if parent.poll(1.0):
                    try:
                        kind, data, lines, final = parent.recv()
                    except EOFError:
                        break
                    self.status.mirror(data, lines)
                    if kind == "done":
                        outcome = final
                elif not proc.is_alive():
                    break
        finally:
            proc.join(10)
            if proc.is_alive():
                proc.kill()
            parent.close()
        if outcome is None:
            # Killed (out of memory, say) or crashed without a word: never leave the job "running".
            log.error("job %s: its process ended unexpectedly (exit code %s)", job["id"], proc.exitcode)
            return "failed", (f"The job's process ended unexpectedly (exit code {proc.exitcode}), for example "
                              "because the container ran out of memory. See the container log."), {}
        return outcome

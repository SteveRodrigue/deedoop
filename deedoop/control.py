"""Pause/stop control and live progress shared by the worker and the web UI."""

from __future__ import annotations

import collections
import copy
import threading
import time
from typing import Any

LOG_LIMIT = 1000            # scan log entries kept per job; more are counted, not kept
LOG_MESSAGE_MAX = 500
RATE_WINDOW = 10.0          # seconds of samples behind the live files/s and MB/s


class Stopped(Exception):
    """Raised inside a job when the user stopped it or the schedule window ended."""


class JobProblem(Exception):
    """A job cannot run for a reason the user can fix (nothing selected, say).
    The worker records it as a failure with this message alone, no traceback,
    so the dashboard can show it as it is."""


class Control:
    """Pause and stop switches for a job. With a multiprocessing context the
    switches are shared with the job's own process (``worker.py`` runs jobs
    there, so a scan never slows the web pages down)."""

    def __init__(self, ctx: Any = None) -> None:
        make = ctx.Event if ctx is not None else threading.Event
        self._running = make()
        self._running.set()
        self._stop = make()
        self._reason = ctx.Array("c", 128) if ctx is not None else None
        self._local_reason = ""

    @property
    def stop_reason(self) -> str:
        return self._reason.value.decode("utf-8", "replace") if self._reason is not None else self._local_reason

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def stop(self, reason: str = "stopped") -> None:
        if self._reason is not None:
            self._reason.value = reason.encode()[:127]
        else:
            self._local_reason = reason
        self._stop.set()
        self._running.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def checkpoint(self) -> None:
        """Block while paused; raise :class:`Stopped` if a stop was requested."""
        if self._stop.is_set():
            raise Stopped(self.stop_reason)
        while not self._running.wait(0.5):
            if self._stop.is_set():
                raise Stopped(self.stop_reason)
        if self._stop.is_set():
            raise Stopped(self.stop_reason)


class Status:
    """Thread-safe progress tree, e.g.::

        {"job": 3, "phase": "hash_full", "disks": {"disk1": {...}}}

    It also keeps time: when the job and the current phase started, and how
    long the job was paused, so that an ETA counts working time only. And it
    collects the scan log, which the worker writes to the database.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._log: list[tuple[float, str, str]] = []
        self._flushed = 0
        self._samples: collections.deque[tuple[float, str, int, int]] = collections.deque(maxlen=64)

    # -- progress -----------------------------------------------------------

    def reset(self, **kw: Any) -> None:
        with self._lock:
            now = time.time()
            self._data = {"disks": {}, "started": now, "paused_total": 0.0, "paused_since": None,
                          "phase_started": now, "phase_active": 0.0, "log_dropped": 0, **kw}
            self._log, self._flushed = [], 0
            self._samples.clear()

    def _active(self, now: float) -> float:
        d = self._data
        paused = d.get("paused_total", 0.0) + (now - d["paused_since"] if d.get("paused_since") else 0.0)
        return max(0.0, now - d.get("started", now) - paused)

    def set(self, **kw: Any) -> None:
        with self._lock:
            phase = kw.get("phase")
            if phase is not None and phase != self._data.get("phase") and self._data:
                now = time.time()
                prev, began = self._data.get("phase"), self._data.get("phase_active", 0.0)
                if prev and prev != "starting":
                    took = self._active(now) - began
                    self._add_log(now, "info", f"{phase_name(prev)}: finished in {fmt_seconds(took)}")
                    # Kept with the job, so the next scan can say how long this phase took last time.
                    times = self._data.setdefault("phase_times", {})
                    times[prev] = round(times.get(prev, 0.0) + took, 1)
                if phase != "done":
                    self._add_log(now, "info", f"{phase_name(phase)}")
                kw = {**kw, "phase_started": now, "phase_active": self._active(now)}
                for d in self._data.get("disks", {}).values():  # the last phase's counts are not this one's
                    for k in ("todo_files", "todo_bytes", "done_files", "done_bytes"):
                        d.pop(k, None)
            self._data.update(kw)

    def disk(self, name: str, **kw: Any) -> None:
        with self._lock:
            self._data.setdefault("disks", {}).setdefault(name, {}).update(kw)

    def add(self, name: str, key: str, n: int) -> None:
        with self._lock:
            d = self._data.setdefault("disks", {}).setdefault(name, {})
            d[key] = d.get(key, 0) + n

    def paused(self, on: bool) -> None:
        """The job was paused (by the user or the schedule) or resumed."""
        with self._lock:
            if not self._data:
                return
            now, since = time.time(), self._data.get("paused_since")
            if on and not since:
                self._data["paused_since"] = now
            elif not on and since:
                self._data["paused_total"] = self._data.get("paused_total", 0.0) + now - since
                self._data["paused_since"] = None

    # -- a job in its own process (worker.py) ---------------------------------------

    def raw(self) -> dict[str, Any]:
        """The progress tree as it is, for the web process's copy."""
        with self._lock:
            return copy.deepcopy(self._data)

    def log_since(self, n: int) -> tuple[list[tuple[float, str, str]], int]:
        """Log lines added after the first ``n``, and the new count."""
        with self._lock:
            return list(self._log[n:]), len(self._log)

    def mirror(self, data: dict[str, Any], lines: list[tuple[float, str, str]]) -> None:
        """Take the progress and new log lines sent by the job's process."""
        with self._lock:
            if data:
                self._data = data
            for at, level, message in lines:
                self._add_log(at, level, message)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snap = copy.deepcopy(self._data)
            if snap:
                now = time.time()
                snap["active"] = self._active(now)
                snap["rate_files"], snap["rate_bytes"] = self._rates(snap, now)
            return snap

    def _rates(self, snap: dict[str, Any], now: float) -> tuple[float, float]:
        """Files and bytes per second in the current phase over the last
        :data:`RATE_WINDOW` seconds, so the dashboard shows the speed now,
        not the average since the phase began. 0 while paused."""
        phase = snap.get("phase") or ""
        disks = (snap.get("disks") or {}).values()
        if phase == "walk":
            files = sum(d.get("walked", 0) for d in disks)
            size = sum(d.get("walked_bytes", 0) for d in disks)
        else:
            files = sum(d.get("done_files", 0) for d in disks) or (snap.get("done") or 0)
            size = sum(d.get("done_bytes", 0) for d in disks)
        s = self._samples
        if s and s[-1][1] != phase:
            s.clear()
        if not s or now - s[-1][0] >= 0.25:
            s.append((now, phase, files, size))
        while len(s) > 2 and now - s[0][0] > RATE_WINDOW:
            s.popleft()
        if snap.get("paused_since") or len(s) < 2:
            return 0.0, 0.0
        t0, _, f0, b0 = s[0]
        span = now - t0
        if span < 0.5:
            return 0.0, 0.0
        return max(0.0, (files - f0) / span), max(0.0, (size - b0) / span)

    def clear(self) -> None:
        with self._lock:
            self._data = {}

    # -- scan log -----------------------------------------------------------

    def log(self, level: str, message: str) -> None:
        """Add a line to the job's scan log: ``info``, ``warn`` or ``error``."""
        with self._lock:
            if self._data:
                self._add_log(time.time(), level, message)

    def _add_log(self, now: float, level: str, message: str) -> None:
        if len(self._log) >= LOG_LIMIT:
            self._data["log_dropped"] = self._data.get("log_dropped", 0) + 1
            return
        # One line, no control characters (a file name may hold a line break).
        text = "".join(ch if ch.isprintable() else "?" for ch in " ".join(str(message).split()))
        self._log.append((now, level if level in ("info", "warn", "error") else "info", text[:LOG_MESSAGE_MAX]))

    def recent_log(self, n: int = 8) -> list[tuple[float, str, str]]:
        with self._lock:
            return list(self._log[-n:])

    def drain_log(self) -> list[tuple[float, str, str]]:
        """Lines not yet written to the database."""
        with self._lock:
            out = self._log[self._flushed:]
            self._flushed = len(self._log)
            return out


PHASE_NAMES = {
    "starting": "Starting", "walk": "Walking folders", "archives": "Opening archives",
    "sidecars": "Reading Takeout JSON", "hash_image": "Reading photos", "hash_similar": "Fingerprinting photos",
    "similar": "Finding similar photos", "metadata": "Reading metadata", "probe": "Reading video and audio headers",
    "hash_stream": "Hashing video and audio streams", "hash_partial": "Quick hashing same-size files",
    "hash_full": "Full hashing likely duplicates", "group": "Building duplicate groups", "done": "Finishing",
    "changes": "Changing files", "undo": "Undoing",
    "fp_audio": "Fingerprinting songs", "fp_video": "Fingerprinting videos",
    "media_pairs": "Finding re-encoded songs and videos",
}


def phase_name(phase: str) -> str:
    return PHASE_NAMES.get(phase, phase.replace("_", " ").capitalize())


def fmt_seconds(sec: float) -> str:
    """'45 s', '12 min', '2 h 05 min'."""
    s = int(round(max(0.0, sec)))
    if s < 60:
        return f"{s} s"
    h, m = divmod(s // 60, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def fmt_bytes(n: float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1000 or unit == "PB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} PB"


# -- what the status card shows ------------------------------------------------------

ETA_MIN_SECONDS = 15        # no ETA until a phase has run this long
ETA_LIVE_SECONDS = 3        # ... or this long, when the live speed is known
ETA_MIN_FRACTION = 0.01     # ... and done 1% of its work


def progress_view(live: dict, now: float, last_walked: int = 0, last_times: dict | None = None) -> dict:
    """Phase x of y, how far the current phase is, and a rough ETA for it.

    The ETA is the phase's working time so far (pauses left out) scaled by the
    work left: crude, but honest about being per phase. Later phases depend on
    what earlier ones find, so there is no ETA for the whole scan. While
    walking, the total is the number of files the last scan walked, if any.
    """
    plan: list[str] = live.get("plan") or []
    phase = live.get("phase") or ""
    key = phase if phase in plan else ("group" if phase in ("similar", "media_pairs", "done") and "group" in plan else "")
    out: dict[str, Any] = {"plan": [{"key": p, "name": phase_name(p)} for p in plan],
                           "phase_no": plan.index(key) + 1 if key else 0, "phase_count": len(plan),
                           "phase_name": phase_name(phase) if phase else "", "pct": None, "eta": "",
                           "rough": False, "unit": "files", "done": 0, "todo": 0,
                           "rate_files": live.get("rate_files", 0.0), "rate_bytes": live.get("rate_bytes", 0.0)}
    disks = (live.get("disks") or {}).values()
    if phase == "walk":
        done = sum(d.get("walked", 0) for d in disks)
        todo = last_walked if last_walked > done else 0
        out["rough"] = True
        out["walked_bytes"] = sum(d.get("walked_bytes", 0) for d in disks)
        out["walked_dirs"] = sum(d.get("walked_dirs", 0) for d in disks)
    elif any(d.get("todo_bytes") for d in disks):
        done, todo = sum(d.get("done_bytes", 0) for d in disks), sum(d.get("todo_bytes", 0) for d in disks)
        out["unit"] = "bytes"
    elif any(d.get("todo_files") for d in disks):
        done, todo = sum(d.get("done_files", 0) for d in disks), sum(d.get("todo_files", 0) for d in disks)
    else:
        done, todo = live.get("done") or 0, live.get("todo") or 0
    out["done"], out["todo"] = done, todo
    if todo:
        frac = min(1.0, done / todo)
        out["pct"] = round(100 * frac, 1)
        spent = live.get("active", 0.0) - live.get("phase_active", 0.0)
        if live.get("paused_since"):
            out["eta"] = "paused"
        elif done < todo and spent >= ETA_LIVE_SECONDS and (rate := out["rate_bytes"] if out["unit"] == "bytes"
                                                           else out["rate_files"]) > 0:
            # The speed over the last few seconds: shows up sooner and follows a disk that slows down.
            out["eta"] = "about " + fmt_seconds((todo - done) / rate) + " left"
        elif spent >= ETA_MIN_SECONDS and frac >= ETA_MIN_FRACTION and done < todo:
            out["eta"] = "about " + fmt_seconds(spent * (todo - done) / done) + " left"
    # how long this phase took in the last scan: a rough guide while the ETA has nothing to go on
    took = (last_times or {}).get(phase)
    out["last_took"] = fmt_seconds(took) if took else ""
    # whole-plan bar: each phase one segment
    no = out["phase_no"]
    out["segments"] = [{"name": phase_name(p), "state": "done" if i + 1 < no else "now" if i + 1 == no else "todo"}
                       for i, p in enumerate(plan)]
    return out

"""Pause/stop control and live progress shared by the worker and the web UI."""

from __future__ import annotations

import copy
import threading
import time
from typing import Any


class Stopped(Exception):
    """Raised inside a job when the user stopped it or the schedule window ended."""


class Control:
    def __init__(self) -> None:
        self._running = threading.Event()
        self._running.set()
        self._stop = threading.Event()
        self.stop_reason = ""

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def stop(self, reason: str = "stopped") -> None:
        self.stop_reason = reason
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
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}

    def reset(self, **kw: Any) -> None:
        with self._lock:
            self._data = {"disks": {}, "started": time.time(), **kw}

    def set(self, **kw: Any) -> None:
        with self._lock:
            self._data.update(kw)

    def disk(self, name: str, **kw: Any) -> None:
        with self._lock:
            self._data.setdefault("disks", {}).setdefault(name, {}).update(kw)

    def add(self, name: str, key: str, n: int) -> None:
        with self._lock:
            d = self._data.setdefault("disks", {}).setdefault(name, {})
            d[key] = d.get(key, 0) + n

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data = {}

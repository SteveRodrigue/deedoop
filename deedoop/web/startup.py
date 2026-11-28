"""The web UI answers from the first second, even while the database is being
upgraded.

After an update, opening the database can take minutes on a big index (a large
write-ahead log to fold in, new indexes to build). Until then the server used
not to listen at all, and deedoop looked dead. :class:`StartupGate` listens at
once: it serves a page that says what is happening and reloads itself, and
hands every request to the real app once it is ready. It shows no data and
takes no input, so it needs no sign-in (SC1, SC8).
"""

from __future__ import annotations

import html
import logging
import threading
from collections.abc import Callable

log = logging.getLogger("deedoop.startup")

WAITING_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="5"><title>Starting · deedoop</title>
<style>
:root { color-scheme: light dark; --bg: #f3f5f8; --card: #fff; --ink: #14213d; --muted: #52607a; --line: #d5dbe4; }
@media (prefers-color-scheme: dark) { :root { --bg: #0e1622; --card: #162030; --ink: #e6edf5; --muted: #9aa8bc; --line: #26344a; } }
body { margin: 0; background: var(--bg); color: var(--ink); font: 16px/1.5 system-ui, sans-serif; }
main { max-width: 560px; margin: 12vh auto 0; padding: 0 16px; }
section { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 20px 24px; }
h1 { font-size: 20px; margin: 0 0 8px; } p { margin: 8px 0; } .muted { color: var(--muted); font-size: 14px; }
</style></head>
<body><main><section>
<h1>deedoop is upgrading its database</h1>
<p>This happens once after an update. On a big index it can take several minutes, longer if the appdata
folder is on an array disk rather than the cache pool.</p>
<p><strong>Now: {step}</strong></p>
<p class="muted">This page reloads by itself every 5 seconds. Please do not restart the container: the work
would start over. The container log (Docker tab, deedoop icon, Logs) shows each step.</p>
</section></main></body></html>
"""

HEADERS = [
    (b"content-type", b"text/html; charset=utf-8"),
    (b"cache-control", b"no-store"),
    (b"retry-after", b"5"),
    (b"content-security-policy", b"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
]


class _LastStep(logging.Handler):
    """Keeps the last message of the database upgrade, to show on the page. Only
    ``deedoop.db`` and ``deedoop.startup``: never the setup code (SC8)."""

    SOURCES = ("deedoop.db", "deedoop.startup")

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.step = "opening the database"

    def emit(self, record: logging.LogRecord) -> None:
        if record.name in self.SOURCES:
            self.step = record.getMessage()


class StartupGate:
    """An ASGI app that serves :data:`WAITING_PAGE` until ``prepare()`` (run in
    a thread) returns the real app, then passes every request to it.
    ``prepare`` also starts the worker, so a scan carries on without waiting
    for a visit; ``shutdown`` stops it when the server stops."""

    def __init__(self, prepare: Callable[[], object], shutdown: Callable[[], None] | None = None):
        self._prepare = prepare
        self._shutdown = shutdown
        self.app = None
        self.error: BaseException | None = None
        self.ready = threading.Event()
        self._step = _LastStep()
        logging.getLogger("deedoop").addHandler(self._step)

    def _run(self) -> None:
        try:
            self.app = self._prepare()
        except BaseException as e:  # noqa: BLE001 - shown in the log and on the page's status
            self.error = e
            log.exception("deedoop could not open its database")
        finally:
            logging.getLogger("deedoop").removeHandler(self._step)
            self.ready.set()

    def start(self) -> None:
        threading.Thread(target=self._run, name="deedoop-startup", daemon=True).start()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if self.app is not None:
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        path = scope.get("path", "")
        if path == "/healthz":  # up and working on it: Docker should not call it unhealthy
            body, status = (b"failed" if self.error else b"starting"), (500 if self.error else 200)
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": body})
            return
        await send({"type": "http.response.start", "status": 503, "headers": HEADERS})
        page = WAITING_PAGE.replace("{step}", html.escape(self._step.step))
        await send({"type": "http.response.body", "body": page.encode()})

    async def _lifespan(self, receive, send) -> None:
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                self.start()
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                if self.app is not None and self._shutdown is not None:
                    self._shutdown()
                await send({"type": "lifespan.shutdown.complete"})
                return

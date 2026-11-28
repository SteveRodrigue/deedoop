"""FastAPI web UI. Server-rendered Jinja pages with htmx for live updates.

The UI never reads or writes the user's files. It reads the index, queues
jobs for the worker and changes settings.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import warnings
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__, auth, av, benchmark, changes, db, media_similar, plans, queries, scanners, similar
from ..archive_index import refresh_coverage
from ..archives import read_member_bytes
from ..buildinfo import REPO_URL, build_info, build_label
from ..categories import CATEGORIES
from ..config import AppConfig
from ..config import load as load_config
from ..control import PHASE_NAMES, progress_view
from ..control import fmt_bytes as human_bytes
from ..discovery import RootError, add_custom_root, list_disks, list_shares, remove_root, sync_share_roots
from ..imaging import THUMB_EXTS, NotAnImage, make_thumbnail
from ..metadata import exiftool_available
from ..tls import RENEW_DAYS, TlsInfo
from ..worker import JOB_NAMES as worker_job_names
from ..worker import Worker
from . import security
from .security import safe_next

HERE = Path(__file__).parent

# Typed, bounded query and form values (SC7).
Page = Annotated[int, Query(ge=1, le=1_000_000)]
MinMb = Annotated[float, Query(ge=0, le=1e9)]


def fmt_time(ts: float | None) -> str:
    if not ts:
        return "never"
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def tail_path(path: str | None, keep: int = 2) -> str:
    """'…/folder/file.ext' for a long path: the end is the part that changes
    while a scan runs, so cutting it off made the progress look stuck."""
    parts = (path or "").rstrip("/").split("/")
    return (path or "") if len(parts) <= keep + 2 else "…/" + "/".join(parts[-keep:])


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def create_app(cfg: AppConfig | None = None, worker: Worker | None = None, start_worker: bool = True,
               tls_info: TlsInfo | None = None) -> FastAPI:
    cfg = cfg or load_config()
    db.init(cfg.db_path)
    # Thumbnails of photos with damaged EXIF made Pillow print warnings with no file
    # name into the container log; scans record them, with the path, in the scan log.
    warnings.filterwarnings("ignore", category=UserWarning, module=r"PIL\.")
    worker = worker or Worker(cfg, isolate=True)  # jobs in their own process: pages stay quick

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_worker:
            worker.start()
        yield
        if start_worker:
            worker.shutdown()

    network = security.Network(cfg.trusted_proxies)
    setup_code = auth.SetupCode()
    c0 = db.connect(cfg.db_path)
    try:
        auth.bootstrap(c0, cfg.admin_password, setup_code)
    finally:
        c0.close()

    # Every route runs the guard first: sign-in, CSRF, origin and host checks (SC1).
    app = FastAPI(title="deedoop", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None, dependencies=[Depends(security.Guard(cfg.db_path, network))])
    app.state.cfg = cfg
    app.state.worker = worker
    app.state.setup_code = setup_code
    app.add_middleware(security.SecurityHeaders, csp=security.content_security_policy(
        security.inline_script_hashes(HERE / "templates" / "base.html")))
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def jobbar_view() -> dict | None:
        """The running job for the status bar at the bottom of every page: what,
        which phase, how far, how fast. No file names or paths, so it can show
        to signed-out visitors of the read-only view too (SC8)."""
        c = conn()
        try:
            active = db.active_job(c)
            queued_scan = c.execute("SELECT id FROM jobs WHERE type = 'scan' AND state = 'queued' "
                                    "ORDER BY id LIMIT 1").fetchone()
        finally:
            c.close()
        if active is None:
            return None
        live = worker.status.snapshot() if worker.current_job == active["id"] else {}
        pv = progress_view(live, dt.datetime.now().timestamp()) if live else {}
        return {"id": active["id"], "type": active["type"], "name": worker_job_names.get(active["type"], "Job"),
                "running": bool(live), "paused": bool(live.get("paused_since")), "pv": pv,
                "queued_scan": queued_scan["id"] if queued_scan and queued_scan["id"] != active["id"] else None}

    def auth_context(request: Request) -> dict:
        sess = getattr(request.state, "session", None)
        return {"session": sess, "signed_in": sess is not None, "jobbar": jobbar_view(),
                "needs_confirm": sess is not None and not sess.recently_confirmed(),
                "legacy_password": cfg.legacy_password_set,
                "public_view": getattr(request.state, "public_view", False)}

    templates = Jinja2Templates(directory=HERE / "templates", context_processors=[auth_context])
    templates.env.filters["bytes"] = human_bytes
    templates.env.filters["time"] = fmt_time
    templates.env.filters["tail"] = tail_path
    templates.env.filters["change_text"] = lambda ch: f"{ch[0]}: {ch[1]} → {ch[2]}"
    templates.env.globals["version"] = __version__
    templates.env.globals["build"] = build_info()
    templates.env.globals["repo_url"] = REPO_URL
    templates.env.globals["phase_names"] = PHASE_NAMES
    templates.env.filters["clock"] = lambda ts: dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else ""

    def conn():
        return db.connect(cfg.db_path)

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        if "gate" not in ctx:
            c = conn()
            try:
                ctx["gate"] = changes.gate(c, cfg.mnt)
            finally:
                c.close()
        return templates.TemplateResponse(request, name, {"nav": name.split(".")[0], **ctx})

    def job_view(c) -> dict:
        active = db.active_job(c)
        last = c.execute(
            "SELECT * FROM jobs WHERE state NOT IN ('queued', 'running') AND type = 'scan' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        live = worker.status.snapshot() if worker.current_job else {}
        queued_scan = c.execute("SELECT id FROM jobs WHERE type = 'scan' AND state = 'queued' AND id != ? "
                                "ORDER BY id LIMIT 1", (active["id"] if active else 0,)).fetchone()
        now = dt.datetime.now().timestamp()
        elapsed = fmt_duration(now - live["started"]) if live.get("started") else ""
        last_progress = json.loads(last["progress"]) if last else {}
        last_walked = sum(d.get("walked", 0) for d in (last_progress.get("disks") or {}).values()) \
            if last and last["state"] == "done" else 0
        # Phase times of the last scan that finished (a stopped one only knows its first phases).
        done_scan = c.execute("SELECT progress FROM jobs WHERE type = 'scan' AND state = 'done' "
                              "ORDER BY id DESC LIMIT 1").fetchone()
        last_times = (json.loads(done_scan["progress"]) or {}).get("phase_times", {}) if done_scan else {}
        return {
            "active": dict(active) if active else None,
            "last": dict(last) if last else None,
            "queued_scan": queued_scan["id"] if queued_scan else None,
            "last_progress": last_progress,
            "live": live,
            "elapsed": elapsed,
            "pv": progress_view(live, now, last_walked, last_times) if live else {},
            "last_took": fmt_duration(last_progress["active"]) if last_progress.get("active") else "",
            "log_tail": worker.status.recent_log(8) if worker.current_job else [],
            "paused": worker.control.paused if worker.current_job else False,
        }

    # -- sign-in (milestone 6.5) -------------------------------------------------------

    def _ip(request: Request) -> str:
        return getattr(request.state, "client_ip", "") or ""

    def _signed_in_redirect(request: Request, user_id: int, target: str) -> RedirectResponse:
        c = conn()
        try:
            token = auth.new_session(c, user_id, _ip(request), request.headers.get("user-agent", ""))
        finally:
            c.close()
        resp = RedirectResponse(safe_next(target), status_code=303)
        resp.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_MAX, httponly=True, samesite="strict",
                        secure=network.is_https(request), path="/")
        return resp

    def public_status(c) -> dict:
        """What the sign-in page shows to anyone: no file names, no paths."""
        active = db.active_job(c)
        scan = c.execute("SELECT state FROM jobs WHERE type = 'scan' AND state IN ('queued', 'running') "
                         "ORDER BY id LIMIT 1").fetchone()
        last = c.execute("SELECT finished_at, state FROM jobs WHERE type = 'scan' AND state NOT IN ('queued', 'running') "
                         "ORDER BY id DESC LIMIT 1").fetchone()
        st = queries.index_stats(c, cfg.db_path)
        return {"scanning": scan is not None and scan["state"] == "running",
                "scan_queued": scan is not None and scan["state"] == "queued", "busy": active is not None,
                "last_scan": last["finished_at"] if last else None, "last_state": last["state"] if last else "",
                "files": st.get("files", 0), "groups": st.get("groups", 0), "wasted": st.get("total_wasted", 0)}

    def _login_page(request: Request, c, *, next: str = "/", error: str = "", ticket: str = ""):
        first_run = not auth.has_admin(c)
        if first_run and not ticket:
            setup_code.current()  # makes sure a valid code is in the log
        return templates.TemplateResponse(request, "login.html", {
            "nav": "login", "next": safe_next(next), "error": error, "first_run": first_run,
            "ticket": ticket, "status": public_status(c), "public_view": request.state.public_view,
            "gate": {"open": False}})

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/", error: str = ""):
        if request.state.session is not None:
            return RedirectResponse(safe_next(next), status_code=303)
        c = conn()
        try:
            return _login_page(request, c, next=next, error=error)
        finally:
            c.close()

    @app.post("/login")
    def login(request: Request, username: Annotated[str, Form(max_length=64)] = "",
              password: Annotated[str, Form(max_length=auth.MAX_PASSWORD)] = "", next: str = Form("/")):
        c = conn()
        try:
            try:
                uid = auth.authenticate(c, username, password, _ip(request))
            except auth.AuthError as e:
                return RedirectResponse(f"/login?next={_q(safe_next(next))}&error={_q(str(e))}", status_code=303)
            auth.event(c, "signin", username.strip(), _ip(request))
        finally:
            c.close()
        return _signed_in_redirect(request, uid, next)

    @app.post("/first-run")
    def first_run(request: Request, code: Annotated[str, Form(max_length=64)] = "",
                  ticket: Annotated[str, Form(max_length=128)] = "",
                  username: Annotated[str, Form(max_length=64)] = "admin",
                  password: Annotated[str, Form(max_length=auth.MAX_PASSWORD)] = "",
                  password2: Annotated[str, Form(max_length=auth.MAX_PASSWORD)] = ""):
        """Two steps. 1: the setup code from the log; a right one returns the
        account form with a ticket. 2: the ticket, user name and password."""
        c = conn()
        try:
            if auth.has_admin(c):
                return RedirectResponse("/login", status_code=303)
            # Wrong codes and tickets are throttled like wrong passwords.
            wait = auth.lockout_remaining(c, _ip(request))
            if wait:
                return RedirectResponse(f"/login?error={_q('Too many wrong setup codes. Try again later.')}",
                                        status_code=303)
            if not ticket:
                if not setup_code.check(code):
                    try:
                        auth.authenticate(c, "(setup code)", "", _ip(request))
                    except auth.AuthError:
                        pass
                    return RedirectResponse(f"/login?error={_q('That setup code is wrong or has expired. The current code is in the container log.')}",
                                            status_code=303)
                return _login_page(request, c, ticket=setup_code.issue_ticket())
            if not setup_code.check_ticket(ticket):
                try:
                    auth.authenticate(c, "(setup code)", "", _ip(request))
                except auth.AuthError:
                    pass
                return RedirectResponse(f"/login?error={_q('That step took too long. Enter the setup code again.')}",
                                        status_code=303)
            if password != password2:
                return _login_page(request, c, ticket=ticket, error="The two passwords are different.")
            try:
                uid = auth.create_admin(c, username, password)
            except auth.AuthError as e:
                return _login_page(request, c, ticket=ticket, error=str(e))
            setup_code.burn()
            auth.event(c, "admin_created", username.strip(), _ip(request), "with the setup code")
        finally:
            c.close()
        return _signed_in_redirect(request, uid, "/setup")

    @app.post("/logout")
    def logout(request: Request):
        c = conn()
        try:
            auth.end_session(c, request.cookies.get(auth.COOKIE))
            auth.event(c, "signout", request.state.session.username, _ip(request))
        finally:
            c.close()
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.COOKIE, path="/")
        return resp

    def _confirm(request: Request, c, password: str) -> str | None:
        """Dangerous actions need the password if it was typed more than 10
        minutes ago. Return an error message, or None when confirmed."""
        sess = request.state.session
        if sess.recently_confirmed():
            return None
        if not password:
            return "Enter your password to confirm."
        try:
            auth.confirm_password(c, sess, password, _ip(request))
        except auth.AuthError as e:
            return str(e)
        return None

    @app.post("/account/password")
    def account_password(request: Request, current: Annotated[str, Form(max_length=auth.MAX_PASSWORD)] = "",
                         password: Annotated[str, Form(max_length=auth.MAX_PASSWORD)] = "",
                         password2: Annotated[str, Form(max_length=auth.MAX_PASSWORD)] = ""):
        sess = request.state.session
        c = conn()
        try:
            try:
                auth.authenticate(c, sess.username, current, _ip(request))
            except auth.AuthError as e:
                return RedirectResponse(f"/setup?error={_q('Current password: ' + str(e))}#account", status_code=303)
            if password != password2:
                return RedirectResponse(f"/setup?error={_q('The two new passwords are different.')}#account",
                                        status_code=303)
            try:
                auth.set_password(c, sess.user_id, password)  # also signs out every session
            except auth.AuthError as e:
                return RedirectResponse(f"/setup?error={_q(str(e))}#account", status_code=303)
            auth.event(c, "password_changed", sess.username, _ip(request))
        finally:
            c.close()
        return _signed_in_redirect(request, sess.user_id, "/setup?saved=password#account")

    @app.post("/account/username")
    def account_username(request: Request, username: Annotated[str, Form(max_length=64)] = ""):
        sess = request.state.session
        c = conn()
        try:
            try:
                auth.rename_admin(c, sess.user_id, username)
            except auth.AuthError as e:
                return RedirectResponse(f"/setup?error={_q(str(e))}#account", status_code=303)
            auth.event(c, "username_changed", username.strip(), _ip(request), f"was {sess.username}")
        finally:
            c.close()
        return RedirectResponse("/setup?saved=account#account", status_code=303)

    @app.post("/account/signout-all")
    def account_signout_all(request: Request):
        c = conn()
        try:
            n = auth.end_all_sessions(c)
            auth.event(c, "signout_all", request.state.session.username, _ip(request), f"{n} sessions ended")
        finally:
            c.close()
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.COOKIE, path="/")
        return resp

    @app.post("/setup/access")
    def setup_access(public_view: str = Form(""), allowed_hosts: Annotated[str, Form(max_length=4000)] = ""):
        hosts = [h.strip().lower() for h in _lines(allowed_hosts.replace(",", "\n"))]
        c = conn()
        try:
            db.set_settings(c, {"public_view": bool(public_view), "allowed_hosts": hosts})
        finally:
            c.close()
        return RedirectResponse("/setup?saved=access#access", status_code=303)

    def _who(request: Request, c, action_id: int) -> None:
        sess = request.state.session
        c.execute("UPDATE actions SET username = ?, ip = ? WHERE id = ?", (sess.username, _ip(request), action_id))

    # -- pages ----------------------------------------------------------------

    DASHBOARD_NOTICES = {
        "noshares": ("err", "There is nothing to scan yet: tick the shares to scan on the Setup screen and save, "
                            "then start the scan."),
        "queued": ("ok", "Scan queued: it starts as soon as the job that is running now finishes."),
    }

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, notice: Annotated[str, Query(max_length=16)] = ""):
        c = conn()
        try:
            stats = queries.index_stats(c, cfg.db_path)
            roots = c.execute("SELECT COUNT(*) FROM roots WHERE enabled = 1").fetchone()[0]
            benchmarked = c.execute("SELECT 1 FROM benchmarks LIMIT 1").fetchone() is not None
            return render(request, "dashboard.html", stats=stats, roots=roots, benchmarked=benchmarked,
                          by_cat=queries.wasted_by_category(c), disks=queries.per_disk(c),
                          settings=(s := db.get_settings(c)), methods_on=scanners.methods(s),
                          raw_only=scanners.raw_only(s), notice=DASHBOARD_NOTICES.get(notice), **job_view(c))
        finally:
            c.close()

    @app.get("/partials/jobbar", response_class=HTMLResponse)
    def jobbar_partial(request: Request):
        return templates.TemplateResponse(request, "_jobbar.html", {})

    @app.get("/partials/status", response_class=HTMLResponse)
    def status_partial(request: Request):
        c = conn()
        try:
            return templates.TemplateResponse(request, "_status.html", {**job_view(c), "settings": db.get_settings(c)})
        finally:
            c.close()

    @app.get("/setup", response_class=HTMLResponse)
    def setup(request: Request, error: str = "", saved: str = ""):
        c = conn()
        try:
            disks = list_disks(cfg.mnt)
            shares = list_shares(cfg.mnt, disks)
            settings = db.get_settings(c)
            roots = c.execute("SELECT * FROM roots ORDER BY source, path").fetchall()
            bench_row = c.execute("SELECT * FROM benchmarks ORDER BY id DESC LIMIT 1").fetchone()
            bench = json.loads(bench_row["result"]) if bench_row else None
            applied = settings["bench_applied"]
            disk_files = {r["disk"]: r["files"] for r in queries.per_disk(c)}
            return render(request, "setup.html", disks=disks, shares=shares, settings=settings,
                          roots=roots, root_counts=queries.root_counts(c), disk_files=disk_files,
                          bench=bench, bench_rows=benchmark.compare(settings, bench["proposal"]) if bench else [],
                          bench_applied=applied if applied and bench_row and applied.get("bench_id") == bench_row["id"]
                          else None, error=error, saved=saved,
                          mnt=str(cfg.mnt), exiftool=exiftool_available(), gate=changes.gate(c, cfg.mnt),
                          scanner_rows=scanners.status(settings), raw_only=scanners.raw_only(settings),
                          chromaprint=av.tools_available() and av.chromaprint_available(),
                          tls=tls_info or TlsInfo(), request_https=network.is_https(request),
                          renew_days=RENEW_DAYS, events=auth.recent_events(c) if request.state.session else [], **job_view(c))
        finally:
            c.close()

    @app.post("/setup/shares")
    async def setup_shares(request: Request):
        form = await request.form()
        selected = sorted(set(form.getlist("share")))
        c = conn()
        try:
            db.set_settings(c, {"shares": selected})
            sync_share_roots(c, cfg.mnt)
        finally:
            c.close()
        return RedirectResponse("/setup?saved=shares", status_code=303)

    @app.post("/setup/custom")
    def setup_custom(path: str = Form(...)):
        c = conn()
        try:
            add_custom_root(c, cfg.mnt, path)
        except RootError as e:
            return RedirectResponse(f"/setup?error={_q(str(e))}", status_code=303)
        finally:
            c.close()
        return RedirectResponse("/setup?saved=folder", status_code=303)

    @app.post("/setup/root/{root_id}/remove")
    def setup_remove_root(root_id: int):
        c = conn()
        try:
            row = c.execute("SELECT * FROM roots WHERE id = ?", (root_id,)).fetchone()
            if row and row["source"] == "share":
                s = db.get_settings(c)
                db.set_settings(c, {"shares": [x for x in s["shares"] if x != row["share"]]})
            remove_root(c, root_id)
        finally:
            c.close()
        return RedirectResponse("/setup?saved=removed", status_code=303)

    @app.post("/setup/settings")
    def setup_settings(
        exclusions: str = Form(""),
        takeout_paths: str = Form(""),
        min_size: int = Form(1),
        disk_mode: str = Form("single"),
        parallel_disks: int = Form(2),
        hash_threads: int = Form(1),
        schedule_enabled: str = Form(""),
        schedule_start: str = Form("01:00"),
        schedule_end: str = Form("07:00"),
        image_matching: str = Form(""),
        metadata: str = Form(""),
        archives_everywhere: str = Form(""),
        similar_matching: str = Form(""),
        similar_threshold: int = Form(similar.DEFAULT_THRESHOLD),
        similar_media: str = Form(""),
        scan_photo: str = Form(""),
        scan_video: str = Form(""),
        scan_audio: str = Form(""),
        scan_archive: str = Form(""),
    ):
        if disk_mode not in ("single", "parallel"):
            return RedirectResponse("/setup?error=Unknown+disk+mode", status_code=303)
        try:
            dt.datetime.strptime(schedule_start, "%H:%M")
            dt.datetime.strptime(schedule_end, "%H:%M")
        except ValueError:
            return RedirectResponse("/setup?error=Times+must+look+like+01:00", status_code=303)
        c = conn()
        try:
            before = db.get_settings(c)
            toggles = {"photo": bool(scan_photo), "video": bool(scan_video), "audio": bool(scan_audio),
                       "archive": bool(scan_archive)}
            db.set_settings(c, {
                "scanners": toggles,
                "exclusions": _lines(exclusions),
                "takeout_paths": _lines(takeout_paths),
                "min_size": max(1, min_size),
                "disk_mode": disk_mode,
                "parallel_disks": max(2, min(parallel_disks, 32)),
                "hash_threads": max(1, min(hash_threads, 64)),
                "schedule_enabled": bool(schedule_enabled),
                "schedule_start": schedule_start,
                "schedule_end": schedule_end,
                "image_matching": bool(image_matching),
                "metadata": bool(metadata),
                "archives_everywhere": bool(archives_everywhere),
                "similar_matching": bool(similar_matching),
                "similar_threshold": max(0, min(similar_threshold, similar.MAX_DISTANCE)),
                "similar_media": bool(similar_media),
            })
            after = db.get_settings(c)
            # A scanner or method turned off: drop its groups and marks now, without
            # waiting for the next scan. One turned on needs a scan to read files.
            regroup = (scanners.methods(before) != scanners.methods(after)
                       or scanners.active(before, "archive") != scanners.active(after, "archive"))
            if regroup and db.active_job(c) is None and c.execute("SELECT 1 FROM groups LIMIT 1").fetchone():
                db.enqueue_job(c, "regroup")
        finally:
            c.close()
        worker.wake()
        return RedirectResponse("/setup?saved=settings#scanners", status_code=303)

    @app.post("/setup/changes")
    def setup_changes(request: Request, allow_changes: str = Form(""), allow_direct_delete: str = Form(""),
                      quarantine_days: int = Form(30),
                      password: Annotated[str, Form(max_length=auth.MAX_PASSWORD)] = ""):
        c = conn()
        try:
            before = db.get_settings(c)
            opening = (bool(allow_changes) and not before["allow_changes"]) or (
                bool(allow_changes) and bool(allow_direct_delete) and not before["allow_direct_delete"])
            if opening and (err := _confirm(request, c, password)):
                return RedirectResponse(f"/setup?error={_q(err)}#changes", status_code=303)
            db.set_settings(c, {"allow_changes": bool(allow_changes),
                                "allow_direct_delete": bool(allow_changes) and bool(allow_direct_delete),
                                "quarantine_days": max(0, min(quarantine_days, 3650))})
        finally:
            c.close()
        return RedirectResponse("/setup?saved=changes#changes", status_code=303)

    @app.post("/setup/benchmark")
    def setup_benchmark():
        c = conn()
        try:
            if db.active_job(c) is not None:
                # It reads the same disks as a scan, so the numbers would be wrong.
                err = "The benchmark cannot run while a scan or another job is running. Run it when that finishes."
                return RedirectResponse(f"/setup?error={_q(err)}#benchmark", status_code=303)
            db.enqueue_job(c, "benchmark")
        finally:
            c.close()
        worker.wake()
        return RedirectResponse("/setup?saved=bench_started#benchmark", status_code=303)

    @app.post("/setup/benchmark/apply")
    def setup_benchmark_apply():
        c = conn()
        try:
            bench = c.execute("SELECT id, result FROM benchmarks ORDER BY id DESC LIMIT 1").fetchone()
            if not bench:
                return RedirectResponse(f"/setup?error={_q('Run the benchmark first.')}#benchmark", status_code=303)
            proposal = json.loads(bench["result"])["proposal"]
            rows = benchmark.compare(db.get_settings(c), proposal)
            db.set_settings(c, {**benchmark.proposal_settings(proposal), "bench_applied": {
                "at": dt.datetime.now().timestamp(), "bench_id": bench["id"],
                "changes": [[r["label"], r["current"], r["proposed"]] for r in rows if r["changed"]]}})
        finally:
            c.close()
        return RedirectResponse("/setup?saved=benchmark#disk-mode", status_code=303)

    @app.post("/setup/benchmark/dismiss")
    def setup_benchmark_dismiss():
        c = conn()
        try:
            db.set_settings(c, {"bench_hint_dismissed": True})
        finally:
            c.close()
        return RedirectResponse("/", status_code=303)

    @app.get("/duplicates", response_class=HTMLResponse)
    def duplicates(request: Request, method: str = "exact", category: str = "", q: str = "", min_mb: MinMb = 0,
                   disk: str = "", group: int = 0, archives: str = "", page: Page = 1, msg: str = ""):
        method = method if method in queries.METHODS else "exact"
        flt = queries.GroupFilter(method=method, category=category, q=q, min_size=int(min_mb * 1e6), disk=disk,
                                  group=group, archives=bool(archives), page=page)
        c = conn()
        try:
            groups, total, wasted = queries.list_groups(c, flt)
            members = queries.group_members(c, [g["id"] for g in groups])
            disks = [r[0] for r in c.execute("SELECT DISTINCT disk FROM roots WHERE enabled = 1 ORDER BY disk")]
            pages = max(1, -(-total // flt.per_page))
            qs = _q_params(method=method if method != "exact" else "", category=category, q=q,
                           min_mb=min_mb or "", disk=disk, group=group or "", archives=archives)
            settings = db.get_settings(c)
            shown = scanners.methods(settings)
            if method not in shown:
                shown.append(method)  # turned off: still reachable from a link, with a note
            counts = {m: c.execute("SELECT COUNT(*), COALESCE(SUM(wasted), 0) FROM groups WHERE method = ? "
                                   "AND wasted > 0", (m,)).fetchone() for m in shown}
            return render(request, "duplicates.html", groups=groups, members=members, total=total,
                          wasted=wasted, flt=flt, pages=pages, categories=CATEGORIES, disks=disks, qs=qs,
                          min_mb=min_mb or "", methods={m: queries.METHODS[m] for m in shown}, counts=counts,
                          method_on=method in scanners.methods(settings), media_fields=queries.MEDIA_FIELDS,
                          settings=settings, marked=changes.marked_in_groups(c, [g["id"] for g in groups]),
                          marks=changes.marks_summary(c), keeper_rules=changes.KEEPER_RULES, msg=msg,
                          back=str(request.url.path) + ("?" + str(request.url.query) if request.url.query else ""))
        finally:
            c.close()

    @app.get("/browse", response_class=HTMLResponse)
    def browse(request: Request, loc: str = "", dir: str = "", q: str = "", kind: str = "", year: str = "",
               camera: str = "", no_gps: str = "", no_date: str = "", dups: str = "", page: Page = 1,
               view: str = "list", artist: Annotated[str, Query(max_length=300)] = "",
               album: Annotated[str, Query(max_length=300)] = "", codec: Annotated[str, Query(max_length=40)] = "",
               length: Annotated[str, Query(max_length=10)] = ""):
        view = view if view in ("list", "grid", "music") else "list"
        c = conn()
        try:
            locs = queries.locations(c)
            if not loc and locs:
                loc = locs[0]["key"]
            music_view = view == "music"
            flt = queries.BrowseFilter(loc=loc, dir=dir.strip("/"), q=q, kind=kind, year=year, camera=camera,
                                       no_gps=bool(no_gps), no_date=bool(no_date), dups=bool(dups), page=page,
                                       artist="" if music_view else artist, album=album, codec=codec,
                                       length=length)
            res = queries.browse(c, flt) if not music_view else {"subdirs": [], "files": [], "total": 0,
                                                                  "base": queries.loc_roots(c, loc)[1]}
            music = queries.music(c, queries.BrowseFilter(loc=loc, dir=flt.dir, artist=artist)) if music_view else None
            crumbs, acc = [], []
            for part in flt.dir.split("/") if flt.dir else []:
                acc.append(part)
                crumbs.append((part, "/".join(acc)))
            filters = _q_params(q=q, kind=kind, year=year, camera=camera, no_gps=no_gps, no_date=no_date,
                                dups=dups, artist=flt.artist, album=album, codec=codec, length=length,
                                view=view if view != "list" else "")
            s = db.get_settings(c)
            return render(request, "browse.html", locs=locs, flt=flt, res=res, crumbs=crumbs,
                          facets=queries.facets(c), categories=CATEGORIES, view=view, filters=filters,
                          pages=max(1, -(-res["total"] // flt.per_page)), loc=loc, music=music, artist=artist,
                          media_on=bool(scanners.media_kinds(s)), audio_on=scanners.active(s, "audio"))
        finally:
            c.close()

    @app.get("/file/{file_id}", response_class=HTMLResponse)
    def file_page(request: Request, file_id: int):
        c = conn()
        try:
            f = queries.file_detail(c, file_id)
            if f is None:
                return Response("No such file in the index", status_code=404)
            s = db.get_settings(c)
            return render(request, "file.html", f=f,
                          similar=queries.similar_for_file(c, file_id, threshold_of(s)) if s["similar_matching"] else [])
        finally:
            c.close()

    @app.get("/thumb/{file_id}")
    def thumb(file_id: int, s: int = 320):
        size = 160 if s <= 160 else 320 if s <= 320 else 640
        c = conn()
        try:
            sql = (f"SELECT f.dir, f.name, f.ext, f.size, f.mtime_ns, r.path, {queries.CONTAINER_COLS} "  # noqa: S608
                   f"FROM files f JOIN roots r ON r.id = f.root_id {queries.CONTAINER_JOINS} WHERE f.id = ?")  # SC5: constants
            r = c.execute(sql, (file_id,)).fetchone()
        finally:
            c.close()
        if r is None or r["ext"] not in THUMB_EXTS:
            return Response(status_code=404)
        if not queries.can_thumb(r):
            return Response(PLACEHOLDER_SVG, media_type="image/svg+xml", headers={"Cache-Control": "max-age=300"})
        folder = cfg.config_dir / "thumbs" / str(size) / f"{file_id % 256:02x}"
        dest = folder / f"{file_id}-{r['mtime_ns']}-{r['size']}.jpg"
        if not dest.exists():
            for old in folder.glob(f"{file_id}-*.jpg"):  # the file changed since
                old.unlink(missing_ok=True)
            try:
                if r["container_id"]:
                    archive = str(Path(r["path"]) / r["c1_dir"] / r["c1_name"])
                    member = f"{r['dir']}/{r['name']}" if r["dir"] else r["name"]
                    make_thumbnail(io.BytesIO(read_member_bytes(archive, member)), dest, size)
                else:
                    make_thumbnail(str(Path(r["path"]) / r["dir"] / r["name"]), dest, size)
            except (NotAnImage, OSError, KeyError, zipfile.BadZipFile):
                return Response(PLACEHOLDER_SVG, media_type="image/svg+xml",
                                headers={"Cache-Control": "max-age=300"})
        return FileResponse(dest, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @app.get("/media/{file_id}")
    def media_file(file_id: int):
        """Play a song or video in the file page. Read-only, loose files only,
        audio and video types only, served with range requests (SC1: a GET
        that reads, never writes)."""
        c = conn()
        try:
            r = c.execute("SELECT f.dir, f.name, f.ext, f.size, f.mtime_ns, f.container_id, f.state, r.path "
                          "FROM files f JOIN roots r ON r.id = f.root_id WHERE f.id = ? AND r.enabled = 1",
                          (file_id,)).fetchone()
        finally:
            c.close()
        if r is None or r["container_id"] or r["state"] != "present" or r["ext"] not in PLAYABLE:
            return Response(status_code=404)
        path = Path(r["path"]) / r["dir"] / r["name"]
        try:
            st = path.lstat()  # a symlink swapped in after the scan is not followed
        except OSError:
            return Response(status_code=404)
        if not path.is_file() or path.is_symlink() or (st.st_size, st.st_mtime_ns) != (r["size"], r["mtime_ns"]):
            return Response(status_code=404)
        return FileResponse(path, media_type=PLAYABLE[r["ext"]], headers={"Cache-Control": "private, max-age=3600"},
                            content_disposition_type="inline")

    @app.get("/review", response_class=HTMLResponse)
    def review(request: Request, view: str = "queue", done: str = "", kind: str = "photos", msg: str = ""):
        c = conn()
        try:
            s = db.get_settings(c)
            t = threshold_of(s)
            decided = view == "decided"
            media_counts = media_similar.counts(c)
            if kind == "media":
                pairs = [p for p in (queries.media_pair_view(c, r) for r in media_similar.queue(
                    c, decided=decided, limit=100 if decided else 2)) if p]
                for p in pairs:
                    for side in (p["a"], p["b"]):
                        side["play"] = side["ext"] in PLAYABLE
                return render(request, "review_media.html", pairs=pairs, view="decided" if decided else "queue",
                              counts=media_counts, photo_counts=similar.counts(c, t), settings=s,
                              decisions=media_similar.DECISIONS, done=done, msg=msg,
                              media_on="media_similar" in scanners.methods(s))
            pairs = [p for p in (queries.pair_view(c, r) for r in similar.queue(c, t, decided=decided,
                                                                                  limit=100 if decided else 2)) if p]
            return render(request, "review.html", pairs=pairs, view="decided" if decided else "queue",
                          counts=similar.counts(c, t), threshold=t, settings=s, decisions=similar.DECISIONS,
                          done=done, msg=msg, max_distance=similar.MAX_DISTANCE, media_counts=media_counts)
        finally:
            c.close()

    @app.post("/review/media/decide")
    def review_media_decide(a: int = Form(...), b: int = Form(...), decision: str = Form(...)):
        c = conn()
        try:
            ok = media_similar.decide(c, a, b, decision)
            if ok and decision != "later":
                media_similar.rebuild_groups(c)
        finally:
            c.close()
        # SC7: the redirect carries only a name from our own list, never the posted text.
        done = next((d for d in media_similar.DECISIONS if d == decision), "") if ok else ""
        return RedirectResponse(f"/review?kind=media&done={done}", status_code=303)

    @app.post("/review/media/undo")
    def review_media_undo(a: int = Form(...), b: int = Form(...)):
        c = conn()
        try:
            media_similar.undo(c, a, b)
            media_similar.rebuild_groups(c)
        finally:
            c.close()
        return RedirectResponse(_with_msg("/review?kind=media&view=decided", "Decision undone"), status_code=303)

    @app.get("/poster/{file_id}")
    def poster(file_id: int):
        """A frame from the middle of a video, for the review queue (milestone 8).
        Read-only; made once with ffmpeg and cached like thumbnails (SC1, SC3)."""
        c = conn()
        try:
            r = c.execute("SELECT f.dir, f.name, f.size, f.mtime_ns, f.container_id, f.state, r.path, av.duration "
                          "FROM files f JOIN roots r ON r.id = f.root_id JOIN av ON av.file_id = f.id "
                          "WHERE f.id = ? AND r.enabled = 1 AND av.kind = 'video' AND av.error IS NULL",
                          (file_id,)).fetchone()
        finally:
            c.close()
        if r is None or r["container_id"] or r["state"] != "present":
            return Response(status_code=404)
        folder = cfg.config_dir / "thumbs" / "poster" / f"{file_id % 256:02x}"
        dest = folder / f"{file_id}-{r['mtime_ns']}-{r['size']}.jpg"
        if not dest.exists():
            path = Path(r["path"]) / r["dir"] / r["name"]
            try:
                st = path.lstat()
                if path.is_symlink() or (st.st_size, st.st_mtime_ns) != (r["size"], r["mtime_ns"]):
                    return Response(status_code=404)
                data = av.poster(str(path), (r["duration"] or 0) / 2)
            except (OSError, RuntimeError, ValueError):
                return Response(PLACEHOLDER_SVG, media_type="image/svg+xml", headers={"Cache-Control": "max-age=300"})
            if not data.startswith(b"\xff\xd8"):
                return Response(PLACEHOLDER_SVG, media_type="image/svg+xml", headers={"Cache-Control": "max-age=300"})
            folder.mkdir(parents=True, exist_ok=True)
            for old in folder.glob(f"{file_id}-*.jpg"):
                old.unlink(missing_ok=True)
            tmp = dest.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(dest)
        return FileResponse(dest, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @app.post("/review/decide")
    def review_decide(a: int = Form(...), b: int = Form(...), decision: str = Form(...)):
        c = conn()
        try:
            ok = similar.decide(c, a, b, decision)
            if ok and decision != "later":
                similar.rebuild_similar_groups(c, threshold_of(db.get_settings(c)))
        finally:
            c.close()
        return RedirectResponse(f"/review?done={_q(decision) if ok else ''}", status_code=303)

    @app.post("/review/undo")
    def review_undo(a: int = Form(...), b: int = Form(...)):
        c = conn()
        try:
            similar.undo(c, a, b)
            similar.rebuild_similar_groups(c, threshold_of(db.get_settings(c)))
        finally:
            c.close()
        return RedirectResponse(_with_msg("/review?view=decided", "Decision undone"), status_code=303)

    @app.post("/review/threshold")
    def review_threshold(threshold: int = Form(...), view: str = Form("queue")):
        c = conn()
        try:
            t = max(0, min(threshold, similar.MAX_DISTANCE))
            db.set_settings(c, {"similar_threshold": t})
            similar.rebuild_similar_groups(c, t)
            refresh_coverage(c, t)
        finally:
            c.close()
        return RedirectResponse(_with_msg(f"/review?view={'decided' if view == 'decided' else 'queue'}",
                                          f"Match closeness set to {t}"), status_code=303)

    @app.get("/archives", response_class=HTMLResponse)
    def archives_page(request: Request):
        c = conn()
        try:
            return render(request, "archives.html", archives=queries.archive_list(c), settings=db.get_settings(c))
        finally:
            c.close()

    @app.get("/archives/{file_id}", response_class=HTMLResponse)
    def archive_page(request: Request, file_id: int, status: str = "new", album: str = "", page: Page = 1):
        c = conn()
        try:
            parts = queries.archive_parts(c, file_id)
            if not parts:
                return Response("No such archive in the index", status_code=404)
            s = db.get_settings(c)
            members = queries.archive_members(c, parts, threshold_of(s) if s["similar_matching"] else None)
        finally:
            c.close()
        status = status if status in queries.STATUSES or status == "all" else "new"
        shown = [m for m in members if (status == "all" or m["status"] == status
                                        or (status == "new" and m["status"] == "unreadable"))
                 and (not album or queries.album_of(m["dir"]) == album)]
        per_page = 120
        pages = max(1, -(-len(shown) // per_page))
        page = min(max(page, 1), pages)
        counts = {k: sum(1 for m in members if m["status"] == k) for k in queries.STATUSES}
        first = parts[0]
        label = queries._set_label(first) if first["takeout_set"] else first["name"]
        return render(request, "archive.html", parts=parts, label=label, albums=queries.albums(members),
                      shown=shown[(page - 1) * per_page:page * per_page], total_shown=len(shown), status=status,
                      album=album, counts=counts, statuses=queries.STATUSES, page=page, pages=pages,
                      file_id=file_id, total=len(members),
                      new_bytes=sum(m["size"] for m in members if m["status"] in ("new", "unreadable")),
                      qs=_q_params(status=status if status != "new" else "", album=album))

    # -- reorganise (milestone 5: plans only, nothing is moved) ---------------------

    def _plan_inputs(c, loc, dir, q, kind, year, camera, no_gps, no_date, dups):
        locs = queries.locations(c)
        if not loc and locs:
            loc = locs[0]["key"]
        flt = queries.BrowseFilter(loc=loc, dir=dir.strip("/"), q=q, kind=kind, year=year, camera=camera,
                                   no_gps=bool(no_gps), no_date=bool(no_date), dups=bool(dups))
        source = {"loc": loc, "dir": flt.dir, "q": q, "kind": kind, "year": year, "camera": camera,
                  "no_gps": no_gps, "no_date": no_date, "dups": dups}
        return locs, flt, source

    def _run_preview(c, flt, rule):
        rows = queries.selection(c, flt, limit=PLAN_LIMIT + 1)
        capped = len(rows) > PLAN_LIMIT
        return plans.preview(c, cfg.mnt, rows[:PLAN_LIMIT], rule), capped

    @app.get("/reorganize", response_class=HTMLResponse)
    def reorganize(request: Request, loc: str = "", dir: str = "", q: str = "", kind: str = "", year: str = "",
                   camera: str = "", no_gps: str = "", no_date: str = "", dups: str = "", template: str = "",
                   fallback: str = "undated", conflicts: str = "rename", status: str = "", page: Page = 1):
        c = conn()
        try:
            locs, flt, source = _plan_inputs(c, loc, dir, q, kind, year, camera, no_gps, no_date, dups)
            rule = plans.Rule(template.strip() or plans.DEFAULT_TEMPLATE, fallback, conflicts)
            error, items, capped = "", [], False
            if locs:
                try:
                    items, capped = _run_preview(c, flt, rule)
                except plans.RuleError as e:
                    error = str(e)
            base = next((x["user_path"] for x in locs if x["key"] == flt.loc), "")
        finally:
            c.close()
        summ = plans.summary(items)
        shown = [i for i in items if not status or i.status == status]
        per_page = 200
        pages = max(1, -(-len(shown) // per_page))
        page = min(max(page, 1), pages)
        src_qs = _q_params(**source)
        rule_qs = _q_params(template=rule.template, fallback=fallback if fallback != "undated" else "",
                            conflicts=conflicts if conflicts != "rename" else "")
        return render(request, "reorganize.html", locs=locs, flt=flt, source=source, rule=rule, error=error,
                      summ=summ, shown=shown[(page - 1) * per_page:page * per_page], total_shown=len(shown),
                      status=status, page=page, pages=pages, capped=capped, limit=PLAN_LIMIT, base=base,
                      statuses=plans.STATUS, presets=plans.PRESETS, tokens=plans.TOKENS,
                      fallbacks=plans.FALLBACKS, conflict_opts=plans.CONFLICTS, src_qs=src_qs, rule_qs=rule_qs)

    @app.post("/reorganize/save")
    def reorganize_save(loc: str = Form(""), dir: str = Form(""), q: str = Form(""), kind: str = Form(""),
                        year: str = Form(""), camera: str = Form(""), no_gps: str = Form(""),
                        no_date: str = Form(""), dups: str = Form(""), template: str = Form(...),
                        fallback: str = Form("undated"), conflicts: str = Form("rename"), name: str = Form("")):
        c = conn()
        try:
            _locs, flt, source = _plan_inputs(c, loc, dir, q, kind, year, camera, no_gps, no_date, dups)
            rule = plans.Rule(template.strip(), fallback, conflicts)
            try:
                items, _capped = _run_preview(c, flt, rule)
            except plans.RuleError:
                return RedirectResponse("/reorganize?" + _q_params(**source, template=template), status_code=303)
            pid = plans.save(c, name or rule.template, rule, source, items)
        finally:
            c.close()
        return RedirectResponse(f"/plans/{pid}?saved=1", status_code=303)

    @app.get("/plans", response_class=HTMLResponse)
    def plans_page(request: Request):
        c = conn()
        try:
            return render(request, "plans.html", plans=plans.list_plans(c), statuses=plans.STATUS)
        finally:
            c.close()

    @app.get("/plans/{plan_id}", response_class=HTMLResponse)
    def plan_page(request: Request, plan_id: int, status: str = "", page: Page = 1, saved: str = "",
                  checked: str = "", error: str = ""):
        c = conn()
        try:
            p = plans.get_plan(c, plan_id)
            if p is None:
                return Response("No such plan", status_code=404)
            per_page = 200
            items, total = plans.plan_items(c, plan_id, status, page, per_page)
            folders = plans.plan_folders(c, plan_id)
            gate = changes.gate(c, cfg.mnt)
            applied = c.execute("SELECT id, state, created_at FROM actions WHERE plan_id = ? ORDER BY id DESC",
                                (plan_id,)).fetchall()
        finally:
            c.close()
        pages = max(1, -(-total // per_page))
        return render(request, "plan.html", p=p, items=items, total=total, status=status, page=page, pages=pages,
                      statuses=plans.STATUS, folders=folders, saved=saved, checked=checked, error=error,
                      gate=gate, applied=applied,
                      src_qs=_q_params(**p["source"], template=p["rule"]["template"],
                                       fallback=p["rule"]["fallback"], conflicts=p["rule"]["conflicts"]))

    @app.post("/plans/{plan_id}/recheck")
    def plan_recheck(plan_id: int):
        c = conn()
        try:
            res = plans.recheck(c, plan_id)
        finally:
            c.close()
        return RedirectResponse(f"/plans/{plan_id}?checked={res['stale']}", status_code=303)

    @app.post("/plans/{plan_id}/delete")
    def plan_delete(plan_id: int):
        c = conn()
        try:
            with db.Tx(c):
                c.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
        finally:
            c.close()
        return RedirectResponse("/plans", status_code=303)

    @app.get("/plans/{plan_id}/export.csv")
    def plan_export(plan_id: int):
        c = conn()
        try:
            p = plans.get_plan(c, plan_id)
            if p is None:
                return Response(status_code=404)
            body = "".join(plans.export_csv(c, plan_id))
        finally:
            c.close()
        return Response(body, media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="deedoop-plan-{plan_id}.csv"'})

    # -- changes (milestone 6) -----------------------------------------------------

    def _back(back: str, default: str = "/duplicates") -> str:
        return safe_next(back, default)

    @app.post("/marks/group")
    def marks_group(group_id: int = Form(...), keep: int = Form(...), back: str = Form("")):
        c = conn()
        try:
            n = changes.mark_group(c, group_id, keep)
        finally:
            c.close()
        return RedirectResponse(_with_msg(_back(back), f"{n} marked for removal"), status_code=303)

    @app.post("/marks/group/{group_id}/clear")
    def marks_group_clear(group_id: int, back: str = Form("")):
        c = conn()
        try:
            changes.unmark_group(c, group_id)
        finally:
            c.close()
        return RedirectResponse(_with_msg(_back(back), "Marks cleared for this group"), status_code=303)

    @app.post("/marks/bulk")
    def marks_bulk(method: str = Form("exact"), category: str = Form(""), q: str = Form(""), min_mb: Annotated[float, Form(ge=0, le=1e9)] = 0,
                   disk: str = Form(""), rule: str = Form("suggested"), text: str = Form(""), back: str = Form("")):
        if rule not in changes.KEEPER_RULES or method == "similar":
            return RedirectResponse(_back(back), status_code=303)
        flt = queries.GroupFilter(method=method, category=category, q=q, min_size=int(min_mb * 1e6), disk=disk,
                                  per_page=10**9)
        c = conn()
        try:
            groups, _total, _wasted = queries.list_groups(c, flt)
            res = changes.mark_groups(c, [g["id"] for g in groups], rule, text.strip())
        finally:
            c.close()
        msg = f"{res['files']:,} copies in {res['groups']:,} groups marked for removal"
        if res["left"]:
            msg += f"; {res['left']:,} groups left alone (no copy matched the rule)"
        return RedirectResponse(_with_msg(_back(back), msg), status_code=303)

    @app.post("/marks/reviewed")
    def marks_reviewed():
        c = conn()
        try:
            s = db.get_settings(c)
            n = changes.mark_reviewed(c, threshold_of(s), s)
        finally:
            c.close()
        return RedirectResponse(_with_msg("/changes", f"{n} copies from Review marked for removal"), status_code=303)

    @app.post("/marks/clear")
    def marks_clear(file_id: Annotated[list[int] | None, Form()] = None, everything: str = Form("")):
        c = conn()
        try:
            changes.clear_marks(c, None if everything else (file_id or []))
        finally:
            c.close()
        return RedirectResponse(_with_msg("/changes", "All marks cleared" if everything else "Kept: no longer marked"),
                                status_code=303)

    @app.get("/changes", response_class=HTMLResponse)
    def changes_page(request: Request, page: Page = 1, msg: str = "", error: str = ""):
        per_page = 200
        c = conn()
        try:
            s = db.get_settings(c)
            summ = changes.marks_summary(c)
            rows = changes.marked(c, per_page, (max(page, 1) - 1) * per_page)
            reviewed = len(changes.all_review_removals(c, s, threshold_of(s)))
            return render(request, "changes.html", gate=changes.gate(c, cfg.mnt), summ=summ, rows=rows,
                          page=page, pages=max(1, -(-summ["files"] // per_page)), reviewed=reviewed,
                          actions=changes.list_actions(c), modes=changes.MODES, msg=msg, error=error, **job_view(c))
        finally:
            c.close()

    @app.post("/changes/remove")
    def changes_remove(request: Request, mode: str = Form("quarantine"), confirm: str = Form(""),
                       password: Annotated[str, Form(max_length=auth.MAX_PASSWORD)] = ""):
        if mode == "delete" and confirm.strip().lower() != "delete":
            return RedirectResponse(_with_msg("/changes", "Type DELETE to confirm a permanent delete", "error"),
                                    status_code=303)
        c = conn()
        try:
            if mode == "delete" and (err := _confirm(request, c, password)):
                return RedirectResponse(_with_msg("/changes", err, "error"), status_code=303)
            aid = changes.create_removal(c, cfg.mnt, mode)
            _who(request, c, aid)
        except changes.ChangeError as e:
            return RedirectResponse(_with_msg("/changes", str(e), "error"), status_code=303)
        finally:
            c.close()
        worker.wake()
        return RedirectResponse(f"/changes/{aid}", status_code=303)

    @app.get("/changes/{action_id}", response_class=HTMLResponse)
    def change_page(request: Request, action_id: int, status: str = "", page: Page = 1, msg: str = "",
                    error: str = ""):
        c = conn()
        try:
            a = changes.get_action(c, action_id)
            if a is None:
                return Response("No such change", status_code=404)
            per_page = 200
            items, total, counts = changes.action_items(c, action_id, status, page, per_page)
            return render(request, "change.html", a=a, items=items, total=total, counts=counts, status=status,
                          page=page, pages=max(1, -(-total // per_page)), statuses=changes.ITEM_STATUS,
                          modes=changes.MODES, gate=changes.gate(c, cfg.mnt), msg=msg, error=error, **job_view(c))
        finally:
            c.close()

    @app.post("/changes/{action_id}/undo")
    def change_undo(action_id: int):
        c = conn()
        try:
            changes.request_undo(c, cfg.mnt, action_id)
        except changes.ChangeError as e:
            return RedirectResponse(_with_msg(f"/changes/{action_id}", str(e), "error"), status_code=303)
        finally:
            c.close()
        worker.wake()
        return RedirectResponse(_with_msg(f"/changes/{action_id}", "Undo started"), status_code=303)

    @app.post("/changes/{action_id}/purge")
    def change_purge(request: Request, action_id: int, confirm: str = Form(""),
                     password: Annotated[str, Form(max_length=auth.MAX_PASSWORD)] = ""):
        if not confirm:
            return RedirectResponse(_with_msg(f"/changes/{action_id}", "Tick the box to confirm", "error"),
                                    status_code=303)
        c = conn()
        try:
            if err := _confirm(request, c, password):
                return RedirectResponse(_with_msg(f"/changes/{action_id}", err, "error"), status_code=303)
            changes.request_purge(c, cfg.mnt, action_id)
        except changes.ChangeError as e:
            return RedirectResponse(_with_msg(f"/changes/{action_id}", str(e), "error"), status_code=303)
        finally:
            c.close()
        worker.wake()
        return RedirectResponse(_with_msg(f"/changes/{action_id}", "Emptying the quarantine"), status_code=303)

    @app.post("/plans/{plan_id}/apply")
    def plan_apply(request: Request, plan_id: int, confirm: str = Form("")):
        if not confirm:
            return RedirectResponse(f"/plans/{plan_id}?error=" + _q("Tick the box to confirm"), status_code=303)
        c = conn()
        try:
            aid = changes.create_move(c, cfg.mnt, plan_id)
            _who(request, c, aid)
        except changes.ChangeError as e:
            return RedirectResponse(f"/plans/{plan_id}?error=" + _q(str(e)), status_code=303)
        finally:
            c.close()
        worker.wake()
        return RedirectResponse(f"/changes/{aid}", status_code=303)

    @app.get("/same-path", response_class=HTMLResponse)
    def same_path(request: Request):
        c = conn()
        try:
            return render(request, "same_path.html", rows=queries.same_path_on_two_disks(c),
                          errors=queries.file_errors(c))
        finally:
            c.close()

    @app.get("/log", response_class=HTMLResponse)
    def scan_log(request: Request, job: Annotated[int, Query(ge=0)] = 0, level: Annotated[str, Query(max_length=8)] = "all",
                 partial: bool = False):
        """The scan log of one job (milestone 7.5); the latest job by default."""
        c = conn()
        try:
            jobs = c.execute("SELECT id, type, state, started_at, finished_at FROM jobs "
                             "WHERE started_at IS NOT NULL ORDER BY id DESC LIMIT ?", (db.LOG_KEEP_JOBS,)).fetchall()
            chosen = next((j for j in jobs if j["id"] == job), jobs[0] if jobs else None)
            warnings_only = level == "warn"
            rows = db.job_log(c, chosen["id"], warnings_only) if chosen else []
            running = bool(chosen and chosen["state"] in ("queued", "running"))
            ctx = {"jobs": jobs, "job": chosen, "rows": rows, "level": "warn" if warnings_only else "all",
                   "running": running, "limit": 2000}
            if partial:
                return templates.TemplateResponse(request, "_log.html", ctx)
            return render(request, "log.html", **ctx)
        finally:
            c.close()

    @app.get("/export/duplicates.{fmt}")
    def export(fmt: str, method: str = "exact", category: str = "", q: str = "", min_mb: MinMb = 0, disk: str = "",
               archives: str = ""):
        flt = queries.GroupFilter(method=method, category=category, q=q, min_size=int(min_mb * 1e6), disk=disk,
                                  archives=bool(archives))
        if fmt not in ("csv", "json"):
            return Response(status_code=404)
        c = conn()

        def gen():
            try:
                yield from (queries.export_csv(c, flt) if fmt == "csv" else queries.export_json(c, flt))
            finally:
                c.close()

        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
        media = "text/csv" if fmt == "csv" else "application/json"
        return StreamingResponse(gen(), media_type=media, headers={
            "Content-Disposition": f'attachment; filename="deedoop-duplicates-{stamp}.{fmt}"'})

    # -- scan controls ----------------------------------------------------------

    @app.post("/scan/start")
    def scan_start():
        c = conn()
        try:
            if not db.get_settings(c)["shares"] and not c.execute(
                    "SELECT 1 FROM roots WHERE enabled = 1 AND source != 'share' LIMIT 1").fetchone():
                # Nothing to read: say so instead of a scan that "finishes" at once.
                return RedirectResponse("/?notice=noshares", status_code=303)
            if c.execute("SELECT 1 FROM jobs WHERE type = 'scan' AND state IN ('queued', 'running')").fetchone():
                return RedirectResponse("/", status_code=303)
            blocker = db.active_job(c)
            # Another job (a benchmark, a change, regrouping) is running: the scan
            # waits in the queue behind it instead of being silently dropped.
            db.enqueue_job(c, "scan")
        finally:
            c.close()
        worker.wake()
        return RedirectResponse("/?notice=queued" if blocker else "/", status_code=303)

    @app.post("/scan/pause")
    def scan_pause():
        worker.pause()
        return RedirectResponse("/", status_code=303)

    @app.post("/scan/resume")
    def scan_resume():
        worker.resume()
        return RedirectResponse("/", status_code=303)

    @app.post("/scan/stop")
    def scan_stop():
        c = conn()
        try:
            # A job still waiting in the queue is simply cancelled.
            c.execute("UPDATE jobs SET state = 'stopped', finished_at = strftime('%s','now') "
                      "WHERE state = 'queued'")
        finally:
            c.close()
        worker.stop()
        return RedirectResponse("/", status_code=303)

    # -- API ------------------------------------------------------------------

    @app.get("/api/status")
    def api_status():
        c = conn()
        try:
            v = job_view(c)
            return JSONResponse({"version": __version__, "build": build_label(), "stats": queries.index_stats(c, cfg.db_path),
                                 "active": v["active"], "live": v["live"], "paused": v["paused"]})
        finally:
            c.close()

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


# What the browser's <audio> and <video> elements are given (milestone 7).
PLAYABLE = {
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac", "flac": "audio/flac", "wav": "audio/wav",
    "ogg": "audio/ogg", "opus": "audio/ogg", "oga": "audio/ogg", "webm": "video/webm", "mp4": "video/mp4",
    "m4v": "video/mp4", "mov": "video/quicktime", "mkv": "video/x-matroska",
}

PLAN_LIMIT = 50_000  # files in one preview; narrow the folder or filters for more

PLACEHOLDER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 30"><rect width="40" height="30" fill="#8884"/>'
    '<path d="M8 22l8-9 6 6 4-4 6 7z" fill="#8888"/></svg>'
)


def threshold_of(settings: dict) -> int:
    try:
        return max(0, min(int(settings["similar_threshold"]), similar.MAX_DISTANCE))
    except (TypeError, ValueError):
        return similar.DEFAULT_THRESHOLD


def _q(s: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(s)


def _with_msg(url: str, text: str, key: str = "msg") -> str:
    base, _, frag = url.partition("#")
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{key}={_q(text)}" + (f"#{frag}" if frag else "")


def _q_params(**kw) -> str:
    from urllib.parse import urlencode

    return urlencode({k: v for k, v in kw.items() if v not in ("", None, 0)})

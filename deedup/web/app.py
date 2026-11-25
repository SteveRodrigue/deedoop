"""FastAPI web UI. Server-rendered Jinja pages with htmx for live updates.

The UI never reads or writes the user's files. It reads the index, queues
jobs for the worker and changes settings.
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
import secrets
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__, changes, db, plans, queries, similar
from ..archive_index import refresh_coverage
from ..archives import read_member_bytes
from ..categories import CATEGORIES
from ..config import AppConfig
from ..config import load as load_config
from ..discovery import RootError, add_custom_root, list_disks, list_shares, remove_root, sync_share_roots
from ..imaging import THUMB_EXTS, NotAnImage, make_thumbnail
from ..metadata import exiftool_available
from ..worker import Worker

HERE = Path(__file__).parent


def human_bytes(n: float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1000 or unit == "PB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} PB"


def fmt_time(ts: float | None) -> str:
    if not ts:
        return "never"
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def create_app(cfg: AppConfig | None = None, worker: Worker | None = None, start_worker: bool = True) -> FastAPI:
    cfg = cfg or load_config()
    db.init(cfg.db_path)
    worker = worker or Worker(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_worker:
            worker.start()
        yield
        if start_worker:
            worker.shutdown()

    app = FastAPI(title="deedup", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.cfg = cfg
    app.state.worker = worker
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["bytes"] = human_bytes
    templates.env.filters["time"] = fmt_time
    templates.env.globals["version"] = __version__

    if cfg.password:
        expected = "Basic " + base64.b64encode(f"deedup:{cfg.password}".encode()).decode()

        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            if request.url.path == "/healthz":
                return await call_next(request)
            got = request.headers.get("authorization", "")
            if not secrets.compare_digest(got.encode(), expected.encode()):
                return Response("Authentication required", status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="deedup"'})
            return await call_next(request)

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
        elapsed = fmt_duration(dt.datetime.now().timestamp() - live["started"]) if live.get("started") else ""
        return {
            "active": dict(active) if active else None,
            "last": dict(last) if last else None,
            "last_progress": json.loads(last["progress"]) if last else {},
            "live": live,
            "elapsed": elapsed,
            "paused": worker.control.paused if worker.current_job else False,
        }

    # -- pages ----------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        c = conn()
        try:
            stats = queries.index_stats(c, cfg.db_path)
            roots = c.execute("SELECT COUNT(*) FROM roots WHERE enabled = 1").fetchone()[0]
            return render(request, "dashboard.html", stats=stats, roots=roots,
                          by_cat=queries.wasted_by_category(c), disks=queries.per_disk(c),
                          settings=db.get_settings(c), **job_view(c))
        finally:
            c.close()

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
            bench = c.execute("SELECT * FROM benchmarks ORDER BY id DESC LIMIT 1").fetchone()
            disk_files = {r["disk"]: r["files"] for r in queries.per_disk(c)}
            return render(request, "setup.html", disks=disks, shares=shares, settings=settings,
                          roots=roots, root_counts=queries.root_counts(c), disk_files=disk_files,
                          bench=json.loads(bench["result"]) if bench else None, error=error, saved=saved,
                          mnt=str(cfg.mnt), exiftool=exiftool_available(), gate=changes.gate(c, cfg.mnt),
                          **job_view(c))
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
            db.set_settings(c, {
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
            })
        finally:
            c.close()
        worker.wake()
        return RedirectResponse("/setup?saved=settings", status_code=303)

    @app.post("/setup/changes")
    def setup_changes(allow_changes: str = Form(""), allow_direct_delete: str = Form(""),
                      quarantine_days: int = Form(30)):
        c = conn()
        try:
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
            if db.active_job(c) is None:
                db.enqueue_job(c, "benchmark")
        finally:
            c.close()
        worker.wake()
        return RedirectResponse("/setup#benchmark", status_code=303)

    @app.post("/setup/benchmark/apply")
    def setup_benchmark_apply():
        c = conn()
        try:
            bench = c.execute("SELECT result FROM benchmarks ORDER BY id DESC LIMIT 1").fetchone()
            if bench:
                db.set_settings(c, json.loads(bench["result"])["proposal"])
        finally:
            c.close()
        return RedirectResponse("/setup?saved=benchmark#benchmark", status_code=303)

    @app.get("/duplicates", response_class=HTMLResponse)
    def duplicates(request: Request, method: str = "exact", category: str = "", q: str = "", min_mb: float = 0,
                   disk: str = "", group: int = 0, archives: str = "", page: int = 1, msg: str = ""):
        method = method if method in queries.METHODS else "exact"
        flt = queries.GroupFilter(method=method, category=category, q=q, min_size=int(min_mb * 1e6), disk=disk,
                                  group=group, archives=bool(archives), page=page)
        c = conn()
        try:
            groups, total, wasted = queries.list_groups(c, flt)
            members = queries.group_members(c, [g["id"] for g in groups])
            disks = [r["disk"] for r in queries.per_disk(c)]
            pages = max(1, -(-total // flt.per_page))
            qs = _q_params(method=method if method != "exact" else "", category=category, q=q,
                           min_mb=min_mb or "", disk=disk, group=group or "", archives=archives)
            counts = {m: c.execute("SELECT COUNT(*), COALESCE(SUM(wasted), 0) FROM groups WHERE method = ? "
                                   "AND wasted > 0", (m,)).fetchone() for m in queries.METHODS}
            return render(request, "duplicates.html", groups=groups, members=members, total=total,
                          wasted=wasted, flt=flt, pages=pages, categories=CATEGORIES, disks=disks, qs=qs,
                          min_mb=min_mb or "", methods=queries.METHODS, counts=counts,
                          settings=db.get_settings(c), marked=changes.marked_in_groups(c, [g["id"] for g in groups]),
                          marks=changes.marks_summary(c), keeper_rules=changes.KEEPER_RULES, msg=msg,
                          back=str(request.url.path) + ("?" + str(request.url.query) if request.url.query else ""))
        finally:
            c.close()

    @app.get("/browse", response_class=HTMLResponse)
    def browse(request: Request, loc: str = "", dir: str = "", q: str = "", kind: str = "", year: str = "",
               camera: str = "", no_gps: str = "", no_date: str = "", dups: str = "", page: int = 1,
               view: str = "list"):
        c = conn()
        try:
            locs = queries.locations(c)
            if not loc and locs:
                loc = locs[0]["key"]
            flt = queries.BrowseFilter(loc=loc, dir=dir.strip("/"), q=q, kind=kind, year=year, camera=camera,
                                       no_gps=bool(no_gps), no_date=bool(no_date), dups=bool(dups), page=page)
            res = queries.browse(c, flt)
            crumbs, acc = [], []
            for part in flt.dir.split("/") if flt.dir else []:
                acc.append(part)
                crumbs.append((part, "/".join(acc)))
            filters = _q_params(q=q, kind=kind, year=year, camera=camera, no_gps=no_gps, no_date=no_date,
                                dups=dups, view=view if view != "list" else "")
            return render(request, "browse.html", locs=locs, flt=flt, res=res, crumbs=crumbs,
                          facets=queries.facets(c), categories=CATEGORIES, view=view, filters=filters,
                          pages=max(1, -(-res["total"] // flt.per_page)), loc=loc)
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
            r = c.execute(
                f"SELECT f.dir, f.name, f.ext, f.size, f.mtime_ns, r.path, {queries.CONTAINER_COLS} FROM files f "
                f"JOIN roots r ON r.id = f.root_id {queries.CONTAINER_JOINS} WHERE f.id = ?", (file_id,)).fetchone()
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

    @app.get("/review", response_class=HTMLResponse)
    def review(request: Request, view: str = "queue", done: str = ""):
        c = conn()
        try:
            s = db.get_settings(c)
            t = threshold_of(s)
            decided = view == "decided"
            pairs = [p for p in (queries.pair_view(c, r) for r in similar.queue(c, t, decided=decided,
                                                                                  limit=100 if decided else 2)) if p]
            return render(request, "review.html", pairs=pairs, view="decided" if decided else "queue",
                          counts=similar.counts(c, t), threshold=t, settings=s, decisions=similar.DECISIONS,
                          done=done, max_distance=similar.MAX_DISTANCE)
        finally:
            c.close()

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
        return RedirectResponse("/review?view=decided", status_code=303)

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
        return RedirectResponse(f"/review?view={'decided' if view == 'decided' else 'queue'}", status_code=303)

    @app.get("/archives", response_class=HTMLResponse)
    def archives_page(request: Request):
        c = conn()
        try:
            return render(request, "archives.html", archives=queries.archive_list(c), settings=db.get_settings(c))
        finally:
            c.close()

    @app.get("/archives/{file_id}", response_class=HTMLResponse)
    def archive_page(request: Request, file_id: int, status: str = "new", album: str = "", page: int = 1):
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
                   fallback: str = "undated", conflicts: str = "rename", status: str = "", page: int = 1):
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
    def plan_page(request: Request, plan_id: int, status: str = "", page: int = 1, saved: str = "",
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
            "Content-Disposition": f'attachment; filename="deedup-plan-{plan_id}.csv"'})

    # -- changes (milestone 6) -----------------------------------------------------

    def _back(back: str, default: str = "/duplicates") -> str:
        return back if back.startswith("/") and not back.startswith("//") else default

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
        return RedirectResponse(_back(back), status_code=303)

    @app.post("/marks/bulk")
    def marks_bulk(method: str = Form("exact"), category: str = Form(""), q: str = Form(""), min_mb: float = Form(0),
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
            n = changes.mark_reviewed(c, threshold_of(db.get_settings(c)))
        finally:
            c.close()
        return RedirectResponse(_with_msg("/changes", f"{n} photos from Review marked for removal"), status_code=303)

    @app.post("/marks/clear")
    def marks_clear(file_id: Annotated[list[int] | None, Form()] = None, everything: str = Form("")):
        c = conn()
        try:
            changes.clear_marks(c, None if everything else (file_id or []))
        finally:
            c.close()
        return RedirectResponse("/changes", status_code=303)

    @app.get("/changes", response_class=HTMLResponse)
    def changes_page(request: Request, page: int = 1, msg: str = "", error: str = ""):
        per_page = 200
        c = conn()
        try:
            s = db.get_settings(c)
            summ = changes.marks_summary(c)
            rows = changes.marked(c, per_page, (max(page, 1) - 1) * per_page)
            reviewed = len(changes.review_removals(c, threshold_of(s))) if s["similar_matching"] else 0
            return render(request, "changes.html", gate=changes.gate(c, cfg.mnt), summ=summ, rows=rows,
                          page=page, pages=max(1, -(-summ["files"] // per_page)), reviewed=reviewed,
                          actions=changes.list_actions(c), modes=changes.MODES, msg=msg, error=error, **job_view(c))
        finally:
            c.close()

    @app.post("/changes/remove")
    def changes_remove(mode: str = Form("quarantine"), confirm: str = Form("")):
        if mode == "delete" and confirm.strip().lower() != "delete":
            return RedirectResponse(_with_msg("/changes", "Type DELETE to confirm a permanent delete", "error"),
                                    status_code=303)
        c = conn()
        try:
            aid = changes.create_removal(c, cfg.mnt, mode)
        except changes.ChangeError as e:
            return RedirectResponse(_with_msg("/changes", str(e), "error"), status_code=303)
        finally:
            c.close()
        worker.wake()
        return RedirectResponse(f"/changes/{aid}", status_code=303)

    @app.get("/changes/{action_id}", response_class=HTMLResponse)
    def change_page(request: Request, action_id: int, status: str = "", page: int = 1, msg: str = "",
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
    def change_purge(action_id: int, confirm: str = Form("")):
        if not confirm:
            return RedirectResponse(_with_msg(f"/changes/{action_id}", "Tick the box to confirm", "error"),
                                    status_code=303)
        c = conn()
        try:
            changes.request_purge(c, cfg.mnt, action_id)
        except changes.ChangeError as e:
            return RedirectResponse(_with_msg(f"/changes/{action_id}", str(e), "error"), status_code=303)
        finally:
            c.close()
        worker.wake()
        return RedirectResponse(_with_msg(f"/changes/{action_id}", "Emptying the quarantine"), status_code=303)

    @app.post("/plans/{plan_id}/apply")
    def plan_apply(plan_id: int, confirm: str = Form("")):
        if not confirm:
            return RedirectResponse(f"/plans/{plan_id}?error=" + _q("Tick the box to confirm"), status_code=303)
        c = conn()
        try:
            aid = changes.create_move(c, cfg.mnt, plan_id)
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

    @app.get("/export/duplicates.{fmt}")
    def export(fmt: str, method: str = "exact", category: str = "", q: str = "", min_mb: float = 0, disk: str = "",
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
            "Content-Disposition": f'attachment; filename="deedup-duplicates-{stamp}.{fmt}"'})

    # -- scan controls ----------------------------------------------------------

    @app.post("/scan/start")
    def scan_start():
        c = conn()
        try:
            if db.active_job(c) is None:
                db.enqueue_job(c, "scan")
        finally:
            c.close()
        worker.wake()
        return RedirectResponse("/", status_code=303)

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
            return JSONResponse({"version": __version__, "stats": queries.index_stats(c, cfg.db_path),
                                 "active": v["active"], "live": v["live"], "paused": v["paused"]})
        finally:
            c.close()

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


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

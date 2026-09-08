"""Loopback-only browser adapter. No account setup, credentials, arbitrary files or SQL routes."""

import json
import secrets
import threading
from pathlib import Path

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.routing import Route

from .cycles import CycleReviewService
from .dashboard import DashboardService
from .journal import JournalService
from .sync import SyncAlreadyRunning, SyncService, sync_is_running
from .weekly import WeeklyService

ASSETS = Path(__file__).with_name("web_assets")
STATIC_FILES = {
    "app.css": ("app.css", "text/css"),
    "app.js": ("app.js", "text/javascript"),
    "chart.js": ("vendor/chart.umd.js", "text/javascript"),
}


class DeferredClient:
    """Construct the client only after the durable checkpoint and conditional gate exist."""

    def __init__(self, factory):
        self.factory, self.client = factory, None

    def _get(self):
        if self.client is None:
            self.client = self.factory()
        return self.client

    def list_records(self, *args):
        return self._get().list_records(*args)

    def cycle_by_id(self, *args):
        return self._get().cycle_by_id(*args)


class DashboardRuntime:
    def __init__(self, store_factory, *, client_factory=None, oauth_status=None):
        self.store_factory = store_factory
        self.client_factory = client_factory
        self.oauth_status = oauth_status
        self._lock = threading.Lock()
        self._busy = False
        self._stopping = False
        self._error = None
        self._error_after = None

    def prepare_shutdown(self):
        with self._lock:
            if self._busy:
                return False
            self._stopping = True
            return True

    def status(self):
        with self.store_factory() as store:
            store.purge_expired()
            service = SyncService(store, None)
            completed = store.db.execute(
                "SELECT completed_at,request FROM sync_runs WHERE status='completed' ORDER BY completed_at DESC,rowid DESC LIMIT 1"
            ).fetchone()
            current = service.latest()
            connected = None
            if self.oauth_status:
                try:
                    connected = bool(self.oauth_status()["connected"])
                except Exception:
                    connected = False
            with self._lock:
                if (
                    self._error
                    and current
                    and current["status"] == "completed"
                    and (current["run_id"], current["status"]) != self._error_after
                ):
                    self._error = None
                busy, error = self._busy, self._error
            busy = busy or sync_is_running(store.path.with_suffix(".sync.lock"))
            allowed = not store.policy or "whoop" in store.policy.sources
            can_sync = (
                self.client_factory is not None
                and allowed
                and (connected is True if store.environment == "real" else connected is not False)
            )
            plans = {
                str(days): service.plan_catch_up(*DashboardService(store).window(days))
                for days in (7, 30)
            }
            return {
                "environment": store.environment,
                "connected": connected,
                "running": busy,
                "last_run": current,
                "last_success_at": completed["completed_at"] if completed else None,
                "last_success_window": json.loads(completed["request"]) if completed else None,
                "error": error,
                "sync_enabled": self.client_factory is not None,
                "can_sync": can_sync,
                "sync_blocked": (
                    "disabled"
                    if self.client_factory is None
                    else "policy"
                    if not allowed
                    else "authorization"
                    if not can_sync
                    else None
                ),
                "checked_at": store.clock(),
                "freshness": {
                    days: service.freshness(**plan["request"]) for days, plan in plans.items()
                },
                "sync_plan": plans,
                "retention_days": store.policy.retention_days if store.policy else None,
            }

    def start_sync(self, days: int, resume: str | None = None, *, if_stale: bool = False):
        if self.client_factory is None:
            raise ValueError("演示环境只刷新视图，不访问 WHOOP。")
        if type(days) is not int or days not in (7, 30):
            raise ValueError("请选择 7 天或 30 天。")
        if resume is not None and (not isinstance(resume, str) or not resume or len(resume) > 64):
            raise ValueError("同步断点无效。")
        if type(if_stale) is not bool or (if_stale and resume is not None):
            raise ValueError("按需同步不能同时指定断点。")
        status = self.status()
        if status["running"]:
            return {"accepted": True, "already_running": True}
        if not status["can_sync"]:
            return {"accepted": False, "reason": status["sync_blocked"]}
        current = status["last_run"]
        if resume and (
            not current or current["run_id"] != resume or current["status"] == "completed"
        ):
            return {"accepted": False, "reason": "checkpoint_unavailable"}
        if if_stale:
            if current and current["status"] != "completed":
                return {"accepted": False, "reason": "unfinished"}
            if status["freshness"][str(days)]["state"] == "fresh":
                return {"accepted": False, "reason": "fresh"}
        with self._lock:
            if self._stopping:
                return {"accepted": False, "reason": "stopping"}
            if self._busy:
                return {"accepted": True, "already_running": True}
            self._busy, self._error = True, None

        def work():
            try:
                with self.store_factory() as store:
                    service = SyncService(store, DeferredClient(self.client_factory))
                    if resume:
                        service.run(run_id=resume, max_pages=100)
                    else:
                        start, end = DashboardService(store).window(days)
                        service.run(start, end, max_pages=100, if_stale=if_stale, catch_up=True)
            except SyncAlreadyRunning:
                pass  # Another local process owns the same shared sync lock; status follows it.
            except Exception:
                # SyncService persists its sanitized error and cursor; never send arbitrary exception text.
                with self._lock:
                    self._error = "同步未完成。请查看断点状态，必要时在本机重新授权后继续。"
                    self._error_after = (current["run_id"], current["status"]) if current else None
            finally:
                with self._lock:
                    self._busy = False

        thread = threading.Thread(target=work, name="whoop-dashboard-sync", daemon=True)
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._busy = False
            raise
        return {"accepted": True, "already_running": False}


class LocalBoundary:
    """Reject rebinding/cross-site requests before even reading the real database."""

    def __init__(self, app, *, port: int):
        self.app = app
        self.authority = f"127.0.0.1:{port}"
        self.origin = f"http://{self.authority}"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        client = scope.get("client")
        rejected = (
            headers.get("host") != self.authority
            or (client and client[0] != "127.0.0.1")
            or headers.get("origin", self.origin) != self.origin
            or headers.get("sec-fetch-site", "none") not in {"none", "same-origin"}
            or (scope["method"] == "POST" and headers.get("origin") != self.origin)
        )

        response_started = False

        async def secured_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    (
                        b"content-security-policy",
                        b"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
                    ),
                ]
            await send(message)

        if rejected:
            await JSONResponse({"error": "此看板只接受本机同源访问。"}, status_code=403)(
                scope, receive, secured_send
            )
            return
        try:
            await self.app(scope, receive, secured_send)
        except Exception:
            # Starlette re-raises even after its generic error response. Do not let Uvicorn
            # log arbitrary exception text from a protected database or provider response.
            if not response_started:
                await JSONResponse(
                    {"error": "本地服务暂时不可用，请检查本机配置后重试。"}, status_code=503
                )(scope, receive, secured_send)


def create_app(runtime: DashboardRuntime, port: int = 8766, *, launcher=None, shutdown=None):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Dashboard port must be between 1024 and 65535")
    session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

    def authorized(request):
        return secrets.compare_digest(request.cookies.get("whoop_local_session", ""), session)

    def home(request):
        response = HTMLResponse((ASSETS / "index.html").read_text().replace("__CSRF_TOKEN__", csrf))
        response.set_cookie(
            "whoop_local_session", session, httponly=True, samesite="strict", max_age=28800
        )
        return response

    def ready(request):
        if launcher is None:
            return JSONResponse({"error": "接口不可用。"}, status_code=404)
        if (
            list(request.query_params) != ["challenge"]
            or len(request.query_params.multi_items()) != 1
        ):
            return JSONResponse({"error": "启动请求无效。"}, status_code=400)
        try:
            return JSONResponse(launcher.ready(request.query_params["challenge"]))
        except ValueError:
            return JSONResponse({"error": "启动请求无效。"}, status_code=400)

    def stop(request):
        if launcher is None or shutdown is None:
            return JSONResponse({"error": "接口不可用。"}, status_code=404)
        if not launcher.authorize_stop(request.headers):
            return JSONResponse({"error": "启动请求无效。"}, status_code=403)
        if runtime.status()["running"] or not runtime.prepare_shutdown():
            return JSONResponse({"error": "同步仍在进行，请稍后关闭。"}, status_code=409)
        launcher.mark_stopping()
        return JSONResponse(
            {"stopping": True}, status_code=202, background=BackgroundTask(shutdown)
        )

    def static(request):
        entry = STATIC_FILES.get(request.path_params["asset"])
        if not entry:
            return JSONResponse({"error": "文件不存在。"}, status_code=404)
        return FileResponse(ASSETS / entry[0], media_type=entry[1])

    def dashboard(request):
        if not authorized(request):
            return JSONResponse({"error": "请重新打开本地看板。"}, status_code=401)
        try:
            days = int(request.query_params.get("days", "7"))
            key = request.query_params.get("metric", "hrv")
            with runtime.store_factory() as store:
                return JSONResponse(DashboardService(store).overview(key, days))
        except ValueError:
            return JSONResponse(
                {"error": "无法读取此窗口，请刷新或选择有效指标。"}, status_code=400
            )

    def evidence(request):
        if not authorized(request):
            return JSONResponse({"error": "请重新打开本地看板。"}, status_code=401)
        try:
            with runtime.store_factory() as store:
                result = DashboardService(store).evidence(
                    request.query_params.get("metric", ""),
                    int(request.query_params.get("revision", "0")),
                )
                return JSONResponse(result)
        except ValueError:
            return JSONResponse(
                {"error": "来源记录不可用，可能已过期；请刷新视图。"}, status_code=404
            )

    def status(request):
        if not authorized(request):
            return JSONResponse({"error": "请重新打开本地看板。"}, status_code=401)
        return JSONResponse(runtime.status())

    def journal(request):
        if not authorized(request):
            return JSONResponse({"error": "请重新打开本地看板。"}, status_code=401)
        try:
            if not set(request.query_params) <= {"days", "page"}:
                raise ValueError
            with runtime.store_factory() as store:
                return JSONResponse(
                    JournalService(store).timeline(
                        int(request.query_params.get("days", "7")),
                        int(request.query_params.get("page", "1")),
                    )
                )
        except ValueError:
            return JSONResponse(
                {"error": "无法读取日志，请刷新或检查本机导入映射。"}, status_code=400
            )

    def journal_evidence(request):
        if not authorized(request):
            return JSONResponse({"error": "请重新打开本地看板。"}, status_code=401)
        try:
            if set(request.query_params) != {"revision"}:
                raise ValueError
            with runtime.store_factory() as store:
                return JSONResponse(
                    JournalService(store).evidence(int(request.query_params["revision"]))
                )
        except ValueError:
            return JSONResponse(
                {"error": "日志来源已更新、删除或过期，请刷新时间线。"}, status_code=404
            )

    def cycles(request):
        if not authorized(request):
            return JSONResponse({"error": "请重新打开本地看板。"}, status_code=401)
        try:
            if not set(request.query_params) <= {"days", "page"} or len(
                request.query_params.multi_items()
            ) != len(request.query_params):
                raise ValueError
            with runtime.store_factory() as store:
                return JSONResponse(
                    CycleReviewService(store).review(
                        int(request.query_params.get("days", "7")),
                        int(request.query_params.get("page", "1")),
                    )
                )
        except ValueError:
            return JSONResponse(
                {"error": "无法读取恢复回看，请刷新或选择有效窗口。"}, status_code=400
            )

    def weekly(request):
        if not authorized(request):
            return JSONResponse({"error": "请重新打开本地看板。"}, status_code=401)
        try:
            if not set(request.query_params) <= {"week"} or len(
                request.query_params.multi_items()
            ) != len(request.query_params):
                raise ValueError
            with runtime.store_factory() as store:
                return JSONResponse(WeeklyService(store).review(request.query_params.get("week")))
        except ValueError:
            return JSONResponse({"error": "无法读取这一周，请刷新或选择可用周。"}, status_code=400)

    def weekly_records(request):
        if not authorized(request):
            return JSONResponse({"error": "请重新打开本地看板。"}, status_code=401)
        try:
            if not set(request.query_params) <= {"week", "metric", "page"} or len(
                request.query_params.multi_items()
            ) != len(request.query_params):
                raise ValueError
            with runtime.store_factory() as store:
                return JSONResponse(
                    WeeklyService(store).records(
                        request.query_params.get("week"),
                        request.query_params.get("metric", "recovery"),
                        int(request.query_params.get("page", "1")),
                    )
                )
        except ValueError:
            return JSONResponse({"error": "无法读取周记录，请刷新或重新选择。"}, status_code=400)

    async def sync(request: Request):
        if not authorized(request):
            return JSONResponse({"error": "请重新打开本地看板。"}, status_code=401)
        if not secrets.compare_digest(request.headers.get("x-whoop-csrf", ""), csrf):
            return JSONResponse({"error": "请求已失效，请刷新页面。"}, status_code=403)
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return JSONResponse({"error": "请求格式无效。"}, status_code=415)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 2048:
                return JSONResponse({"error": "请求过大。"}, status_code=413)
        try:
            values = json.loads(body)
            if not isinstance(values, dict) or not set(values) <= {"days", "resume", "if_stale"}:
                raise ValueError
            # Only launch a bounded sync here; the worker owns its connection and WHOOP client.
            return JSONResponse(
                runtime.start_sync(
                    values.get("days", 7),
                    values.get("resume"),
                    **({"if_stale": values["if_stale"]} if "if_stale" in values else {}),
                ),
                status_code=202,
            )
        except (ValueError, TypeError):
            return JSONResponse({"error": "无法开始同步，请检查环境和窗口。"}, status_code=400)

    async def unexpected(request, exc):
        return JSONResponse(
            {"error": "本地服务暂时不可用，请检查本机配置后重试。"}, status_code=503
        )

    app = Starlette(
        debug=False,
        routes=[
            Route("/", home),
            Route("/api/ready", ready),
            Route("/api/launcher/stop", stop, methods=["POST"]),
            Route("/assets/{asset}", static),
            Route("/api/dashboard", dashboard),
            Route("/api/evidence", evidence),
            Route("/api/status", status),
            Route("/api/cycles", cycles),
            Route("/api/journal", journal),
            Route("/api/journal/evidence", journal_evidence),
            Route("/api/weekly", weekly),
            Route("/api/weekly/records", weekly_records),
            Route("/api/sync", sync, methods=["POST"]),
        ],
        exception_handlers={Exception: unexpected},
    )
    return LocalBoundary(app, port=port)


def run_dashboard(args):
    if type(args.port) is not int or not 1024 <= args.port <= 65535:
        raise ValueError("Dashboard port must be between 1024 and 65535")
    import uvicorn

    from .launcher import LauncherControl
    from .live_cli import oauth_for, store_for
    from .storage import Store
    from .whoop_client import WhoopClient

    if args.demo:
        if args.environment != "synthetic":
            raise ValueError("Demo data cannot be loaded into a real database")
        from .dashboard_demo import seed_demo

        args.db = args.db or Path("runtime/synthetic/dashboard-demo.sqlite3")
        with store_for(args) as store:
            seed_demo(store)
    with store_for(args) as store:
        path, environment, key = store.path, store.environment, store.encryption_key
    oauth = oauth_for(args.oauth_config) if environment == "real" else None
    runtime = DashboardRuntime(
        lambda: Store(path, environment=environment, encryption_key=key),
        client_factory=(lambda: WhoopClient(oauth)) if oauth else None,
        oauth_status=oauth.status if oauth else None,
    )
    launcher = LauncherControl(args)
    server = None

    def shutdown():
        server.should_exit = True

    app = create_app(runtime, args.port, launcher=launcher, shutdown=shutdown)
    print(f"WHOOP 本地看板：http://127.0.0.1:{args.port} · {environment}", flush=True)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=args.port,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        ws="none",
        log_level="warning",
    )
    server = uvicorn.Server(config)
    server.run()

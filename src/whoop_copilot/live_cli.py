"""Local setup and WHOOP workflow. No secrets are accepted in command-line arguments."""

import getpass
import hashlib
import json
import os
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .credentials import KeychainVault, database_key
from .exports import inspect_export, parse_export
from .oauth import DEFAULT_SCOPES, OAuthConfig, WhoopOAuth
from .protection import LocalPolicy
from .storage import DEFAULT_DB, SCHEMA_VERSION, Store, canonical
from .sync import SyncService, sync_lock
from .whoop_client import WhoopClient

REAL_DB = Path("runtime/real/copilot.sqlite3")
OAUTH_CONFIG = Path("runtime/real/oauth-config.json")


def add_commands(sub):
    init = sub.add_parser("init-real", help="Initialize an encrypted, locally authorized database")
    init.add_argument("--retention-days", type=int, required=True)
    init.add_argument("--allow-source", action="append", choices=["whoop", "whoop_export"])
    init.add_argument(
        "--accept-local-storage",
        action="store_true",
        help="Authorize local storage under the stated retention policy",
    )
    inspect = sub.add_parser(
        "export-inspect", help="Inspect CSV/ZIP headers without outputting row values"
    )
    inspect.add_argument("path", type=Path)
    load = sub.add_parser("import-export")
    load.add_argument("path", type=Path)
    load.add_argument("--profile", type=Path, required=True)
    load.add_argument("--exported-at", required=True)
    load.add_argument("--synthetic", action="store_true")
    sub.add_parser("purge-expired")
    forget = sub.add_parser(
        "forget-source", help="Remove a source and dependent results and managed backups"
    )
    forget.add_argument("provider", choices=["whoop", "whoop_export"])
    forget.add_argument("--confirm-provider", required=True)
    whoop = sub.add_parser("whoop")
    child = whoop.add_subparsers(dest="whoop_command", required=True)
    configure = child.add_parser("configure")
    configure.add_argument("--client-id", required=True)
    configure.add_argument("--redirect-uri", default="http://127.0.0.1:8765/oauth/callback")
    child.add_parser("status")
    child.add_parser("login")
    child.add_parser("disconnect")
    sync = child.add_parser("sync")
    sync.add_argument("--start")
    sync.add_argument("--end")
    sync.add_argument("--resume")
    sync.add_argument("--max-pages", type=int, default=100)
    status = child.add_parser("sync-status")
    status.add_argument("run_id")


def store_for(args):
    environment = args.environment
    path = args.db or (REAL_DB if environment == "real" else DEFAULT_DB)
    if environment == "real":
        if not path.is_file():
            raise ValueError("Run init-real with an explicit local storage policy first")
        return Store(path, environment="real", encryption_key=database_key(path))
    return Store(path)


def oauth_for(config_path: Path, config: OAuthConfig | None = None, secret: str | None = None):
    if config is None:
        if not config_path.is_file():
            raise ValueError("Configure your WHOOP developer app before authorization")
        raw = json.loads(config_path.read_text())
        config = OAuthConfig(raw["client_id"], raw["redirect_uri"], tuple(raw["scopes"]))
    identity = hashlib.sha256(
        canonical(
            {
                "client_id": config.client_id,
                "redirect_uri": config.redirect_uri,
                "scopes": config.scopes,
            }
        ).encode()
    ).hexdigest()
    vault = KeychainVault("oauth:" + identity, config_path.parent / ("oauth-" + identity + ".lock"))
    return WhoopOAuth(config, vault, client_secret=secret)


def login(oauth: WhoopOAuth, *, opener=webbrowser.open, timeout=180) -> dict:
    parts = urlsplit(oauth.config.redirect_uri)
    if parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.port is None:
        raise ValueError(
            "This local login command requires the registered http://127.0.0.1:<port>/path callback"
        )
    result = []

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Callback query strings contain single-use authorization codes.

        def do_GET(self):
            if urlsplit(self.path).path != parts.path:
                self.send_error(404)
                return
            try:
                status = oauth.finish(oauth.config.redirect_uri + "?" + urlsplit(self.path).query)
                result.append(status)
                code, body = 200, b"WHOOP authorization completed. Return to your terminal."
            except ValueError:
                code, body = (
                    400,
                    b"Authorization was not accepted. Return to your terminal to retry.",
                )
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

    class LoopbackServer(HTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(5)
            return connection, address

    with LoopbackServer(("127.0.0.1", parts.port), Callback) as server:
        server.timeout = 1
        started = oauth.begin()
        opener(started["authorization_url"])
        print("已打开 WHOOP 授权页；本地等待回调。", file=__import__("sys").stderr)
        deadline = time.monotonic() + timeout
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result:
        raise ValueError("Local authorization timed out; run login again when ready")
    return result[0]


def execute_live(args):
    if args.command == "init-real":
        policy = LocalPolicy(
            args.retention_days,
            tuple(dict.fromkeys(args.allow_source or ["whoop", "whoop_export"])),
            args.accept_local_storage,
        )
        path = args.db or REAL_DB
        key = database_key(path, create=not path.exists())
        with Store(path, environment="real", encryption_key=key, policy=policy):
            return {
                "environment": "real",
                "database": str(path.resolve()),
                "schema_version": SCHEMA_VERSION,
                "retention_days": policy.retention_days,
                "sources": policy.sources,
                "model_transmission": False,
            }
    if args.command == "export-inspect":
        return inspect_export(args.path)
    if args.command == "import-export":
        if args.synthetic != (args.environment == "synthetic"):
            raise ValueError(
                "Use --synthetic for test exports, or --environment real for actual exports"
            )
        with store_for(args) as store:
            profile = json.loads(args.profile.read_text())
            return store.ingest(
                parse_export(args.path, profile, args.exported_at, synthetic=args.synthetic)
            )
    if args.command in {"purge-expired", "forget-source"}:
        if args.command == "forget-source" and args.provider != args.confirm_provider:
            raise ValueError("Confirm the exact source before removing it")
        with store_for(args) as store:
            with sync_lock(store.path.with_suffix(".sync.lock")):
                return (
                    store.purge_expired()
                    if args.command == "purge-expired"
                    else store.forget_source(args.provider)
                )
    if args.command == "whoop":
        if args.whoop_command == "configure":
            config = OAuthConfig(args.client_id, args.redirect_uri)
            secret = getpass.getpass("WHOOP client secret（输入不显示）：")
            oauth_for(args.oauth_config, config, secret)
            args.oauth_config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(
                args.oauth_config, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "w") as file:
                json.dump(
                    {
                        "client_id": config.client_id,
                        "redirect_uri": config.redirect_uri,
                        "scopes": DEFAULT_SCOPES,
                    },
                    file,
                )
            return {
                "configured": True,
                "secret_storage": "OS keychain",
                "redirect_uri": config.redirect_uri,
            }
        if args.whoop_command in {"sync", "sync-status"}:
            if args.environment != "real":
                raise ValueError("WHOOP network sync requires --environment real")
            with store_for(args) as store:
                oauth = oauth_for(args.oauth_config)
                service = SyncService(store, WhoopClient(oauth))
                if args.whoop_command == "sync-status":
                    return service.status(args.run_id)
                return service.run(
                    args.start, args.end, run_id=args.resume, max_pages=args.max_pages
                )
        oauth = oauth_for(args.oauth_config)
        if args.whoop_command == "status":
            return oauth.status()
        if args.whoop_command == "login":
            return login(oauth)
        if args.whoop_command == "disconnect":
            return oauth.disconnect()
    raise ValueError("Unknown local connection command")

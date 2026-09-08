"""User-invoked local dashboard lifecycle; no scheduler, login item or health-data log."""

import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
import webbrowser
from contextlib import contextmanager
from pathlib import Path

import httpx

from .credentials import CredentialError, KeychainVault

PROTOCOL = "whoop-local-launcher/1"
CHALLENGE = re.compile(r"[0-9a-f]{64}\Z")
WAIT_SECONDS = 20
REQUEST_SECONDS = 1


def _configuration(args):
    from .live_cli import REAL_DB
    from .storage import DEFAULT_DB

    if type(args.port) is not int or not 1024 <= args.port <= 65535:
        raise ValueError("Dashboard port must be between 1024 and 65535")
    if args.demo and args.environment != "synthetic":
        raise ValueError("Demo data cannot be loaded into a real database")
    default = (
        Path("runtime/synthetic/dashboard-demo.sqlite3")
        if args.demo
        else REAL_DB
        if args.environment == "real"
        else DEFAULT_DB
    )
    return {
        "project": str(Path.cwd().resolve()),
        "database": str((args.db or default).resolve()),
        "environment": args.environment,
        "oauth_config": str(args.oauth_config.resolve()),
        "port": args.port,
        "demo": args.demo,
    }


def _private_file(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError("本地启动文件权限无效，请检查 runtime/launcher。")
    return fd


class LauncherControl:
    """A private random token proves the exact project/configuration, without exposing paths."""

    def __init__(self, args):
        self.config = _configuration(args)
        self.url = f"http://127.0.0.1:{args.port}"
        identity = hashlib.sha256(
            json.dumps(self.config, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        directory = Path(self.config["project"]) / "runtime/launcher"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("本地启动目录权限无效，请检查 runtime/launcher。")
        self.path = directory / (identity + ".key")
        self.lock_path = directory / (identity + ".lock")
        self.identity_lock_path = directory / (identity + ".identity.lock")
        # Serialize first creation as a process can observe an O_EXCL-created file before its write.
        with self.lock(identity=True):
            if args.environment == "real":
                vault = KeychainVault("launcher:" + identity, self.identity_lock_path)
                record = vault.read()
                if not record:
                    record = {"launcher_key": secrets.token_hex(32)}
                    vault.write(record)
                value = record.get("launcher_key")
                if not isinstance(value, str) or not CHALLENGE.fullmatch(value):
                    raise CredentialError("Local launcher keychain entry is invalid")
                self._key = bytes.fromhex(value)
            else:
                if not self.path.exists():
                    fd = _private_file(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    with os.fdopen(fd, "wb") as file:
                        file.write(secrets.token_bytes(32))
                fd = _private_file(self.path, os.O_RDONLY)
                with os.fdopen(fd, "rb") as file:
                    self._key = file.read(33)
            if len(self._key) != 32:
                raise ValueError("本地启动身份文件无效，请检查 runtime/launcher。")
        self._stop_used = False

    @contextmanager
    def lock(self, *, timeout=WAIT_SECONDS, identity=False):
        path = self.identity_lock_path if identity else self.lock_path
        fd = _private_file(path, os.O_CREAT | os.O_RDWR)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ValueError("另一个本地启动操作仍在进行，请稍后重试。") from None
                    time.sleep(0.1)
            yield
        finally:
            os.close(fd)

    def _signature(self, purpose, value):
        return hmac.new(
            self._key, f"{PROTOCOL}:{purpose}:{value}".encode(), hashlib.sha256
        ).hexdigest()

    def ready(self, challenge):
        if not isinstance(challenge, str) or not CHALLENGE.fullmatch(challenge):
            raise ValueError("Invalid launcher challenge")
        return {"protocol": PROTOCOL, "proof": self._signature("ready", challenge)}

    def stop_headers(self):
        challenge = secrets.token_hex(32)
        issued = str(int(time.time()))
        return {
            "Origin": self.url,
            "X-Whoop-Launcher-Challenge": challenge,
            "X-Whoop-Launcher-Time": issued,
            "X-Whoop-Launcher-Proof": self._signature("stop", f"{issued}:{challenge}"),
        }

    def authorize_stop(self, headers):
        challenge = headers.get("x-whoop-launcher-challenge", "")
        issued = headers.get("x-whoop-launcher-time", "")
        proof = headers.get("x-whoop-launcher-proof", "")
        if (
            self._stop_used
            or not CHALLENGE.fullmatch(challenge)
            or not re.fullmatch(r"[0-9]{1,12}", issued)
            or abs(time.time() - int(issued)) > 30
        ):
            return False
        return hmac.compare_digest(proof, self._signature("stop", f"{issued}:{challenge}"))

    def mark_stopping(self):
        self._stop_used = True


def _response(control, method, path, *, params=None, headers=None, transport=None):
    async def read():
        # An httpx timeout alone limits inactivity, not trickled headers/body duration.
        async with asyncio.timeout(REQUEST_SECONDS):
            async with httpx.AsyncClient(
                timeout=REQUEST_SECONDS,
                trust_env=False,
                follow_redirects=False,
                transport=transport,
            ) as client:
                async with client.stream(
                    method,
                    control.url + path,
                    params=params,
                    headers={"Accept-Encoding": "identity", **(headers or {})},
                ) as response:
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        return response.status_code, None
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > 1024:
                            return response.status_code, None
                        body.extend(chunk)
                    try:
                        parsed = json.loads(body)
                    except (ValueError, TypeError):
                        parsed = None
                    return response.status_code, parsed

    return asyncio.run(read())


def _probe(control, *, transport=None):
    challenge = secrets.token_hex(32)
    try:
        status, body = _response(
            control, "GET", "/api/ready", params={"challenge": challenge}, transport=transport
        )
    except httpx.ConnectError:
        return "absent"
    except (httpx.HTTPError, TimeoutError):
        return "occupied"
    return "ready" if status == 200 and body == control.ready(challenge) else "occupied"


def _command(control):
    config = control.config
    return [
        sys.executable,
        "-m",
        "whoop_copilot.cli",
        "--environment",
        config["environment"],
        "--db",
        config["database"],
        "--oauth-config",
        config["oauth_config"],
        "dashboard",
        "--port",
        str(config["port"]),
        *(["--demo"] if config["demo"] else []),
    ]


def _occupied():
    return ValueError(
        "端口已被其他服务或旧版看板占用。请先关闭对应服务后重试；启动器不会打开或终止未知进程。"
    )


def _end_child(child):
    # Only this launcher's own child is terminated; never infer a PID from a stale file.
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)


def open_dashboard(args):
    control = LauncherControl(args)
    with control.lock():
        state = _probe(control)
        reused = state == "ready"
        if state == "occupied":
            raise _occupied()
        if not reused:
            if args.environment == "real" and not Path(control.config["database"]).is_file():
                raise ValueError("请先完成本机加密数据库初始化。")
            child = subprocess.Popen(
                _command(control),
                cwd=control.config["project"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            deadline = time.monotonic() + WAIT_SECONDS
            try:
                while True:
                    if child.poll() is not None:
                        raise ValueError("看板启动失败。请在本机运行 dashboard 命令检查配置。")
                    state = _probe(control)
                    if state == "ready":
                        break
                    if state == "occupied":
                        raise _occupied()
                    if time.monotonic() >= deadline:
                        raise ValueError("看板未能在 20 秒内就绪，请检查本机配置后重试。")
                    time.sleep(0.1)
            except BaseException:
                _end_child(child)
                raise
        try:
            opened = bool(webbrowser.open(control.url + "/"))
        except (webbrowser.Error, OSError):
            opened = False
        return {"started": not reused, "reused": reused, "opened": opened, "url": control.url + "/"}


def stop_dashboard(args):
    control = LauncherControl(args)
    with control.lock():
        state = _probe(control)
        if state == "absent":
            return {"stopped": True, "already_stopped": True}
        if state != "ready":
            raise _occupied()
        try:
            status, body = _response(
                control, "POST", "/api/launcher/stop", headers=control.stop_headers()
            )
            if status == 409:
                raise ValueError("同步仍在进行，请等待同步完成后再关闭看板。")
            if status != 202 or body != {"stopping": True}:
                raise ValueError("看板未接受关闭请求，请稍后重试。")
            deadline = time.monotonic() + WAIT_SECONDS
            while _probe(control) == "ready":
                if time.monotonic() >= deadline:
                    raise ValueError("看板正在关闭，请稍后重试。")
                time.sleep(0.1)
        except (httpx.HTTPError, TimeoutError):
            raise ValueError("未能确认看板关闭，请稍后重试。") from None
        return {"stopped": True, "already_stopped": False}

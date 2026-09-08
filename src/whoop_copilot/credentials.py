"""Explicit OS-keychain entries and process-safe locks; never a plaintext fallback."""

import fcntl
import hashlib
import json
import os
import secrets
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import keyring

SERVICE = "WHOOP Personal Copilot"
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_SECURE_BACKENDS = {
    ("keyring.backends.macOS", "Keyring"),
    ("keyring.backends.Windows", "WinVaultKeyring"),
    ("keyring.backends.SecretService", "Keyring"),
    ("keyring.backends.libsecret", "Keyring"),
}


class CredentialError(ValueError):
    """Safe-to-display keychain error without backend exception details."""


class KeychainVault:
    def __init__(self, account: str, lock_path: str | Path, backend=None):
        if not account or len(account) > 512:
            raise CredentialError("Invalid keychain account identifier")
        try:
            self.backend = backend if backend is not None else keyring.get_keyring()
            cls = type(self.backend)
            if (cls.__module__, cls.__name__) not in _SECURE_BACKENDS:
                raise CredentialError("An OS secure keyring backend is required")
        except Exception:
            raise CredentialError("An OS secure keyring backend is required") from None
        self.account = account
        self.lock_path = Path(lock_path).absolute()

    def read(self) -> dict[str, Any]:
        try:
            raw = self.backend.get_password(SERVICE, self.account)
            result = json.loads(raw) if raw is not None else {}
            if not isinstance(result, dict):
                raise ValueError
            return result
        except Exception:
            raise CredentialError("Could not read this application's keychain entry") from None

    def write(self, value: dict[str, Any]) -> None:
        try:
            self.backend.set_password(SERVICE, self.account, json.dumps(value, allow_nan=False))
        except Exception:
            raise CredentialError("Could not save this application's keychain entry") from None

    def delete(self) -> None:
        try:
            if self.backend.get_password(SERVICE, self.account) is not None:
                self.backend.delete_password(SERVICE, self.account)
        except Exception:
            raise CredentialError("Could not remove this application's keychain entry") from None

    @contextmanager
    def locked(self):
        """Serialize the complete read/request/write transaction across processes."""
        lock_id = str(self.lock_path.resolve())
        with _LOCKS_GUARD:
            local_lock = _LOCKS.setdefault(lock_id, threading.RLock())
        with local_lock:
            self.lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            except OSError:
                raise CredentialError("Could not open the credential transaction lock") from None
            try:
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)


def database_key(path: str | Path, create: bool = False) -> bytes:
    """Resolve only this database's 256-bit SQLCipher key in the OS keychain."""
    target = Path(path).resolve()
    account = "database:" + hashlib.sha256(str(target).encode()).hexdigest()
    vault = KeychainVault(account, target.parent / f".{target.name}.key.lock")
    with vault.locked():
        record = vault.read()
        if not record:
            if not create:
                raise CredentialError("Database key is unavailable in the OS keychain")
            key = secrets.token_bytes(32)
            vault.write({"database_key": key.hex()})
            return key
        try:
            value = record["database_key"]
            if not isinstance(value, str) or len(value) != 64:
                raise ValueError
            key = bytes.fromhex(value)
            if len(key) != 32:
                raise ValueError
            return key
        except (KeyError, TypeError, ValueError):
            raise CredentialError("Database key entry is invalid") from None

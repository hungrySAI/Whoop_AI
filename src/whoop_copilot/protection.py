"""SQLCipher integration and explicit, local-only source retention policy."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sqlcipher3 import dbapi2 as sqlcipher

DATABASE_ERRORS = (sqlite3.Error, sqlcipher.Error)


@dataclass(frozen=True)
class LocalPolicy:
    retention_days: int
    sources: tuple[str, ...] = ("whoop", "whoop_export")
    owner_authorized: bool = False
    external_transmission: bool = False
    version: int = 1

    def __post_init__(self):
        if type(self.retention_days) is not int or not 1 <= self.retention_days <= 3650:
            raise ValueError("Retention must be between 1 and 3650 days")
        if (
            self.owner_authorized is not True
            or self.external_transmission is not False
            or self.version != 1
        ):
            raise ValueError(
                "Real storage requires explicit owner authorization for local-only use"
            )
        if not self.sources or set(self.sources) - {"whoop", "whoop_export"}:
            raise ValueError("Select supported sources for this local authorization")


def driver_for(environment: str):
    if environment not in {"synthetic", "real"}:
        raise ValueError("Unknown environment")
    return sqlcipher if environment == "real" else sqlite3


def connect(path: Path, environment: str, key: bytes | None = None, *, readonly=False):
    driver = driver_for(environment)
    if environment == "real" and (not isinstance(key, bytes) or len(key) != 32):
        raise ValueError("Real databases require a 32-byte key from the protected key store")
    location = path.resolve().as_uri() + "?mode=ro" if readonly else str(path)
    db = driver.connect(location, uri=readonly, isolation_level=None, timeout=10)
    try:
        if environment == "real":
            # Only fixed-size bytes converted to hex enter this PRAGMA. Never log it.
            db.execute(f'''PRAGMA key = "x'{key.hex()}'"''')
            if not db.execute("PRAGMA cipher_version").fetchone():
                raise ValueError("SQLCipher is unavailable; plaintext fallback is forbidden")
            db.execute("PRAGMA cipher_memory_security=ON")
            db.execute("PRAGMA temp_store=MEMORY")
        db.row_factory = driver.Row
        db.execute("PRAGMA secure_delete=ON")
        return db
    except BaseException:
        db.close()
        raise

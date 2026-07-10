"""SQLite manifest: cache of what's verified on the NAS + audit trail of deletes.

The archive is authoritative; this DB is rebuildable from it. Deletes require a
verified row here, so a lost manifest fail-safes to "no reclaiming".
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

MTIME_TOLERANCE = 2.0  # seconds; FAT-ish filesystems have coarse timestamps

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    sha256 TEXT NOT NULL,
    copied_at TEXT NOT NULL,
    deleted_from_device_at TEXT,
    deleted_tier INTEGER
);
CREATE TABLE IF NOT EXISTS failures (
    path TEXT PRIMARY KEY,
    count INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class ManifestRow:
    path: str
    size: int
    mtime: float
    sha256: str
    copied_at: str
    deleted_from_device_at: str | None
    deleted_tier: int | None


class Manifest:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Manifest":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def record_verified(
        self, path: str, size: int, mtime: float, sha256: str, copied_at: str
    ) -> None:
        self._conn.execute(
            """INSERT INTO files (path, size, mtime, sha256, copied_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                 size = excluded.size, mtime = excluded.mtime,
                 sha256 = excluded.sha256, copied_at = excluded.copied_at,
                 deleted_from_device_at = NULL, deleted_tier = NULL""",
            (path, size, mtime, sha256, copied_at),
        )
        self._conn.commit()

    def get(self, path: str) -> ManifestRow | None:
        cur = self._conn.execute(
            "SELECT path, size, mtime, sha256, copied_at, deleted_from_device_at,"
            " deleted_tier FROM files WHERE path = ?",
            (path,),
        )
        row = cur.fetchone()
        return ManifestRow(*row) if row else None

    def verified_ok(self, path: str, size: int, mtime: float) -> bool:
        """True iff this exact device file (path+size+mtime) has a verified NAS copy."""
        row = self.get(path)
        return (
            row is not None
            and row.size == size
            and abs(row.mtime - mtime) <= MTIME_TOLERANCE
        )

    def mark_deleted(self, path: str, tier: int, deleted_at: str) -> None:
        """Write-ahead audit: MUST be committed before the SMB delete is issued.

        Self-enforcing: refuses to audit a path with no manifest row, so a
        caller that skipped verification cannot produce an unrecorded delete.
        """
        cur = self._conn.execute(
            "UPDATE files SET deleted_from_device_at = ?, deleted_tier = ? WHERE path = ?",
            (deleted_at, tier, path),
        )
        if cur.rowcount == 0:
            raise LookupError(
                f"refusing to audit deletion of {path!r}: no manifest row — "
                "deleting a file without a verified-copy record is forbidden"
            )
        self._conn.commit()

    def bump_failure(self, path: str) -> int:
        self._conn.execute(
            """INSERT INTO failures (path, count) VALUES (?, 1)
               ON CONFLICT(path) DO UPDATE SET count = count + 1""",
            (path,),
        )
        self._conn.commit()
        return self.failure_count(path)

    def clear_failure(self, path: str) -> None:
        self._conn.execute("DELETE FROM failures WHERE path = ?", (path,))
        self._conn.commit()

    def failure_count(self, path: str) -> int:
        cur = self._conn.execute("SELECT count FROM failures WHERE path = ?", (path,))
        row = cur.fetchone()
        return row[0] if row else 0

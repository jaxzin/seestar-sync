"""SQLite manifest: cache of what's verified on the NAS + audit trail of deletes.

The archive is authoritative; this DB is rebuildable from it. Deletes require a
verified row here, so a lost manifest fail-safes to "no reclaiming".
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Self

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
    def __init__(self, db_path: Path, read_only: bool = False):
        if read_only:
            # SHADOW_MODE: another instance owns this file. mode=ro guarantees
            # SQLite never writes it (no journal, no schema, no PRAGMA that
            # persists); a missing DB is an error, never a create.
            if not db_path.is_file():
                raise FileNotFoundError(
                    f"{db_path} not found; SHADOW_MODE never creates the manifest "
                    "(point ARCHIVE_DIR at an archive another instance maintains)"
                )
            target, uri = f"{db_path.resolve().as_uri()}?mode=ro", True
        else:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            target, uri = str(db_path), False
        # the package's ONLY sqlite3.connect (see test_manifest_is_the_only_...)
        self._conn = sqlite3.connect(target, uri=uri)
        if read_only:
            return
        # The archive is a network share. SQLite's default DELETE journal mode
        # UNLINKS the rollback journal at commit, and delete-while-open is exactly
        # the operation an SMB mount performs unreliably (the same defect that
        # litters .seestar-sync/ with .smbdelete* tombstones). A failed unlink
        # strands a hot journal; the next open must roll it back, cannot, and
        # raises "disk I/O error" — which crash-looped the daemon for ~4h on
        # 2026-08-12 until the journal was applied by hand off the share.
        #
        # TRUNCATE zeroes the journal instead of unlinking it, so the unreliable
        # operation is never attempted. Set BEFORE executescript: that first write
        # is what the crash-loop died on.
        #
        # NOT WAL: it requires shared memory, which network filesystems do not
        # provide. Do not "upgrade" this.
        #
        # PER-CONNECTION ONLY: non-WAL journal modes are not persisted in the file,
        # so any OTHER sqlite3.connect() on manifest.db silently reverts to DELETE.
        # All access must go through this class. The PRAGMA returns the mode that
        # took effect, and SQLite may refuse a change silently — so check it.
        mode = self._conn.execute("PRAGMA journal_mode=TRUNCATE").fetchone()[0]
        if mode.lower() != "truncate":
            self._conn.close()
            raise RuntimeError(
                f"manifest.db journal_mode is {mode!r}, not 'truncate'; refusing to "
                "run on a network share where DELETE mode strands hot journals"
            )
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
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

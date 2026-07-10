"""Archive writer: mirror of the device under <root>/MyWorks/.

Safety properties (spec §Error handling):
- Foreign-storage guard: refuses to run when the archive dir shares a device
  id with "/" (that means no volume is mounted and copies would land on the
  container's ephemeral layer). Escape hatch: allow_root_fs.
- Sentinel: init() writes ARCHIVE_MARKER; every later start requires it, so a
  dropped SMB mount (empty dir) can never masquerade as the archive.
- Torn-write immunity: copies land as .part, are re-read and verified
  (size + sha256), then atomically renamed. The archive never holds a torn file.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

_CHUNK = 1024 * 1024


class ArchiveError(Exception):
    """Archive volume unusable. CLI maps this to exit code 2."""


class VerifyError(ArchiveError):
    """Landed bytes don't match the device read; the copy was discarded."""


class Archive:
    MARKER = "ARCHIVE_MARKER"

    def __init__(self, root: Path, dev_of: Callable[[Path], int] | None = None):
        self.root = root
        self._dev_of = dev_of or (lambda p: p.stat().st_dev)

    @property
    def meta_dir(self) -> Path:
        return self.root / ".seestar-sync"

    def _check_foreign_storage(self, allow_root_fs: bool) -> None:
        if allow_root_fs:
            return
        if not self.root.exists():
            raise ArchiveError(f"archive dir {self.root} does not exist — is the volume mounted?")
        if self._dev_of(self.root) == self._dev_of(Path("/")):
            raise ArchiveError(
                f"archive dir {self.root} is on the container's own filesystem "
                "(no volume mounted?). Copies would die with the container. "
                "Mount a volume, or set ARCHIVE_ON_ROOT_FS_OK=true if this "
                "host path is intentionally on the root filesystem."
            )

    def init(self, allow_root_fs: bool) -> None:
        self._check_foreign_storage(allow_root_fs)
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        (self.meta_dir / "history").mkdir(exist_ok=True)
        (self.root / "MyWorks").mkdir(exist_ok=True)
        (self.meta_dir / self.MARKER).touch()

    def check_ready(self, allow_root_fs: bool) -> None:
        self._check_foreign_storage(allow_root_fs)
        if not (self.meta_dir / self.MARKER).exists():
            raise ArchiveError(
                f"{self.meta_dir / self.MARKER} missing — archive not initialized "
                "(run with --init) or the volume mount dropped. Refusing to touch anything."
            )

    def dest_path(self, device_path: str) -> Path:
        """Map a device-relative path into the archive mirror.

        Device filenames are untrusted input (they come off the telescope):
        reject anything that could escape <root>/MyWorks.
        """
        base = self.root / "MyWorks"
        pure = Path(device_path)
        if (
            pure.is_absolute()
            or "\\" in device_path
            or ".." in pure.parts
            or not (base / pure).resolve().is_relative_to(base.resolve())
        ):
            raise ArchiveError(f"unsafe device path rejected: {device_path!r}")
        return base / pure

    def write_stream(
        self, device_path: str, reader: BinaryIO, expected_size: int, mtime: float
    ) -> str:
        """Copy reader -> .part, verify size + sha256 by re-reading, atomic rename.

        Returns the sha256 hex digest. Raises VerifyError (and removes the
        .part) on any mismatch, so a torn or corrupted transfer is invisible
        to the archive.
        """
        dest = self.dest_path(device_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")

        device_hash = hashlib.sha256()
        written = 0
        try:
            with open(part, "wb") as out:
                while chunk := reader.read(_CHUNK):
                    device_hash.update(chunk)
                    out.write(chunk)
                    written += len(chunk)
                out.flush()
                os.fsync(out.fileno())

            if written != expected_size:
                raise VerifyError(
                    f"{device_path}: read {written} bytes, expected {expected_size}"
                )

            landed_hash = hashlib.sha256()
            with open(part, "rb") as check:
                while chunk := check.read(_CHUNK):
                    landed_hash.update(chunk)
            if landed_hash.hexdigest() != device_hash.hexdigest():
                raise VerifyError(f"{device_path}: landed bytes differ from device read")

            os.utime(part, (mtime, mtime))  # preserve device mtime for rebuild-manifest
            os.replace(part, dest)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return device_hash.hexdigest()

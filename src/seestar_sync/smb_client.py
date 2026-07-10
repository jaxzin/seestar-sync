"""Production DeviceClient over SMB (smbprotocol's high-level smbclient API).

Protocol facts (verified via ZWO docs + seestarpy source; see spec §Device facts):
share "EMMC Images", guest auth with empty username/password, direct TCP 445.
The RPC port (4700) is auth-locked on fw >= 7.18 and deliberately unused here.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import BinaryIO

import smbclient
from smbprotocol.exceptions import SMBException

from .device import DeviceUnavailable, FreeSpaceUnavailable
from .models import DeviceFile, DiskUsage

log = logging.getLogger(__name__)

SHARE = "EMMC Images"
ROOT = "MyWorks"


class _MappedReader:
    """File-like wrapper keeping mid-stream SMB failures inside the seam.

    _map_errors protects the open; the reads happen later, inside
    archive.write_stream — without this wrapper a telescope powering off
    mid-transfer leaks a raw ConnectionResetError past DeviceClient and
    kills the daemon.
    """

    def __init__(self, raw, map_errors):
        self._raw = raw
        self._map_errors = map_errors

    def read(self, n: int = -1) -> bytes:
        with self._map_errors():
            return self._raw.read(n)

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:  # noqa: BLE001 — closing a dead handle is not an event
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class SmbDeviceClient:
    def __init__(self, host: str, capacity_gb: float | None = None, timeout: float = 30.0):
        self._host = host
        self._capacity_bytes = (
            int(capacity_gb * 1024**3) if capacity_gb is not None else None
        )
        with self._map_errors():
            smbclient.register_session(
                host,
                username="guest",
                password="",
                connection_timeout=timeout,
                auth_protocol="ntlm",
            )

    def _unc(self, rel: str = "") -> str:
        base = rf"\\{self._host}\{SHARE}\{ROOT}"
        if not rel:
            return base
        # rel is device-controlled: refuse anything that could address outside
        # MyWorks (deletion is the high-blast-radius path through here).
        parts = rel.split("/")
        if rel.startswith("/") or "\\" in rel or ".." in parts or "" in parts:
            raise ValueError(f"unsafe device path rejected: {rel!r}")
        return base + "\\" + rel.replace("/", "\\")

    def _rel(self, unc: str) -> str:
        prefix = self._unc() + "\\"
        return unc.removeprefix(prefix).replace("\\", "/")

    @contextmanager
    def _map_errors(self):
        """Connectivity problems -> DeviceUnavailable; missing files pass through.

        ValueError is in the tuple because smbprotocol's transport wraps
        connect failures in ValueError ("Failed to connect to '<host>:445':
        timed out") — the routine powered-off-telescope case. UNC paths are
        built BEFORE entering this context, so _unc's own fail-loud
        ValueError ("unsafe device path") is never swallowed here.
        """
        try:
            yield
        except FileNotFoundError:
            raise
        except (SMBException, ConnectionError, TimeoutError, OSError, ValueError) as e:
            raise DeviceUnavailable(f"{self._host}: {e}") from e

    def list_files(self) -> list[DeviceFile]:
        out: list[DeviceFile] = []
        root_unc = self._unc()  # UNC built outside _map_errors (see its docstring)
        with self._map_errors():
            self._scan(root_unc, "", out)
        return out

    def _scan(self, unc: str, rel_prefix: str, out: list[DeviceFile]) -> None:
        for entry in smbclient.scandir(unc):
            rel = f"{rel_prefix}{entry.name}"
            if entry.is_dir():
                self._scan(f"{unc}\\{entry.name}", f"{rel}/", out)
            else:
                st = entry.stat()  # cached from the directory listing
                out.append(DeviceFile(rel, st.st_size, st.st_mtime))

    def stat(self, path: str) -> DeviceFile:
        unc = self._unc(path)
        with self._map_errors():
            st = smbclient.stat(unc)
        return DeviceFile(path, st.st_size, st.st_mtime)

    def open_read(self, path: str) -> BinaryIO:
        unc = self._unc(path)
        with self._map_errors():
            raw = smbclient.open_file(unc, mode="rb")
        return _MappedReader(raw, self._map_errors)

    def delete_file(self, path: str) -> None:
        unc = self._unc(path)
        with self._map_errors():
            smbclient.remove(unc)

    def remove_dir_if_empty(self, folder: str) -> bool:
        # Deliberately NOT via _map_errors: a failed empty-dir cleanup is
        # cosmetic, and rmdir-on-non-empty arrives as an OSError we can't
        # distinguish from a connection error after wrapping.
        try:
            smbclient.rmdir(self._unc(folder))
            return True
        except (SMBException, OSError):
            return False  # not empty, already gone, or transient — all cosmetic

    def _stat_volume(self):
        return smbclient.stat_volume(self._unc())

    def disk_usage(self) -> DiskUsage:
        """Primary: SMB2 FsFullSizeInformation via stat_volume (validated by
        the live smoke ring, Task 13 — spec Open item 1). Fallback:
        walk-derived usage against DEVICE_CAPACITY_GB."""
        try:
            v = self._stat_volume()
            return DiskUsage(total_bytes=v.total_size, free_bytes=v.actual_available_size)
        except (SMBException, OSError, NotImplementedError) as e:
            if self._capacity_bytes is None:
                # Distinct from DeviceUnavailable: the device is reachable,
                # reclaim is blinded — must alert, not read as device-off.
                raise FreeSpaceUnavailable(
                    f"stat_volume failed ({e}) and DEVICE_CAPACITY_GB is not set — "
                    "cannot measure free space, refusing to reclaim blind. "
                    "Set DEVICE_CAPACITY_GB (e.g. 128 for the S30 Pro) to enable "
                    "the walk-based fallback."
                ) from e
            log.warning("stat_volume failed (%s); using walk-based fallback", e)
            used = sum(f.size for f in self.list_files())
            return DiskUsage(
                total_bytes=self._capacity_bytes,
                free_bytes=max(0, self._capacity_bytes - used),
            )

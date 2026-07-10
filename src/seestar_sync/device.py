"""DeviceClient: the seam between policy code and the physical Seestar.

Production impl: smb_client.SmbDeviceClient. Test impl: tests/fakes.FakeDevice.
Policy code never learns SMB exists; this is also the migration point if ZWO
ever changes protocols.
"""

from __future__ import annotations

from typing import BinaryIO, Protocol

from .models import DeviceFile, DiskUsage


class DeviceUnavailable(Exception):
    """The Seestar is unreachable or vanished mid-operation (it's a telescope —
    powering off is routine, not an error)."""


class DeviceClient(Protocol):
    def list_files(self) -> list[DeviceFile]:
        """Recursive listing of MyWorks/. Raises DeviceUnavailable."""
        ...

    def stat(self, path: str) -> DeviceFile:
        """Fresh size/mtime. Raises FileNotFoundError or DeviceUnavailable."""
        ...

    def open_read(self, path: str) -> BinaryIO:
        """Streaming read. Raises FileNotFoundError or DeviceUnavailable."""
        ...

    def delete_file(self, path: str) -> None:
        """Raises FileNotFoundError if the path is already gone (app-side
        drift), DeviceUnavailable on connectivity loss."""
        ...

    def remove_dir_if_empty(self, folder: str) -> bool:
        """Best-effort cosmetic cleanup. True when the folder is gone
        (removed or already absent); False when non-empty or removal failed.
        Implementations may differ on edge cases — callers must not rely on
        the return value for correctness."""
        ...

    def disk_usage(self) -> DiskUsage:
        """Raises DeviceUnavailable."""
        ...

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
        """Streaming read. Raises DeviceUnavailable."""
        ...

    def delete_file(self, path: str) -> None:
        """Raises DeviceUnavailable."""
        ...

    def remove_dir_if_empty(self, folder: str) -> bool:
        """Remove a now-empty top-level folder; False if non-empty/missing."""
        ...

    def disk_usage(self) -> DiskUsage:
        """Raises DeviceUnavailable."""
        ...

"""In-memory DeviceClient for the integration test ring."""

from __future__ import annotations

import io

from seestar_sync.device import DeviceUnavailable
from seestar_sync.models import DeviceFile, DiskUsage


class _FailingReader:
    """Reader whose open succeeded but whose stream dies — the mid-transfer
    power-off class the mapped-reader seam must contain."""

    def read(self, n: int = -1) -> bytes:
        raise DeviceUnavailable("simulated mid-read power-off")

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDevice:
    def __init__(self, capacity: int = 1_000_000):
        self.capacity = capacity
        self._files: dict[str, tuple[bytes, float]] = {}
        self.fail_after_reads: int | None = None
        self.short_read_paths: set[str] = set()
        self.fail_mid_read_paths: set[str] = set()  # open succeeds, read() dies
        self.disconnected = False  # flip mid-test to simulate power-off for ALL ops
        self._reads = 0

    def _check_connection(self) -> None:
        if self.disconnected:
            raise DeviceUnavailable("simulated disconnect")

    def put(self, path: str, data: bytes = b"x" * 10, mtime: float = 0.0) -> None:
        self._files[path] = (data, mtime)

    def list_files(self) -> list[DeviceFile]:
        self._check_connection()
        return [
            DeviceFile(path, len(data), mtime)
            for path, (data, mtime) in sorted(self._files.items())
        ]

    def stat(self, path: str) -> DeviceFile:
        self._check_connection()
        if path not in self._files:
            raise FileNotFoundError(path)
        data, mtime = self._files[path]
        return DeviceFile(path, len(data), mtime)

    def open_read(self, path: str):
        self._check_connection()
        if self.fail_after_reads is not None and self._reads >= self.fail_after_reads:
            raise DeviceUnavailable("simulated mid-run power-off")
        if path not in self._files:
            raise FileNotFoundError(path)  # match production contract (app-side drift)
        self._reads += 1
        if path in self.fail_mid_read_paths:
            return _FailingReader()  # open succeeds, read() dies (mid-stream power-off)
        data, _ = self._files[path]
        if path in self.short_read_paths:
            data = data[: max(0, len(data) - 1)]  # torn transfer
        return io.BytesIO(data)

    def delete_file(self, path: str) -> None:
        self._check_connection()
        if path not in self._files:
            raise FileNotFoundError(path)  # match production (smbclient.remove)
        del self._files[path]

    def remove_dir_if_empty(self, folder: str) -> bool:
        prefix = folder + "/"
        return not any(p.startswith(prefix) for p in self._files)

    def disk_usage(self) -> DiskUsage:
        self._check_connection()
        used = sum(len(data) for data, _ in self._files.values())
        return DiskUsage(total_bytes=self.capacity, free_bytes=self.capacity - used)

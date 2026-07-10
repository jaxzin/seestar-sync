import hashlib
import io
import os

import pytest

from seestar_sync.archive import Archive, ArchiveError, VerifyError


def make_archive(tmp_path, dev_of=None):
    root = tmp_path / "archive"
    root.mkdir()
    if dev_of is None:
        # root tree on device 2, "/" on device 1 -> foreign storage, checks pass
        dev_of = lambda p: 2 if str(p).startswith(str(root)) else 1
    return Archive(root, dev_of=dev_of)


def test_init_creates_sentinel_and_meta(tmp_path):
    a = make_archive(tmp_path)
    a.init(allow_root_fs=False)
    assert (a.meta_dir / "ARCHIVE_MARKER").exists()
    a.check_ready(allow_root_fs=False)  # must not raise


def test_check_ready_without_sentinel_fails(tmp_path):
    a = make_archive(tmp_path)
    with pytest.raises(ArchiveError, match="ARCHIVE_MARKER"):
        a.check_ready(allow_root_fs=False)


def test_init_on_container_root_fs_fails(tmp_path):
    # same st_dev as "/" -> ephemeral-filesystem trap -> refuse
    a = make_archive(tmp_path, dev_of=lambda p: 1)
    with pytest.raises(ArchiveError, match="own filesystem"):
        a.init(allow_root_fs=False)
    # escape hatch for legitimately uncontainerized setups
    a.init(allow_root_fs=True)
    a.check_ready(allow_root_fs=True)


def test_write_stream_verifies_and_renames_atomically(tmp_path):
    a = make_archive(tmp_path)
    a.init(allow_root_fs=False)
    data = b"starlight" * 1000
    sha = a.write_stream("M 81_sub/a.fit", io.BytesIO(data), len(data), mtime=1234.0)
    dest = a.dest_path("M 81_sub/a.fit")
    assert dest.read_bytes() == data
    assert sha == hashlib.sha256(data).hexdigest()
    assert abs(os.stat(dest).st_mtime - 1234.0) < 2.0  # device mtime preserved
    assert not list(dest.parent.glob("*.part"))  # no temp debris


def test_short_read_raises_verify_error_and_leaves_no_part(tmp_path):
    a = make_archive(tmp_path)
    a.init(allow_root_fs=False)
    data = b"torn"
    with pytest.raises(VerifyError):
        a.write_stream("M 81_sub/torn.fit", io.BytesIO(data), expected_size=999, mtime=0.0)
    assert not a.dest_path("M 81_sub/torn.fit").exists()
    assert not list((a.root / "MyWorks" / "M 81_sub").glob("*.part"))


def test_write_stream_reports_cumulative_bytes(tmp_path):
    a = make_archive(tmp_path)
    a.init(allow_root_fs=False)
    payload = b"x" * (1024 * 1024 * 3 + 512)  # 3 full chunks + a tail
    seen: list[int] = []
    a.write_stream(
        "M 81_sub/big.fit", io.BytesIO(payload), len(payload), mtime=1000.0,
        on_bytes=seen.append,
    )
    assert seen, "callback was never invoked"
    assert seen == sorted(seen)  # cumulative, monotonic non-decreasing
    assert seen[-1] == len(payload)  # final report is the full size
    assert all(0 < b <= len(payload) for b in seen)
    # one report per 1 MiB chunk (4 chunks: 3 full + tail), not per byte
    assert len(seen) == 4


def test_write_stream_without_callback_still_works(tmp_path):
    a = make_archive(tmp_path)
    a.init(allow_root_fs=False)
    payload = b"abc" * 1000
    sha = a.write_stream(
        "M 81_sub/plain.fit", io.BytesIO(payload), len(payload), mtime=1000.0
    )  # no on_bytes
    assert a.dest_path("M 81_sub/plain.fit").exists()
    assert sha == hashlib.sha256(payload).hexdigest()


def test_dest_path_rejects_traversal(tmp_path):
    # device filenames are device-controlled input: contain them under MyWorks
    a = make_archive(tmp_path)
    a.init(allow_root_fs=False)
    for evil in ("../escape.fit", "a/../../escape.fit", "/etc/passwd", "a\\..\\b.fit"):
        with pytest.raises(ArchiveError, match="unsafe device path"):
            a.dest_path(evil)

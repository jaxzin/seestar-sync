import pytest

from seestar_sync.manifest import Manifest


def make(tmp_path):
    return Manifest(tmp_path / "manifest.db")


def test_journal_mode_never_unlinks(tmp_path):
    """The archive lives on a network share whose delete-while-open is unreliable.

    SQLite's default DELETE journal mode unlinks the rollback journal at commit.
    On the SMB mount this daemon ran against, that unlink silently failed and left
    a hot journal behind; the next open then tried to roll it back, could not, and
    raised `sqlite3.OperationalError: disk I/O error` — crash-looping the daemon
    (2026-08-12, ~4h outage). TRUNCATE zeroes the journal instead of unlinking it,
    so the operation the share cannot perform is never attempted.

    WAL is deliberately NOT used: it needs shared memory, which network
    filesystems do not provide.
    """
    with make(tmp_path) as m:
        mode = m._conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert mode.lower() == "truncate", f"journal_mode is {mode!r}, expected 'truncate'"


def test_commit_leaves_no_journal_to_unlink(tmp_path):
    """After a write, the journal must be zero-length rather than absent.

    This is the observable difference between TRUNCATE and DELETE, and it is the
    property that keeps a failed unlink from being able to strand a hot journal.
    """
    db = tmp_path / "manifest.db"
    with Manifest(db) as m:
        m.record_verified("M 81_sub/a.fit", 100, 1000.0, "abc123", "2026-07-09T00:00:00Z")
    journal = tmp_path / "manifest.db-journal"
    if journal.exists():
        assert journal.stat().st_size == 0, "journal left with content — it is hot"


def test_record_and_verify(tmp_path):
    with make(tmp_path) as m:
        m.record_verified("M 81_sub/a.fit", 100, 1000.0, "abc123", "2026-07-09T00:00:00Z")
        assert m.verified_ok("M 81_sub/a.fit", 100, 1000.0)
        assert m.verified_ok("M 81_sub/a.fit", 100, 1001.5)  # within 2s tolerance
        assert not m.verified_ok("M 81_sub/a.fit", 100, 1005.0)  # mtime drifted
        assert not m.verified_ok("M 81_sub/a.fit", 999, 1000.0)  # size changed
        assert not m.verified_ok("unknown.fit", 1, 0.0)


def test_record_is_upsert(tmp_path):
    with make(tmp_path) as m:
        m.record_verified("a.fit", 100, 1000.0, "old", "2026-07-09T00:00:00Z")
        m.record_verified("a.fit", 200, 2000.0, "new", "2026-07-09T01:00:00Z")
        row = m.get("a.fit")
        assert row.size == 200 and row.sha256 == "new"


def test_mark_deleted_is_audited(tmp_path):
    with make(tmp_path) as m:
        m.record_verified("a.fit", 100, 1000.0, "abc", "2026-07-09T00:00:00Z")
        m.mark_deleted("a.fit", tier=1, deleted_at="2026-07-09T02:00:00Z")
        row = m.get("a.fit")
        assert row.deleted_from_device_at == "2026-07-09T02:00:00Z"
        assert row.deleted_tier == 1
        # deletion audit does not un-verify the file
        assert m.verified_ok("a.fit", 100, 1000.0)


def test_mark_deleted_without_row_refuses(tmp_path):
    # write-ahead audit must be self-enforcing, not caller-dependent
    with make(tmp_path) as m:
        with pytest.raises(LookupError, match="no manifest row"):
            m.mark_deleted("ghost.fit", tier=1, deleted_at="2026-07-09T02:00:00Z")


def test_recopy_clears_delete_audit(tmp_path):
    # load-bearing for false-delete reconciliation: a re-verified file must
    # shed its stale delete audit
    with make(tmp_path) as m:
        m.record_verified("a.fit", 100, 1000.0, "abc", "2026-07-09T00:00:00Z")
        m.mark_deleted("a.fit", tier=1, deleted_at="2026-07-09T01:00:00Z")
        m.record_verified("a.fit", 100, 1000.0, "abc", "2026-07-09T02:00:00Z")
        row = m.get("a.fit")
        assert row.deleted_from_device_at is None
        assert row.deleted_tier is None


def test_failure_counter(tmp_path):
    with make(tmp_path) as m:
        assert m.failure_count("bad.fit") == 0
        assert m.bump_failure("bad.fit") == 1
        assert m.bump_failure("bad.fit") == 2
        m.clear_failure("bad.fit")
        assert m.failure_count("bad.fit") == 0


def test_persistence_across_reopen(tmp_path):
    with make(tmp_path) as m:
        m.record_verified("a.fit", 100, 1000.0, "abc", "2026-07-09T00:00:00Z")
    with make(tmp_path) as m2:
        assert m2.verified_ok("a.fit", 100, 1000.0)

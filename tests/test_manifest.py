from seestar_sync.manifest import Manifest


def make(tmp_path):
    return Manifest(tmp_path / "manifest.db")


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

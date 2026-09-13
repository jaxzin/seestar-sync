import json
from pathlib import Path

from seestar_sync.config import Config
from seestar_sync.models import RunSummary
from seestar_sync.reporters import append_history, report_all, write_status


def summary(result="ok"):
    return RunSummary(started_at="2026-07-09T09:15:03Z", result=result, files_copied=2)


def test_write_status_snapshot(tmp_path):
    write_status(tmp_path, summary())
    data = json.loads((tmp_path / "status.json").read_text())
    # exact spec contract — this JSON is also the MQTT summary payload
    # (published retained to the summary topic)
    assert set(data) == {
        "last_run", "result", "dry_run", "reclaim_enabled", "files_copied",
        "bytes_copied", "files_deleted", "bytes_reclaimed",
        "device_used_percent", "active_folders_skipped", "errors",
    }
    assert data["result"] == "ok"
    assert data["files_copied"] == 2
    assert data["last_run"] == "2026-07-09T09:15:03Z"


def test_append_history_monthly_jsonl(tmp_path):
    append_history(tmp_path, summary())
    append_history(tmp_path, summary(result="partial"))
    lines = (tmp_path / "history" / "2026-07.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["result"] == "partial"


def test_kuma_pings_only_on_ok_results(monkeypatch):
    # the dead-man-switch contract: failed runs must NOT ping, so Kuma
    # alerts on the resulting silence
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    calls: list[str] = []

    def fake_urlopen(url, timeout=None):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr("seestar_sync.reporters.urllib.request.urlopen", fake_urlopen)
    from seestar_sync.reporters import ping_kuma

    url = "http://kuma.local/api/push/token123"
    ping_kuma(url, summary(result="ok"))
    ping_kuma(url, summary(result="skipped_unreachable"))
    ping_kuma(url, summary(result="partial"))
    ping_kuma(url, summary(result="error"))
    assert len(calls) == 2
    assert all("status=up" in c for c in calls)


def test_guard_redacts_urls_from_warnings(tmp_path, monkeypatch, caplog):
    import logging

    url = "http://kuma.local/api/push/SECRETTOKEN123"

    def explode(u, timeout=None):
        raise ValueError(f"unknown url type: {u!r}")

    monkeypatch.setattr("seestar_sync.reporters.urllib.request.urlopen", explode)
    cfg = Config.from_env({"SEESTAR_HOST": "h", "KUMA_PUSH_URL": url})
    with caplog.at_level(logging.WARNING):
        report_all(cfg, tmp_path, summary())
    assert "SECRETTOKEN123" not in caplog.text
    assert "<redacted-url>" in caplog.text


def test_reporters_never_raise(tmp_path, monkeypatch):
    # unwritable meta dir + unreachable endpoints: run must not blow up
    cfg = Config.from_env(
        {
            "SEESTAR_HOST": "h",
            "KUMA_PUSH_URL": "http://127.0.0.1:1/push/x",  # nothing listens
        }
    )
    report_all(cfg, Path("/nonexistent/definitely/not/writable"), summary())
    report_all(cfg, tmp_path, summary())  # endpoints down, files still written
    assert (tmp_path / "status.json").exists()


def test_ha_webhook_reporter_is_gone():
    from seestar_sync import reporters

    assert not hasattr(reporters, "post_ha_webhook")

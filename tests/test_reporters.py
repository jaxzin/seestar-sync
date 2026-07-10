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
    assert data["result"] == "ok"
    assert data["last_run"] == "2026-07-09T09:15:03Z"


def test_append_history_monthly_jsonl(tmp_path):
    append_history(tmp_path, summary())
    append_history(tmp_path, summary(result="partial"))
    lines = (tmp_path / "history" / "2026-07.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["result"] == "partial"


def test_reporters_never_raise(tmp_path, monkeypatch):
    # unwritable meta dir + unreachable endpoints: run must not blow up
    cfg = Config.from_env(
        {
            "SEESTAR_HOST": "h",
            "KUMA_PUSH_URL": "http://127.0.0.1:1/push/x",  # nothing listens
            "HA_WEBHOOK_URL": "http://127.0.0.1:1/api/webhook/x",
        }
    )
    report_all(cfg, Path("/nonexistent/definitely/not/writable"), summary())
    report_all(cfg, tmp_path, summary())  # endpoints down, files still written
    assert (tmp_path / "status.json").exists()

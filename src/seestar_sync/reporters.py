"""Observability sinks. Strictly fire-and-forget: a reporter failure is a log
warning, never a run failure (spec: only the archive volume is load-bearing)."""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from pathlib import Path

from .config import Config
from .models import RunSummary

log = logging.getLogger(__name__)

_HTTP_TIMEOUT = 5.0
# Kuma alerts on silence; partial/error runs deliberately don't ping.
_KUMA_OK_RESULTS = {"ok", "skipped_unreachable"}


def _guard(name: str, fn, *args) -> None:
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001 — reporters must never raise
        # Push/webhook URLs carry secret tokens and some exceptions echo
        # their input (e.g. urllib's "unknown url type: '<url>'") — redact
        # any string argument from the logged text.
        msg = str(e)
        for arg in args:
            if isinstance(arg, str) and arg and arg in msg:
                msg = msg.replace(arg, "<redacted-url>")
        log.warning("reporter %s failed (non-fatal): %s", name, msg)


def write_status(meta_dir: Path, summary: RunSummary) -> None:
    meta_dir.mkdir(parents=True, exist_ok=True)
    tmp = meta_dir / "status.json.tmp"
    tmp.write_text(json.dumps(summary.to_json(), indent=2))
    tmp.replace(meta_dir / "status.json")


def append_history(meta_dir: Path, summary: RunSummary) -> None:
    history = meta_dir / "history"
    history.mkdir(parents=True, exist_ok=True)
    month = summary.started_at[:7]  # "2026-07" from ISO timestamp
    with open(history / f"{month}.jsonl", "a") as f:
        f.write(json.dumps(summary.to_json()) + "\n")


def ping_kuma(url: str, summary: RunSummary) -> None:
    if summary.result not in _KUMA_OK_RESULTS:
        return
    msg = urllib.parse.quote(f"{summary.result}: {summary.files_copied} copied")
    sep = "&" if "?" in url else "?"
    with urllib.request.urlopen(
        f"{url}{sep}status=up&msg={msg}", timeout=_HTTP_TIMEOUT
    ):
        pass


def report_all(config: Config, meta_dir: Path, summary: RunSummary) -> None:
    _guard("status", write_status, meta_dir, summary)
    _guard("history", append_history, meta_dir, summary)
    if config.kuma_push_url:
        _guard("kuma", ping_kuma, config.kuma_push_url, summary)

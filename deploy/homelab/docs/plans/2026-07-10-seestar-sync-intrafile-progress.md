# Intra-File Copy Progress Implementation Plan (v0.2.1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make copy progress advance *during* a file, not just between files, so multi-GB Seestar videos (up to ~29 GB) no longer freeze the HA Progress sensor and the docker logs for tens of minutes.

**Architecture:** `Archive.write_stream` gains an optional `on_bytes(cumulative_written)` callback, invoked as chunks land. The sync orchestrator (`_copy_phase`) passes a callback that emits the existing `ProgressEvent` with `bytes_done = completed_file_bytes + partial`, coalesced at a byte threshold so a 29 GB file produces hundreds of events, not tens of thousands. A monotonic-clock heartbeat logs an operator-visible progress line during long copies. No MQTT/HA discovery change — the Progress `percent` is already `bytes_done / bytes_total`.

**Tech Stack:** Python 3.12, pytest. No new dependencies.

## Global Constraints

- No magic numbers — coalescing threshold and heartbeat interval are named module constants with a comment.
- Reporters/observers never fail the run — the `on_bytes` callback path must not let an observer exception abort a copy (existing `run_sync` observer contract: consumers must not raise; we preserve it).
- No silent exception catching — the callback is a thin pass-through; it adds no new `except`.
- Heartbeat interval **15 s** (just above the MqttReporter's 10 s throttle so the two don't oscillate).
- Coalescing threshold **64 MiB** (`_PROGRESS_COALESCE_BYTES`).
- Version bump `0.1.0` → `0.2.1` in `pyproject.toml` (also corrects the stale `diag/version` MQTT sensor, which reads `importlib.metadata.version("seestar-sync")`).
- Preserve existing behavior: per-file completion still records the manifest row, increments `files_done`, and logs `copied <path> (<bytes>)`.

---

### Task 21: Intra-file progress callback, heartbeat logging, version bump

**Files:**
- Modify: `src/seestar_sync/archive.py` — add `on_bytes` param to `write_stream`, call it in the write loop
- Modify: `src/seestar_sync/syncrun.py` — wire per-file callback in `_copy_phase`, coalesce, heartbeat log
- Modify: `pyproject.toml:3` — version `0.1.0` → `0.2.1`
- Test: `tests/test_archive.py` — callback invocation + cumulative bytes + no-callback default
- Test: `tests/test_syncrun.py` — intra-file ProgressEvent sequence, coalescing, monotonic bytes_done, heartbeat cadence

**Interfaces:**
- Consumes: `ProgressEvent(phase, files_done, files_total, bytes_done, bytes_total, current_path)` (unchanged, from `models.py`); `Archive.write_stream(device_path, reader, expected_size, mtime)` (existing).
- Produces:
  - `Archive.write_stream(device_path, reader, expected_size, mtime, on_bytes: Callable[[int], None] | None = None) -> str` — `on_bytes` receives cumulative bytes written to the `.part` file so far, called after each chunk write. Default `None` preserves current behavior. Called only during the *write* pass, not the verify re-read.
  - `_copy_phase` unchanged signature; internally emits `ProgressEvent(phase="copying", ...)` mid-file with `bytes_done = base + partial`.

---

- [ ] **Step 1: Write the failing test for the archive callback**

In `tests/test_archive.py`:

```python
def test_write_stream_reports_cumulative_bytes(tmp_path):
    arc = Archive(tmp_path)
    arc.init(allow_root_fs=True)
    payload = b"x" * (1024 * 1024 * 3 + 512)  # 3 chunks + tail
    seen: list[int] = []
    arc.write_stream("Target_sub/Light_001.fit", io.BytesIO(payload),
                     len(payload), mtime=1000.0, on_bytes=seen.append)
    # cumulative, monotonic non-decreasing, final equals full size
    assert seen == sorted(seen)
    assert seen[-1] == len(payload)
    assert all(0 < b <= len(payload) for b in seen)


def test_write_stream_without_callback_still_works(tmp_path):
    arc = Archive(tmp_path)
    arc.init(allow_root_fs=True)
    payload = b"abc" * 1000
    sha = arc.write_stream("Target_sub/Light_002.fit", io.BytesIO(payload),
                           len(payload), mtime=1000.0)  # no on_bytes
    assert (tmp_path / "MyWorks" / "Target_sub" / "Light_002.fit").exists()
    assert len(sha) == 64
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_archive.py::test_write_stream_reports_cumulative_bytes -v`
Expected: FAIL — `write_stream() got an unexpected keyword argument 'on_bytes'`

- [ ] **Step 3: Add the callback to `write_stream`**

In `src/seestar_sync/archive.py`, change the signature and the write loop:

```python
    def write_stream(
        self, device_path: str, reader: BinaryIO, expected_size: int, mtime: float,
        on_bytes: Callable[[int], None] | None = None,
    ) -> str:
```

In the write loop (only the first loop, not the verify re-read), after `written += len(chunk)`:

```python
                written += len(chunk)
                if on_bytes is not None:
                    on_bytes(written)  # cumulative bytes into .part; consumer coalesces/throttles
```

`Callable` is already imported (used by `dev_of`). Confirm `from typing import ... Callable` / `from collections.abc import Callable` is present; if not, add it.

- [ ] **Step 4: Run to verify both archive tests pass**

Run: `uv run pytest tests/test_archive.py -v`
Expected: PASS (all, including the two new ones)

- [ ] **Step 5: Commit**

```bash
git add src/seestar_sync/archive.py tests/test_archive.py
git commit -m "feat(archive): report cumulative bytes during write_stream" -- src/seestar_sync/archive.py tests/test_archive.py
```

- [ ] **Step 6: Write the failing test for intra-file progress events + coalescing**

In `tests/test_syncrun.py` (reuse the module's existing FakeDevice / Archive / manifest fixtures; a large single file forces multiple coalesced emits). The recorder collects every `ProgressEvent`:

```python
def test_intrafile_progress_emits_growing_bytes_done(tmp_path, make_device):
    # one 200 MiB file -> with a 64 MiB coalesce threshold, >=3 mid-file emits
    size = 200 * 1024 * 1024
    device = make_device(files=[("Solar_video/big.avi", size)])
    archive = Archive(tmp_path / "arc"); archive.init(allow_root_fs=True)
    manifest = Manifest(tmp_path / "m.db")
    events: list[ProgressEvent] = []
    run_sync(device, archive, manifest, copy_only_config(seestar_host="h"),
             now=_fixed_now(), should_stop=lambda: False, observer=events.append)

    copying = [e for e in events if e.phase == "copying" and e.current_path == "Solar_video/big.avi"]
    mid = [e for e in copying if e.bytes_done < size]  # strictly-partial emits
    assert len(mid) >= 3, f"expected multiple intra-file emits, got {len(mid)}"
    # bytes_done is monotonic non-decreasing across the whole copying phase
    seq = [e.bytes_done for e in copying]
    assert seq == sorted(seq)
    # files_done stays 0 until the file completes, final emit has full bytes
    assert copying[-1].bytes_done == size
    assert copying[-1].files_done == 1
```

(Adapt fixture/helper names — `make_device`, `copy_only_config`, `_fixed_now` — to whatever `tests/test_syncrun.py` already defines; do not invent new fixtures if equivalents exist.)

- [ ] **Step 7: Run to verify it fails**

Run: `uv run pytest tests/test_syncrun.py::test_intrafile_progress_emits_growing_bytes_done -v`
Expected: FAIL — only one `copying` event per file (the current end-of-file emit), so `len(mid) >= 3` fails.

- [ ] **Step 8: Wire the callback + coalescing in `_copy_phase`**

In `src/seestar_sync/syncrun.py`, add module constants near the top (with the other constants):

```python
# Emit at most one intra-file progress event per this many bytes written, so a
# ~29 GB file produces hundreds of events, not tens of thousands. The MQTT
# consumer still throttles by time; this bounds event volume at the source.
_PROGRESS_COALESCE_BYTES = 64 * 1024 * 1024
# Operator-visible heartbeat cadence for in-flight copies (seconds). Sits just
# above the MqttReporter 10 s throttle so the two do not oscillate.
_COPY_HEARTBEAT_SECONDS = 15.0
```

Replace the real-copy branch (the `try: with device.open_read(...) as reader: sha = archive.write_stream(...)`) so it passes a coalescing callback. Use `time.monotonic` for the heartbeat (import `time` if not already):

```python
        base = bytes_done
        last_emit = base
        hb = {"t": time.monotonic()}
        log.info("copying %s (%d bytes)", f.path, f.size)

        def _tick(written: int) -> None:
            nonlocal last_emit
            now_bytes = base + written
            crossed = now_bytes - last_emit >= _PROGRESS_COALESCE_BYTES
            done = written >= f.size
            if crossed or done:
                last_emit = now_bytes
                notify(ProgressEvent(
                    phase="copying", files_done=files_done, files_total=files_total,
                    bytes_done=now_bytes, bytes_total=bytes_total, current_path=f.path))
            t = time.monotonic()
            if t - hb["t"] >= _COPY_HEARTBEAT_SECONDS and not done:
                hb["t"] = t
                log.info("copying %s: %.1f/%.1f GB (%d%%)", f.path,
                         written / 1e9, f.size / 1e9,
                         int(100 * written / f.size) if f.size else 100)

        try:
            with device.open_read(f.path) as reader:
                sha = archive.write_stream(f.path, reader, f.size, f.mtime, on_bytes=_tick)
        except FileNotFoundError:
            log.debug("%s vanished before copy (app-side removal); normal drift", f.path)
            continue
        except DeviceUnavailable as e:
            log.info("device vanished mid-copy (%s), keeping progress", e)
            return True
        except VerifyError as e:
            count = manifest.bump_failure(f.path)
            msg = f"verify failed ({count}/{MAX_VERIFY_FAILURES}): {e}"
            log.warning(msg)
            summary.errors.append(msg)
            continue
        manifest.record_verified(f.path, f.size, f.mtime, sha, iso(now))
        manifest.clear_failure(f.path)
        summary.files_copied += 1
        summary.bytes_copied += f.size
        files_done += 1
        bytes_done += f.size
        notify(ProgressEvent(phase="copying", files_done=files_done,
                             files_total=files_total, bytes_done=bytes_done,
                             bytes_total=bytes_total, current_path=f.path))
        log.info("copied %s (%d bytes)", f.path, f.size)
```

Notes:
- The final `notify(...)` after `files_done += 1` guarantees a `bytes_done == pass_total_so_far`, `files_done` incremented event fires exactly once per completed file (preserves the existing files-copied semantics and the `done` accounting downstream).
- The pre-copy `log.info("copying %s (%d bytes)")` start line replaces silence during long files; the existing post-copy `copied …` line stays.
- `_tick` closes over `files_done` (read at call time — fine, it only changes after the file completes) and `bytes_done` via `base` (snapshotted before the copy). No mutation of loop accumulators inside `_tick`.

- [ ] **Step 9: Run the syncrun test to verify it passes**

Run: `uv run pytest tests/test_syncrun.py::test_intrafile_progress_emits_growing_bytes_done -v`
Expected: PASS

- [ ] **Step 10: Write the heartbeat cadence test (inject a fake monotonic clock)**

If `_tick`'s heartbeat uses `time.monotonic` directly, it is hard to assert deterministically. Prefer testing the coalescing/emit behavior (Step 6) as the load-bearing guarantee, and cover the heartbeat with a `caplog` smoke test that a start line is emitted:

```python
def test_copy_logs_start_line_for_each_file(tmp_path, make_device, caplog):
    device = make_device(files=[("Solar_video/big.avi", 5 * 1024 * 1024)])
    archive = Archive(tmp_path / "arc"); archive.init(allow_root_fs=True)
    manifest = Manifest(tmp_path / "m.db")
    with caplog.at_level("INFO"):
        run_sync(device, archive, manifest, copy_only_config(seestar_host="h"),
                 now=_fixed_now(), should_stop=lambda: False, observer=lambda e: None)
    assert any("copying Solar_video/big.avi (" in r.message for r in caplog.records)
    assert any("copied Solar_video/big.avi (" in r.message for r in caplog.records)
```

- [ ] **Step 11: Run to verify it passes**

Run: `uv run pytest tests/test_syncrun.py::test_copy_logs_start_line_for_each_file -v`
Expected: PASS

- [ ] **Step 12: Bump the version**

In `pyproject.toml`, line 3: `version = "0.1.0"` → `version = "0.2.1"`.

- [ ] **Step 13: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS (all prior tests + the new ones; 5 gated live tests still skipped).

- [ ] **Step 14: Commit**

```bash
git add src/seestar_sync/syncrun.py tests/test_syncrun.py pyproject.toml
git commit -m "feat(sync): intra-file progress events + heartbeat logs; bump to 0.2.1" -- src/seestar_sync/syncrun.py tests/test_syncrun.py pyproject.toml
```

## Self-Review

- **Spec coverage:** intra-file byte reporting (Steps 1–5), coalesced mid-file ProgressEvents driving the bytes-based HA Progress % (Steps 6–9), operator heartbeat/start logs (Steps 8, 10–11), version bump fixing `diag/version` (Step 12). All covered.
- **Placeholder scan:** fixture names in Step 6/10 are flagged to adapt to existing `tests/test_syncrun.py` helpers rather than invent — the implementer must reconcile, not paste blindly.
- **Type consistency:** `on_bytes: Callable[[int], None] | None`, `ProgressEvent` fields match `models.py`; `write_stream` return type unchanged (`str`).
- **Contract preservation:** per-file completion still emits exactly one `files_done`-incremented event; dry-run branch untouched (planned-count events only); verify re-read loop does NOT call `on_bytes` (only real bytes-to-disk are reported).

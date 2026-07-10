import json

import pytest

from seestar_sync.config import Config
from seestar_sync.settings import OVERRIDE_KEYS, SettingsStore, resolve_effective


def cfg(**over):
    env = {"SEESTAR_HOST": "seestar.local"}
    env.update(over)
    return Config.from_env(env)


def test_missing_file_means_no_overrides(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    assert store.load() == {}


def test_set_and_load_round_trip(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.set("reclaim_enabled", True, "2026-07-10T00:00:00Z")
    store.set("seestar_host", "scope2.local", "2026-07-10T00:01:00Z")
    assert store.load() == {"reclaim_enabled": True, "seestar_host": "scope2.local"}
    raw = json.loads((tmp_path / "settings.json").read_text())
    assert raw["reclaim_enabled"]["modified_at"] == "2026-07-10T00:00:00Z"


def test_set_rejects_unknown_keys_and_wrong_types(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    with pytest.raises(ValueError, match="unknown settings key"):
        store.set("sync_interval_minutes", 5, "t")
    with pytest.raises(ValueError, match="dry_run"):
        store.set("dry_run", "yes", "t")  # must be bool, not str
    with pytest.raises(ValueError, match="seestar_host"):
        store.set("seestar_host", "", "t")  # nonempty string required
    with pytest.raises(ValueError, match="seestar_host"):
        store.set("seestar_host", "bad host", "t")  # no whitespace/control chars


def test_corrupt_file_falls_back_to_empty(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{not json")
    assert SettingsStore(p).load() == {}
    p.write_text(json.dumps({"reclaim_enabled": {"value": "banana", "modified_at": "t"}}))
    assert SettingsStore(p).load() == {}  # wrong type inside is dropped too
    p.write_text(json.dumps({"seestar_host": {"value": "evil host", "modified_at": "t"}}))
    assert SettingsStore(p).load() == {}  # whitespace host dropped at LOAD too


def test_resolve_effective_precedence_and_sources(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    base = cfg()  # env: dry_run False, reclaim_enabled False, host seestar.local
    eff, sources = resolve_effective(base, store)
    assert eff == base
    assert sources == {k: "env" for k in OVERRIDE_KEYS}

    store.set("dry_run", True, "t")
    store.set("seestar_host", "scope2.local", "t")
    eff, sources = resolve_effective(base, store)
    assert eff.dry_run is True and eff.seestar_host == "scope2.local"
    assert eff.reclaim_enabled is False  # untouched key keeps env value
    assert sources["dry_run"] == "mqtt override"
    assert sources["reclaim_enabled"] == "env"
    # the original config is untouched (frozen + replace)
    assert base.dry_run is False

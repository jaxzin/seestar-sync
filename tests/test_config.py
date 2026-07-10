import pytest

from seestar_sync.config import Config, ConfigError


def base_env(**overrides):
    env = {"SEESTAR_HOST": "seestar.local"}
    env.update(overrides)
    return env


def test_minimal_env_uses_defaults():
    cfg = Config.from_env(base_env())
    assert cfg.seestar_host == "seestar.local"
    assert str(cfg.archive_dir) == "/archive"
    assert cfg.sync_interval_minutes == 15
    assert cfg.reclaim_trigger_percent == 75.0
    assert cfg.reclaim_target_percent == 60.0
    assert cfg.active_window_minutes == 15
    assert cfg.device_capacity_gb is None
    assert cfg.kuma_push_url is None
    assert cfg.ha_webhook_url is None
    assert cfg.dry_run is False
    assert cfg.archive_on_root_fs_ok is False
    assert cfg.log_level == "INFO"


def test_missing_host_is_fatal():
    with pytest.raises(ConfigError, match="SEESTAR_HOST"):
        Config.from_env({})


def test_target_must_be_below_trigger():
    env = base_env(RECLAIM_TRIGGER_PERCENT="60", RECLAIM_TARGET_PERCENT="75")
    with pytest.raises(ConfigError, match="RECLAIM_TARGET_PERCENT"):
        Config.from_env(env)


def test_bool_parsing_accepts_common_truthy_values():
    for raw in ("1", "true", "TRUE", "yes", "on"):
        assert Config.from_env(base_env(DRY_RUN=raw)).dry_run is True
    assert Config.from_env(base_env(DRY_RUN="false")).dry_run is False


def test_non_numeric_interval_is_fatal():
    with pytest.raises(ConfigError, match="SYNC_INTERVAL_MINUTES"):
        Config.from_env(base_env(SYNC_INTERVAL_MINUTES="soon"))

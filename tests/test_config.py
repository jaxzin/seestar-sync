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
    assert cfg.mqtt_host is None
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


def test_non_numeric_capacity_is_fatal():
    with pytest.raises(ConfigError, match="DEVICE_CAPACITY_GB"):
        Config.from_env(base_env(DEVICE_CAPACITY_GB="lots"))


def test_non_positive_capacity_and_window_are_fatal():
    with pytest.raises(ConfigError, match="DEVICE_CAPACITY_GB"):
        Config.from_env(base_env(DEVICE_CAPACITY_GB="0"))
    # the session guard is a delete-safety feature; it must never be disabled
    with pytest.raises(ConfigError, match="ACTIVE_WINDOW_MINUTES"):
        Config.from_env(base_env(ACTIVE_WINDOW_MINUTES="0"))


def test_kuma_url_must_be_http():
    with pytest.raises(ConfigError, match="KUMA_PUSH_URL"):
        Config.from_env(base_env(KUMA_PUSH_URL="file:///etc/passwd"))
    cfg = Config.from_env(base_env(KUMA_PUSH_URL="https://kuma.local/api/push/x"))
    assert cfg.kuma_push_url == "https://kuma.local/api/push/x"


def test_reclaim_is_opt_in():
    # deletion must be explicitly enabled — a default config can never reclaim
    assert Config.from_env(base_env()).reclaim_enabled is False
    assert Config.from_env(base_env(RECLAIM_ENABLED="true")).reclaim_enabled is True


def test_bad_log_level_is_fatal():
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        Config.from_env(base_env(LOG_LEVEL="banana"))
    assert Config.from_env(base_env(LOG_LEVEL="debug")).log_level == "DEBUG"


def test_garbage_bool_is_fatal():
    for raw in ("0", "no", "off"):
        assert Config.from_env(base_env(DRY_RUN=raw)).dry_run is False
    with pytest.raises(ConfigError, match="DRY_RUN"):
        Config.from_env(base_env(DRY_RUN="maybe"))


def test_non_positive_interval_is_fatal():
    for raw in ("0", "-5"):
        with pytest.raises(ConfigError, match="SYNC_INTERVAL_MINUTES"):
            Config.from_env(base_env(SYNC_INTERVAL_MINUTES=raw))


def test_mqtt_defaults_and_parse():
    cfg = Config.from_env(base_env())
    assert cfg.mqtt_host is None and cfg.mqtt_port == 1883
    assert cfg.mqtt_username is None and cfg.mqtt_password is None
    assert cfg.mqtt_topic_prefix == "seestar-sync"
    cfg = Config.from_env(base_env(
        MQTT_HOST="homeassistant.local", MQTT_PORT="1884",
        MQTT_USERNAME="seestar", MQTT_PASSWORD="hunter2",
        MQTT_TOPIC_PREFIX="scope2",
        KUMA_PUSH_URL="https://kuma.local/api/push/KUMATOKEN123",
    ))
    assert cfg.mqtt_host == "homeassistant.local" and cfg.mqtt_port == 1884
    assert cfg.mqtt_username == "seestar" and cfg.mqtt_password == "hunter2"
    assert cfg.mqtt_topic_prefix == "scope2"
    # secrets must never leak via tracebacks/logs that repr() a Config
    assert "hunter2" not in repr(cfg)
    assert "KUMATOKEN123" not in repr(cfg)


def test_mqtt_validation_is_fatal():
    with pytest.raises(ConfigError, match="MQTT_PORT"):
        Config.from_env(base_env(MQTT_HOST="h", MQTT_PORT="70000"))
    with pytest.raises(ConfigError, match="MQTT_HOST"):
        Config.from_env(base_env(MQTT_USERNAME="u"))  # auth without host
    with pytest.raises(ConfigError, match="MQTT_USERNAME"):
        Config.from_env(base_env(MQTT_HOST="h", MQTT_PASSWORD="p"))
    with pytest.raises(ConfigError, match="credentials are required"):
        Config.from_env(base_env(MQTT_HOST="h"))  # host alone: no anonymous brokers
    with pytest.raises(ConfigError, match="MQTT_TOPIC_PREFIX"):
        # credentials present so the check under test is the one that fires
        Config.from_env(base_env(
            MQTT_HOST="h", MQTT_USERNAME="u", MQTT_PASSWORD="p",
            MQTT_TOPIC_PREFIX="a/b",
        ))


def test_ha_webhook_url_is_gone():
    assert not hasattr(Config.from_env(base_env()), "ha_webhook_url")

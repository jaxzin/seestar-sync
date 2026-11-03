"""The add-on's option-to-environment mapping.

These guard what a Home Assistant user can get wrong or be hurt by: the shipped
defaults must be a harmless rehearsal, every option in config.yaml must reach the
daemon, and what we hand the daemon must be accepted by its own config parser.
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest
import yaml

from seestar_sync import addon
from seestar_sync.config import Config

REPO = Path(__file__).resolve().parents[1]
MANIFEST = yaml.safe_load((REPO / "addon" / "config.yaml").read_text())


def _defaults(**override: object) -> dict[str, object]:
    options = dict(MANIFEST["options"])
    options["seestar_host"] = "192.0.2.50"
    options.update(override)
    return options


def _no_broker():
    return None


SUPERVISOR_BROKER = {
    "host": "core-mosquitto",
    "port": 1883,
    "ssl": False,
    "username": "addon",
    "password": "s3cret",
}


def test_shipped_defaults_are_a_harmless_rehearsal():
    env = addon.build_env(_defaults(), lambda: SUPERVISOR_BROKER)
    config = Config.from_env(env)
    # dry run ON and reclaim OFF: nothing is copied and nothing is ever deleted
    # until the user deliberately changes both.
    assert config.dry_run is True
    assert config.reclaim_enabled is False
    assert config.shadow_mode is False
    assert config.archive_on_root_fs_ok is False


def test_every_manifest_option_is_forwarded():
    # A new option in config.yaml that the mapping forgets would silently do nothing.
    assert set(MANIFEST["options"]) == set(MANIFEST["schema"])
    assert set(MANIFEST["options"]) == addon.MAPPED_OPTIONS


def test_mqtt_identity_defaults_match_the_existing_device():
    # Renaming the topic prefix orphans the HA device and its history.
    assert MANIFEST["options"]["mqtt_topic_prefix"] == "seestar-sync"
    env = addon.build_env(_defaults(), _no_broker)
    assert Config.from_env(env).mqtt_topic_prefix == "seestar-sync"


def test_options_reach_the_daemon_with_typed_values():
    env = addon.build_env(
        _defaults(
            dry_run=False,
            reclaim_enabled=True,
            reclaim_trigger_percent=80,
            reclaim_target_percent=55.5,
            sync_interval_minutes=30,
            archive_dir="/share/astro",
            mqtt_topic_prefix="seestar-sync-test",
            log_level="debug",
        ),
        _no_broker,
    )
    config = Config.from_env(env)
    assert config.dry_run is False
    assert config.reclaim_enabled is True
    assert config.reclaim_trigger_percent == 80
    assert config.reclaim_target_percent == 55.5
    assert config.sync_interval_minutes == 30
    assert config.archive_dir == Path("/share/astro")
    assert config.mqtt_topic_prefix == "seestar-sync-test"
    assert config.log_level == "DEBUG"


def test_blank_optionals_stay_unset_so_daemon_defaults_apply():
    env = addon.build_env(_defaults(kuma_push_url="", device_capacity_gb=None), _no_broker)
    assert "KUMA_PUSH_URL" not in env
    assert "DEVICE_CAPACITY_GB" not in env
    config = Config.from_env(env)
    assert config.kuma_push_url is None
    assert config.device_capacity_gb is None


def test_supervisor_mqtt_service_is_used_when_host_is_blank():
    env = addon.build_env(_defaults(), lambda: SUPERVISOR_BROKER)
    config = Config.from_env(env)
    assert (config.mqtt_host, config.mqtt_port) == ("core-mosquitto", 1883)
    assert (config.mqtt_username, config.mqtt_password) == ("addon", "s3cret")


def test_pinned_mqtt_host_wins_and_never_asks_the_supervisor():
    def must_not_be_called():
        raise AssertionError("Supervisor consulted although mqtt_host is set")

    env = addon.build_env(
        _defaults(mqtt_host="broker.lan", mqtt_port=1884, mqtt_username="u", mqtt_password="p"),
        must_not_be_called,
    )
    config = Config.from_env(env)
    assert (config.mqtt_host, config.mqtt_port) == ("broker.lan", 1884)
    assert (config.mqtt_username, config.mqtt_password) == ("u", "p")


def test_no_broker_runs_without_mqtt_instead_of_refusing_to_sync():
    env = addon.build_env(_defaults(), _no_broker)
    assert not any(key.startswith("MQTT_HOST") for key in env)
    assert Config.from_env(env).mqtt_host is None


def test_tls_only_supervisor_broker_is_refused_with_guidance():
    tls_broker = {**SUPERVISOR_BROKER, "ssl": True, "port": 8883}
    with pytest.raises(addon.AddonError, match="TLS"):
        addon.build_env(_defaults(), lambda: tls_broker)


@pytest.mark.parametrize("host", ["", "   ", None])
def test_missing_telescope_host_is_a_clear_error(host):
    with pytest.raises(addon.AddonError, match="seestar_host"):
        addon.build_env(_defaults(seestar_host=host), _no_broker)


@pytest.mark.parametrize("path", ["", "media/seestar", "relative"])
def test_archive_dir_must_be_absolute(path):
    with pytest.raises(addon.AddonError, match="archive_dir"):
        addon.build_env(_defaults(archive_dir=path), _no_broker)


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *_):
        return json.dumps(self._payload).encode()


def test_fetch_mqtt_service_reads_the_supervisor_envelope():
    seen = {}

    def urlopen(request, timeout):
        seen["auth"] = request.get_header("Authorization")
        return _Response({"result": "ok", "data": SUPERVISOR_BROKER})

    assert addon.fetch_mqtt_service("tok", urlopen) == SUPERVISOR_BROKER
    assert seen["auth"] == "Bearer tok"


@pytest.mark.parametrize("code", [400, 404, 503])
def test_fetch_mqtt_service_treats_no_service_as_absent(code):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, code, "no service", {}, None)

    assert addon.fetch_mqtt_service("tok", urlopen) is None


def test_fetch_mqtt_service_surfaces_other_failures():
    def urlopen(request, timeout):
        raise urllib.error.URLError("supervisor unreachable")

    with pytest.raises(addon.AddonError, match="unreachable"):
        addon.fetch_mqtt_service("tok", urlopen)


def test_main_execs_the_daemon_with_init_and_the_mapped_environment(tmp_path):
    options = tmp_path / "options.json"
    options.write_text(json.dumps(_defaults(mqtt_host="broker.lan", mqtt_username="u")))
    calls = []
    code = addon.main(
        options_path=options,
        environ={"PATH": "/usr/bin", "SUPERVISOR_TOKEN": "tok"},
        exec_fn=lambda prog, argv, env: calls.append((prog, argv, env)),
    )
    assert code == 0
    (prog, argv, env), = calls
    assert (prog, argv) == ("seestar-sync", ["seestar-sync", "daemon", "--init"])
    assert env["SEESTAR_HOST"] == "192.0.2.50"
    assert env["DRY_RUN"] == "true" and env["RECLAIM_ENABLED"] == "false"
    assert env["PATH"] == "/usr/bin"  # the container environment is preserved


def test_main_exits_2_without_starting_anything_on_bad_options(tmp_path):
    options = tmp_path / "options.json"
    options.write_text(json.dumps(_defaults(seestar_host="")))
    calls = []
    code = addon.main(
        options_path=options,
        environ={},
        exec_fn=lambda *a: calls.append(a),
    )
    assert code == 2
    assert calls == []


def test_main_exits_2_when_options_file_is_missing(tmp_path):
    code = addon.main(options_path=tmp_path / "absent.json", environ={}, exec_fn=lambda *a: None)
    assert code == 2

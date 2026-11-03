# Changelog

## 0.3.0

- First release as a Home Assistant add-on.
- Options map to the daemon's environment; MQTT uses the Supervisor's broker
  when `mqtt_host` is blank.
- Starts as a rehearsal: `dry_run` on, `reclaim_enabled` off.
- Home Assistant device and entity identities are unchanged from earlier
  versions (topic prefix `seestar-sync`).

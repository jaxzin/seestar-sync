# Configured entirely from the environment; the consuming repo owns the
# credential:
#   UNIFI_API       controller URL, e.g. https://192.168.10.1
#   UNIFI_USERNAME  the shared `tofu` account (OpenBao secret/infra/unifi/tofu)
#   UNIFI_PASSWORD  "
#   UNIFI_INSECURE  "true" — the UCG serves a self-signed cert on its LAN address
provider "unifi" {}

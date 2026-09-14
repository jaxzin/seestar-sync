# The Seestar S30's network identity. seestar-sync mounts the telescope's SMB
# share (//192.168.10.50/EMMC Images) from k3s, so the address is load-bearing:
# an unreserved lease that moves silently breaks every sync.
#
# Module pinned to a reviewed commit SHA, never a floating tag: v1 has moved
# twice, and the 2026-08-01 Gitea rollback once erased a pinned SHA entirely.
# a411406 carries `ignore_changes = [dev_id_override]`; same pin as
# nuc-setup / homelab-netboot / allsky-setup / tower2-setup.
module "reservation" {
  source = "git::https://gitea.forest-draconis.ts.net/fallen-leaf/terraform-unifi-reservation.git?ref=a41140626030fc4289bec43bd0de490278797d71" # secrets-scan-ok: pinned module commit SHA, not a credential

  hostname = var.hostname
  mac      = var.mac
  ip       = var.ip
  # network_name omitted -> module default "Default" (192.168.10.0/24, the flat
  # LAN the NAS and k3s nodes share). NOT IoT (192.168.20.0/24): seestar-sync
  # reaches the telescope over SMB from the server LAN.
}

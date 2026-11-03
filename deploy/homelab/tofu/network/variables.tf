variable "hostname" {
  description = "UniFi client name for the telescope."
  type        = string
  default     = "seestar"
}

variable "mac" {
  description = "The Seestar S30's Wi-Fi station MAC. The ONLY client this configuration may touch."
  type        = string
  default     = "40:fd:f3:9e:3e:64"

  validation {
    # Pinned so a typo fails at plan time instead of re-pointing someone
    # else's device. Source: read-only controller lookup 2026-09-13 (see
    # README.md) — OUI "AMPAK Technology" (the S30's Wi-Fi module), DHCP
    # hostname "SeeStar", and the LAN ARP entry for 192.168.10.50 agrees.
    condition     = var.mac == "40:fd:f3:9e:3e:64"
    error_message = "This repo may only manage 40:fd:f3:9e:3e:64 (the Seestar S30)."
  }
}

variable "ip" {
  description = "Fixed IP — the telescope's existing (hand-made) reservation, now codified."
  type        = string
  default     = "192.168.10.50"
}

variable "client_id" {
  description = <<-EOT
    UniFi client `_id` for var.mac, used by the import block in import.tf to
    adopt the existing record instead of creating a second one. Read from the
    controller 2026-09-13 via GET /proxy/network/api/s/default/rest/user.
  EOT
  type        = string
  default     = "6a4029b1691cf0d3484b1c9b"

  validation {
    condition     = can(regex("^[0-9a-f]{24}$", var.client_id))
    error_message = "client_id must be a 24-character lowercase hex UniFi object id."
  }
}

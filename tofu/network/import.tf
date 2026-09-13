# ADOPT THE EXISTING CLIENT DECLARATIVELY — no hand-run `tofu import`
# ("no manual IaC, CI only", Brian 2026-08-23). Same shape as tower2-setup.
#
# 40:fd:f3:9e:3e:64 already has a controller client record carrying a
# hand-made reservation (use_fixedip=true, fixed_ip=192.168.10.50). The
# provider's allow_existing would take it over on create anyway, but a
# destroy FORGETS the client (skip_forget_on_destroy=false, and the module has
# no override), so state must describe the object that actually exists.
#
# Import blocks are inert once the address is tracked: first apply adopts,
# every later run is a no-op. Id format `<site>:<_id>`.
import {
  to = module.reservation.unifi_user.this
  id = "default:${var.client_id}"
}

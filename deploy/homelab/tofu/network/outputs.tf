output "reservation" {
  description = "The converged fixed-IP reservation."
  value = {
    id  = module.reservation.id
    mac = module.reservation.mac
    ip  = module.reservation.ip
  }
}

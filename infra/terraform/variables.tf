variable "suffix" {
  description = "Unique suffix for resource names (use your Net ID)"
  type        = string
}

variable "key" {
  description = "Name of the SSH key pair registered in OpenStack"
  type        = string
  default     = "id_rsa_chameleon"
}

variable "reservation_id" {
  description = "Chameleon reservation UUID from your active lease — passed as a scheduler hint to target the reserved GPU node"
  type        = string
}

variable "flavor_name" {
  description = "OpenStack flavor for the GPU node. CHI@TACC bare-metal has only 'baremetal'."
  type        = string
  default     = "baremetal"
}

variable "app_reservation_id" {
  description = "KVM@TACC Blazar reservation ID for the app node. Used as the flavor_id when launching the instance."
  type        = string
}

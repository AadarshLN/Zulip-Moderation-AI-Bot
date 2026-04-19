# Credentials are read from ~/.config/openstack/clouds.yaml
# Never put credentials directly in this file.
#
# clouds.yaml must have two entries:
#   chi_tacc — CHI@TACC bare-metal (GPU node)
#              auth_url: https://chi.tacc.chameleoncloud.org:5000/v3
#   kvm_tacc — KVM@TACC virtual machines (app node, no reservation needed)
#              auth_url: https://kvm.tacc.chameleoncloud.org:5000/v3

# Default provider — CHI@TACC (bare-metal GPU node)
provider "openstack" {
  cloud = "chi_tacc"
}

# KVM@TACC provider — general-purpose app node (on-demand, no reservation)
provider "openstack" {
  alias = "kvm"
  cloud = "kvm_tacc"
}

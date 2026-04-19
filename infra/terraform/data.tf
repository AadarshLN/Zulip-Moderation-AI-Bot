# Pre-existing resources on Chameleon — looked up, not created.

# ── CHI@TACC (GPU bare-metal node) ───────────────────────────────────────────

data "openstack_networking_network_v2" "sharednet1" {
  name = "sharednet1"
}

data "openstack_networking_secgroup_v2" "allow_ssh" {
  name = "allow-ssh"
}

data "openstack_networking_secgroup_v2" "allow_http_80" {
  name = "default"
}

# ── KVM@TACC (app node) ───────────────────────────────────────────────────────

data "openstack_networking_network_v2" "kvm_sharednet1" {
  provider = openstack.kvm
  name     = "sharednet1"
}

data "openstack_networking_secgroup_v2" "kvm_allow_ssh" {
  provider = openstack.kvm
  name     = "allow-ssh"
}

data "openstack_networking_secgroup_v2" "kvm_allow_http" {
  provider = openstack.kvm
  name     = "default"
}

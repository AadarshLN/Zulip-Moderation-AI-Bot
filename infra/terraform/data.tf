# Pre-existing resources on Chameleon — looked up, not created.

data "openstack_networking_network_v2" "sharednet1" {
  name = "sharednet1"
}

# Pre-existing security groups in the Chameleon project
data "openstack_networking_secgroup_v2" "allow_ssh" {
  name = "allow-ssh"
}

data "openstack_networking_secgroup_v2" "allow_http_80" {
  name = "default"
}

# All web traffic (Zulip + MLflow) routes through nginx-ingress on port 80
# using nip.io subdomains, so no additional port rules are needed.

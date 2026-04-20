# ── GPU node (CHI@TACC bare-metal) ───────────────────────────────────────────
# Runs training jobs only. k3s agent (worker). No public services hosted here.

# Rules added to CHI@TACC 'default' secgroup (new secgroups not possible — quota exhausted):
#   8472 UDP  — Flannel VXLAN pod traffic from app-node to gpu-node
#   10250 TCP — kubelet API so kubectl logs/exec on gpu-node pods works from app-node
#               (app-node connects to gpu floating IP which NATs to 10.52.x.x:10250)
# Note: 6443 is NOT needed here — gpu-node connects outbound to app-node:6443
resource "openstack_networking_secgroup_rule_v2" "chi_flannel_vxlan" {
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "udp"
  port_range_min    = 8472
  port_range_max    = 8472
  remote_ip_prefix  = "0.0.0.0/0"
  security_group_id = data.openstack_networking_secgroup_v2.allow_http_80.id
}

resource "openstack_networking_secgroup_rule_v2" "chi_kubelet" {
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 10250
  port_range_max    = 10250
  remote_ip_prefix  = "0.0.0.0/0"
  security_group_id = data.openstack_networking_secgroup_v2.allow_http_80.id
}

resource "openstack_networking_port_v2" "gpu_port" {
  name       = "gpu-port-${var.suffix}"
  network_id = data.openstack_networking_network_v2.sharednet1.id
  security_group_ids = [
    data.openstack_networking_secgroup_v2.allow_ssh.id,
    data.openstack_networking_secgroup_v2.allow_http_80.id,
  ]
}

resource "openstack_networking_floatingip_v2" "gpu_floating_ip" {
  pool        = "public"
  description = "GPU node floating IP for ${var.suffix}"
  port_id     = openstack_networking_port_v2.gpu_port.id
}

resource "openstack_compute_instance_v2" "gpu_node" {
  name        = "gpu-node-${var.suffix}"
  image_name  = "CC-Ubuntu24.04-ROCm"
  flavor_name = "baremetal"
  key_pair    = var.key

  network {
    port = openstack_networking_port_v2.gpu_port.id
  }

  scheduler_hints {
    additional_properties = {
      reservation = var.reservation_id
    }
  }

  user_data = <<-EOF
    #! /bin/bash
    sudo echo "127.0.1.1 gpu-node-${var.suffix}" >> /etc/hosts
    su cc -c /usr/local/bin/cc-load-public-keys
  EOF
}

# ── Persistent data volume (KVM@TACC Cinder) ─────────────────────────────────
# Survives terraform destroy/recreate of the app node.
# Mounted at /opt/local-path-provisioner — all k3s PVCs (postgres, zulip,
# mlflow, rabbitmq) live here and persist across redeployments.
resource "openstack_blockstorage_volume_v3" "app_data" {
  provider    = openstack.kvm
  name        = "app-data-${var.suffix}"
  size        = 100
  description = "Persistent k8s PVC storage for ${var.suffix}"

  lifecycle {
    prevent_destroy = true
  }
}

resource "openstack_compute_volume_attach_v2" "app_data_attach" {
  provider    = openstack.kvm
  instance_id = openstack_compute_instance_v2.app_node.id
  volume_id   = openstack_blockstorage_volume_v3.app_data.id
}

# ── App node (KVM@TACC, general-purpose) ─────────────────────────────────────
# Runs all services: Zulip, PostgreSQL, MLflow, etc.
# k3s server (control plane + worker). Hosts the public floating IP.

# Allow k3s API server (6443) and Flannel VXLAN (8472) from GPU node
resource "openstack_networking_secgroup_v2" "kvm_k3s" {
  provider    = openstack.kvm
  name        = "k3s-api-${var.suffix}"
  description = "Allow k3s API server (6443 TCP) and Flannel VXLAN (8472 UDP) for cross-site cluster"
}

resource "openstack_networking_secgroup_rule_v2" "kvm_k3s_api" {
  provider          = openstack.kvm
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 6443
  port_range_max    = 6443
  remote_ip_prefix  = "0.0.0.0/0"
  security_group_id = openstack_networking_secgroup_v2.kvm_k3s.id
}

resource "openstack_networking_secgroup_rule_v2" "kvm_flannel_vxlan" {
  provider          = openstack.kvm
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "udp"
  port_range_min    = 8472
  port_range_max    = 8472
  remote_ip_prefix  = "0.0.0.0/0"
  security_group_id = openstack_networking_secgroup_v2.kvm_k3s.id
}

resource "openstack_networking_port_v2" "app_port" {
  provider   = openstack.kvm
  name       = "app-port-${var.suffix}"
  network_id = data.openstack_networking_network_v2.kvm_sharednet1.id
  security_group_ids = [
    data.openstack_networking_secgroup_v2.kvm_allow_ssh.id,
    data.openstack_networking_secgroup_v2.kvm_allow_http.id,
    data.openstack_networking_secgroup_v2.kvm_allow_https.id,
    openstack_networking_secgroup_v2.kvm_k3s.id,
  ]
}

resource "openstack_networking_floatingip_v2" "app_floating_ip" {
  provider    = openstack.kvm
  pool        = "public"
  description = "App node public access for ${var.suffix}"
  port_id     = openstack_networking_port_v2.app_port.id
}

resource "openstack_compute_instance_v2" "app_node" {
  provider   = openstack.kvm
  name       = "app-node-${var.suffix}"
  image_name = "CC-Ubuntu24.04"
  # Use the Blazar reservation flavor_id directly — this is how KVM@TACC
  # reservations work (no scheduler_hints needed, unlike CHI@TACC bare-metal)
  flavor_id  = var.app_reservation_id
  key_pair   = var.key

  network {
    port = openstack_networking_port_v2.app_port.id
  }

  user_data = <<-EOF
    #! /bin/bash
    sudo echo "127.0.1.1 app-node-${var.suffix}" >> /etc/hosts
    su cc -c /usr/local/bin/cc-load-public-keys
  EOF
}

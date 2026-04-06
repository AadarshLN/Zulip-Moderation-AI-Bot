# ─── Networking ───────────────────────────────────────────────────────────────

resource "openstack_networking_port_v2" "node_port" {
  name       = "zulip-port-${var.suffix}"
  network_id = data.openstack_networking_network_v2.sharednet1.id
  security_group_ids = [
    data.openstack_networking_secgroup_v2.allow_ssh.id,
    data.openstack_networking_secgroup_v2.allow_http_80.id,
  ]
}

resource "openstack_networking_floatingip_v2" "floating_ip" {
  pool        = "public"
  description = "Zulip Moderation Bot IP for ${var.suffix}"
  port_id     = openstack_networking_port_v2.node_port.id
}

# ─── Compute ──────────────────────────────────────────────────────────────────

resource "openstack_compute_instance_v2" "zulip_node" {
  name        = "zulip-node-${var.suffix}"
  image_name  = "CC-Ubuntu24.04-CUDA"
  flavor_name = "baremetal"
  key_pair    = var.key

  network {
    port = openstack_networking_port_v2.node_port.id
  }

  scheduler_hints {
    additional_properties = {
      reservation = var.reservation_id
    }
  }

  user_data = <<-EOF
    #! /bin/bash
    sudo echo "127.0.1.1 zulip-node-${var.suffix}" >> /etc/hosts
    su cc -c /usr/local/bin/cc-load-public-keys
  EOF
}

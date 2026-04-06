output "floating_ip_out" {
  description = "Public floating IP of the VM — use this for SSH and service access"
  value       = openstack_networking_floatingip_v2.floating_ip.address
}

output "instance_id" {
  description = "OpenStack instance ID"
  value       = openstack_compute_instance_v2.zulip_node.id
}

output "app_node_floating_ip" {
  description = "Public IP of the app node (KVM) — use for service access and nginx-ingress"
  value       = openstack_networking_floatingip_v2.app_floating_ip.address
}

output "gpu_node_floating_ip" {
  description = "Public floating IP of the GPU node — used for k3s agent join and SSH"
  value       = openstack_networking_floatingip_v2.gpu_floating_ip.address
}

output "app_node_id" {
  description = "OpenStack instance ID of the app node"
  value       = openstack_compute_instance_v2.app_node.id
}

output "gpu_node_id" {
  description = "OpenStack instance ID of the GPU node"
  value       = openstack_compute_instance_v2.gpu_node.id
}

# Zulip Moderation AI Bot — Infrastructure Guide

Complete instructions for provisioning and deploying the platform on Chameleon Cloud (CHI@TACC bare-metal).

---

## Prerequisites

### Local tools

```bash
# Terraform
brew install terraform

# Ansible + required collections
pip install ansible
ansible-galaxy collection install community.general ansible.posix

# OpenStack CLI (for creating object store containers)
pip install python-openstackclient
```

### Chameleon credentials

**SSH key** — confirm your Chameleon key exists:
```bash
ls ~/.ssh/id_rsa_chameleon
```
If named differently, update the `key` variable in Terraform.

**OpenStack credentials** — download `clouds.yaml` from the Chameleon dashboard:
> Identity → Application Credentials → Download clouds.yaml

Place it at `~/.config/openstack/clouds.yaml`.

**EC2 credentials** — needed for object store access (MLflow artifacts + Zulip uploads):
> Identity → EC2 Credentials → Create EC2 Credential

Note the Access Key and Secret Key — you'll enter them in the vault in Phase 2.

---

## Phase 1 — Terraform (provision the VM)

Run from `infra/terraform/` on your local machine.

### 1.1 Create your tfvars file

```bash
cd infra/terraform
cat > terraform.tfvars <<EOF
suffix         = "proj09"
reservation_id = "YOUR_RESERVATION_UUID"
EOF
```

Find your reservation UUID on the Chameleon dashboard under Reservations.

### 1.2 Initialize and apply

```bash
terraform init
terraform plan    # review what will be created
terraform apply   # will prompt for confirmation
```

### 1.3 Export node IPs

```bash
export APP_NODE_IP=$(terraform output -raw app_node_floating_ip)
export GPU_NODE_IP=$(terraform output -raw gpu_node_floating_ip)
echo "App node: $APP_NODE_IP"
echo "GPU node: $GPU_NODE_IP"
```

Keep both values — Ansible needs them.

---

## Phase 2 — Secrets (one-time setup)

Run from `infra/ansible/` on your local machine.

### 2.1 Create and populate the vault

```bash
cd infra/ansible

# Copy the template
cp group_vars/all/vault.yml.example group_vars/all/vault.yml

# Edit — fill in ALL values
nano group_vars/all/vault.yml
```

Key values to fill in:

| Variable | How to get it |
|---|---|
| `vault_zulip_secret_key` | `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `vault_zulip_admin_email` | Your email address |
| `vault_zulip_admin_password` | Choose a strong password |
| `vault_chameleon_ec2_access` | EC2 Access Key from Chameleon dashboard |
| `vault_chameleon_ec2_secret` | EC2 Secret Key from Chameleon dashboard |
| `vault_postgres_password` | Choose a strong password |
| `vault_rabbitmq_password` | Choose a strong password |
| `vault_redis_password` | Choose a strong password |
| `vault_mlflow_db_password` | Choose a strong password |

### 2.2 Encrypt the vault

```bash
ansible-vault encrypt group_vars/all/vault.yml
# You will be prompted to set a vault password — remember it

# Store the vault password so you don't have to type it each run
echo "your_vault_password" > ~/.vault_pass
chmod 600 ~/.vault_pass
```

---

## Phase 3 — Ansible (configure and deploy)

All commands run from `infra/ansible/` on your local machine.

### 3.1 Pre-K8s setup

Disables the firewall, installs Docker, creates the local storage directory on both nodes. Installs NVIDIA Container Toolkit on the GPU node only.

```bash
ansible-playbook -i inventory.yml pre_k8s.yml \
  --vault-password-file ~/.vault_pass
```

**Verify:**
```bash
ssh -i ~/.ssh/id_rsa_chameleon cc@$APP_NODE_IP "docker --version"
```

### 3.2 Install k3s

Three plays in sequence:
1. Install k3s server on app-node (control plane)
2. Configure NVIDIA containerd runtime and join GPU node as agent
3. Apply node labels/taints, deploy NVIDIA device plugin, deploy nginx-ingress pinned to app-node

```bash
ansible-playbook -i inventory.yml install_k3s.yml \
  --vault-password-file ~/.vault_pass
```

**Verify:**
```bash
ssh -i ~/.ssh/id_rsa_chameleon cc@$APP_NODE_IP \
  "kubectl get nodes -o wide"
```

Expected: two nodes in `Ready` state — `app-node-proj09` (control-plane) and `gpu-node-proj09`.

### 3.3 Deploy all services

Creates Swift containers (run locally), copies manifests to VM, creates K8s secrets, deploys all services in dependency order, and prints access URLs.

```bash
ansible-playbook -i inventory.yml post_k8s.yml \
  --vault-password-file ~/.vault_pass
```

At the end it prints:
```
Zulip:  http://zulip.<IP>.nip.io
MLflow: http://mlflow.<IP>.nip.io
```

And a realm creation link for first-time setup.

---

## Phase 4 — Verify the deployment

```bash
ssh -i ~/.ssh/id_rsa_chameleon cc@$APP_NODE_IP

# Check all pods
kubectl get pods -n zulip -o wide
kubectl get pods -n platform -o wide

# All should be Running; check PVCs are Bound
kubectl get pvc -A
```

Open in your browser:
- **Zulip**: `http://zulip.<FLOATING_IP>.nip.io`
- **MLflow**: `http://mlflow.<FLOATING_IP>.nip.io`

Use the realm creation link printed by Ansible to register the first admin account.

---

## Phase 5 — Run the training job

### 5.1 Build and push the trainer image

From the repo root:

```bash
docker build -f train/Dockerfile -t <YOUR_DOCKERHUB_USER>/zulip-moderation-trainer:latest .
docker push <YOUR_DOCKERHUB_USER>/zulip-moderation-trainer:latest
```

Update the image field in [infra/k8s/platform/training-job.yaml](infra/k8s/platform/training-job.yaml) if you use a different image name.

### 5.2 Load training data onto the node

Copy your training CSV to the node so it lands in the PVC mount path:

```bash
scp -i ~/.ssh/id_rsa_chameleon your_data.csv \
  cc@$APP_NODE_IP:/opt/local-path-provisioner/training-data/
```

### 5.3 Submit the training job

```bash
ssh -i ~/.ssh/id_rsa_chameleon cc@$APP_NODE_IP

kubectl apply -f ~/k8s/platform/training-job.yaml
```

### 5.4 Monitor the job

```bash
# Watch pod status
kubectl get pods -n platform -w

# Stream logs
kubectl logs -n platform -l job-name=zulip-moderation-training -f
```

### 5.5 Re-run the job

Kubernetes Jobs are immutable after completion. To re-run:

```bash
kubectl delete job zulip-moderation-training -n platform
kubectl apply -f ~/k8s/platform/training-job.yaml
```

---

## Redeployment (when reservation expires)

When you get a new reservation and floating IP:

```bash
cd infra/terraform

# Update terraform.tfvars with the new reservation_id
nano terraform.tfvars

terraform apply

# TODO (Nitish): floating_ip_out does not exist — outputs.tf defines app_node_floating_ip and gpu_node_floating_ip. Update this command.
export ANSIBLE_HOST=$(terraform output -raw floating_ip_out)

cd ../ansible
ansible-playbook -i inventory.yml pre_k8s.yml --vault-password-file ~/.vault_pass
ansible-playbook -i inventory.yml install_k3s.yml --vault-password-file ~/.vault_pass
ansible-playbook -i inventory.yml post_k8s.yml --vault-password-file ~/.vault_pass
```

`post_k8s.yml` is idempotent — it detects an existing Zulip realm and skips realm creation on redeployment.

---

## Troubleshooting

**Zulip takes a long time to start** — normal on first boot (3–5 min). Watch with:
```bash
kubectl logs -n zulip deploy/zulip -f
```

**502 Bad Gateway** — nginx-ingress may not be ready yet, or Zulip is still initialising. Wait and retry.

**GPU not available in training job** — check the device plugin:
```bash
kubectl get pods -n kube-system | grep nvidia
kubectl describe node | grep nvidia
```

**Re-check all pod status:**
```bash
kubectl get pods -A
```

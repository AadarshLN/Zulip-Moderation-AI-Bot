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

### Docker (for building service images)

Docker Desktop must be installed and running. All images must be built for `linux/amd64` since the cluster runs on AMD64 hardware (Mac is ARM64).

Build and push all three service images before running Ansible:

```bash
# ChatSentry API
cd services/chatsentry
docker buildx build --platform linux/amd64 -t kichanitish/chatsentry-api:latest --push .

# Inference service
cd services/inference
docker buildx build --platform linux/amd64 -t kichanitish/inference:latest --push .

# Zulip moderation bot
cd services/zulip-bot
docker buildx build --platform linux/amd64 -t kichanitish/zulip-moderation-bot:latest --push .
```

If you change code in any of these services, rebuild and push before redeploying.

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
| `vault_chatsentry_db_password` | Choose a strong password |
| `vault_zulip_bot_email` | Fill in after Phase 4.1 (Zulip bot creation) |
| `vault_zulip_bot_api_key` | Fill in after Phase 4.1 (Zulip bot creation) |
| `vault_google_oauth2_key` | Google OAuth Client ID — see Phase 4.2 |
| `vault_google_oauth2_secret` | Google OAuth Client Secret — see Phase 4.2 |

**Note:** `vault_zulip_bot_email` and `vault_zulip_bot_api_key` are obtained from the Zulip UI after the realm is created. Leave them as placeholders for now and fill them in before running `post_k8s.yml`.

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

Disables the firewall, installs Docker, creates the local storage directory on both nodes.

```bash
ansible-playbook -i inventory.yml pre_k8s.yml \
  --vault-password-file ~/.vault_pass
```

### 3.2 Install k3s

Three plays in sequence:
1. Install k3s server on app-node (control plane)
2. Configure AMD ROCm containerd runtime and join GPU node as agent
3. Apply node labels/taints, deploy AMD GPU device plugin, deploy nginx-ingress pinned to app-node

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

---

## Phase 4 — Zulip bot setup (one-time, before post_k8s)

The Zulip bot credentials must exist before deploying services. This is a two-step process: deploy Zulip first, create the bot, then deploy everything else.

### 4.1 Deploy Zulip only (first-time)

```bash
ansible-playbook -i inventory.yml post_k8s.yml \
  --vault-password-file ~/.vault_pass
```

Wait for Ansible to print the realm creation link, then open it in your browser to create the admin account and realm.

### 4.2 Create the moderation bot in Zulip

1. Log into Zulip as admin
2. Go to **Personal Settings → Bots → Add a new bot**
3. Bot type: **Generic bot**
4. Name: `chatsentry-bot` (or any name)
5. Click **Create bot**
6. Copy the **bot email** and **API key** shown

### 4.3 Add bot credentials to vault and redeploy

```bash
cd infra/ansible

# Decrypt vault
ansible-vault decrypt group_vars/all/vault.yml --vault-password-file ~/.vault_pass

# Edit — fill in the bot credentials
nano group_vars/all/vault.yml
# Set vault_zulip_bot_email and vault_zulip_bot_api_key

# Re-encrypt
ansible-vault encrypt group_vars/all/vault.yml --vault-password-file ~/.vault_pass
```

### 4.4 Create the moderation stream in Zulip

In the Zulip UI: click **+** next to CHANNELS → **Create a channel** → name it exactly `moderation`.

This is where the bot posts flagged messages for human review.

### 4.5 Grant bot administrator role

1. Go to **Settings → Organization → Users**
2. Find the bot user → click the pencil icon
3. Change role to **Administrator**
4. Save

This allows the bot to delete other users' messages.

---

## Phase 5 — Deploy all services

```bash
ansible-playbook -i inventory.yml post_k8s.yml \
  --vault-password-file ~/.vault_pass
```

This playbook:
- Creates Chameleon Swift object store containers (runs locally)
- Copies all k8s manifests to the VM
- Substitutes the floating IP placeholder in all manifests
- Creates all k8s secrets from vault values
- Deploys all services in dependency order
- Generates a self-signed TLS cert for the Zulip HTTPS ingress
- Prints access URLs and realm creation link (first deploy only)

**Access URLs** (printed at end of playbook):

| Service | URL | Notes |
|---|---|---|
| Zulip | `https://zulip.<IP>.nip.io` | Self-signed cert — click Advanced → Proceed |
| MLflow | `http://mlflow.<IP>.nip.io` | |
| RabbitMQ | `http://rabbitmq.<IP>.nip.io` | |
| Adminer | `http://adminer.<IP>.nip.io` | Postgres UI |
| ChatSentry | `http://chatsentry.<IP>.nip.io` | |

**Adminer login:**
- Server: `postgres.zulip.svc.cluster.local`
- Username: `zulip`
- Password: `vault_postgres_password`
- Database: `chatsentry` (or `zulip`)

---

## Phase 6 — Verify the deployment

```bash
ssh -i ~/.ssh/id_rsa_chameleon cc@$APP_NODE_IP

# Check all pods
kubectl get pods -n zulip -o wide
kubectl get pods -n platform -o wide

# All should be Running; check PVCs are Bound
kubectl get pvc -A
```

Verify the moderation pipeline end-to-end:
```bash
# Watch bot logs
kubectl logs -n platform deploy/zulip-bot --follow
```

Send a message in Zulip and confirm it appears in the bot logs as `Processing message`.

Verify messages are being stored in ChatSentry:
```bash
kubectl exec -n zulip deploy/postgres -- psql -U zulip -d chatsentry \
  -c "SELECT u.email, m.text, m.created_at FROM messages m JOIN users u ON m.user_id = u.id ORDER BY m.created_at DESC LIMIT 5;"
```

---

## Phase 7 — Run the training job

### 7.1 Build and push the trainer image

From the repo root:

```bash
docker build -f train/Dockerfile -t kichanitish/zulip-moderation-trainer:latest .
docker push kichanitish/zulip-moderation-trainer:latest
```

Update the image field in [infra/k8s/platform/training-job.yaml](infra/k8s/platform/training-job.yaml) if you use a different image name.

### 7.2 Load training data onto the node

Copy your training CSV to the node so it lands in the PVC mount path:

```bash
scp -i ~/.ssh/id_rsa_chameleon your_data.csv \
  cc@$APP_NODE_IP:/opt/local-path-provisioner/training-data/
```

### 7.3 Submit the training job

```bash
ssh -i ~/.ssh/id_rsa_chameleon cc@$APP_NODE_IP
kubectl apply -f ~/k8s/platform/training-job.yaml
```

### 7.4 Monitor the job

```bash
kubectl get pods -n platform -w
kubectl logs -n platform -l job-name=zulip-moderation-training -f
```

### 7.5 Re-run the job

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

export APP_NODE_IP=$(terraform output -raw app_node_floating_ip)

cd ../ansible
ansible-playbook -i inventory.yml pre_k8s.yml --vault-password-file ~/.vault_pass
ansible-playbook -i inventory.yml install_k3s.yml --vault-password-file ~/.vault_pass
ansible-playbook -i inventory.yml post_k8s.yml --vault-password-file ~/.vault_pass
```

`post_k8s.yml` is idempotent — it detects an existing Zulip realm and skips realm creation on redeployment.

The Cinder volume persists across redeployments (`prevent_destroy = true` in Terraform) — all PostgreSQL data, MLflow artifacts, and PVC data survive.

**Note:** After redeployment the bot credentials stay the same (stored in vault), but the `#moderation` stream and bot moderator role must already exist in the persisted Zulip data — no manual steps needed on redeployment.

---

## Troubleshooting

**Zulip takes a long time to start** — normal on first boot (3–5 min). Watch with:
```bash
kubectl logs -n zulip deploy/zulip -f
```

**502 Bad Gateway** — nginx-ingress may not be ready yet, or Zulip is still initialising. Wait and retry.

**Inference pod slow to start** — hateBERT downloads and loads on first start (~90s). The zulip-bot init container waits for it automatically.

**GPU not available in training job** — check the AMD device plugin:
```bash
kubectl get pods -n kube-system | grep amdgpu
kubectl describe node gpu-node-proj09 | grep amd
```

**Re-check all pod status:**
```bash
kubectl get pods -A
```

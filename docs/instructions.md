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

Build and push all service images before running Ansible:

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

# GE Viewer (Great Expectations report viewer)
cd services/ge-viewer
docker buildx build --platform linux/amd64 -t kichanitish/ge-viewer:latest --push .

# GPU Service (runs on AMD ROCm GPU node)
cd ../..
docker buildx build --platform linux/amd64 -f Dockerfile.gpu-service \
  -t kichanitish/gpu-service:latest --push .

# Trainer (ROCm/AMD — use Dockerfile.training at repo root, NOT train/Dockerfile)
docker buildx build --platform linux/amd64 -f Dockerfile.training \
  -t kichanitish/zulip-moderation-trainer:latest --push .
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

**EC2 credentials** — needed for object store access (MLflow artifacts, training data, model checkpoints):
> Identity → EC2 Credentials → Create EC2 Credential

Note the Access Key and Secret Key — you'll enter them in the vault in Phase 2.

---

## Phase 1 — Terraform (provision the VMs)

Run from `infra/terraform/` on your local machine.

### 1.1 Create your tfvars file

```bash
cd infra/terraform
cat > terraform.tfvars <<EOF
suffix             = "proj09"
reservation_id     = "YOUR_GPU_RESERVATION_UUID"
app_reservation_id = "YOUR_APP_RESERVATION_UUID"
EOF
```

Find both reservation UUIDs on the Chameleon dashboard under Reservations:
- `reservation_id` — CHI@TACC bare-metal reservation for the GPU node
- `app_reservation_id` — KVM@TACC reservation for the app node

### 1.2 Initialize and apply

```bash
terraform init
terraform plan    # review what will be created
terraform apply   # will prompt for confirmation
```

This provisions two nodes:
- **app-node** (KVM@TACC): runs all platform services (k3s control plane + worker)
- **gpu-node** (CHI@TACC bare metal): AMD MI100 GPU, k3s agent, runs training jobs only

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

| Variable | Username | How to get it |
|---|---|---|
| `vault_zulip_secret_key` | — | `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `vault_zulip_admin_email` | — | Your email address |
| `vault_zulip_admin_password` | — | Choose a strong password |
| `vault_chameleon_ec2_access` | — | EC2 Access Key — Chameleon dashboard: Identity → EC2 Credentials → Create |
| `vault_chameleon_ec2_secret` | — | EC2 Secret Key — same as above |
| `vault_postgres_password` | `zulip` | Password for the Zulip PostgreSQL user (database: `zulip`) |
| `vault_mlflow_db_password` | `mlflow_user` | Password for the MLflow PostgreSQL user (database: `mlflow`) |
| `vault_chatsentry_db_password` | `chatsentry_user` | Password for the ChatSentry PostgreSQL user (database: `chatsentry`) |
| `vault_rabbitmq_password` | `zulip` | Password for the RabbitMQ `zulip` vhost user |
| `vault_redis_password` | — | Choose a strong password |
| `vault_grafana_admin_password` | `admin` | Grafana admin password |
| `vault_gpu_service_api_key` | — | `python3 -c "import secrets; print(secrets.token_hex(32))"` — authenticates data pipeline → GPU service |
| `vault_zulip_bot_email` | — | Fill in after Phase 4.2 (Zulip bot creation) |
| `vault_zulip_bot_api_key` | — | Fill in after Phase 4.2 (Zulip bot creation) |
| `vault_google_oauth2_key` | — | Google OAuth Client ID — console.cloud.google.com → APIs & Services → Credentials |
| `vault_google_oauth2_secret` | — | Google OAuth Client Secret — same as above. Redirect URI: `https://zulip.<IP>.nip.io/complete/google/` |

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

## Phase 3 — Ansible (configure nodes and deploy)

All commands run from `infra/ansible/` on your local machine.

### 3.1 Pre-K8s setup

Disables the firewall, installs Docker, creates the local storage directory on both nodes.

```bash
ansible-playbook -i inventory.yml pre_k8s.yml \
  --vault-password-file ~/.vault_pass
```

### 3.2 Install k3s

Three plays in sequence:
1. Install k3s server on app-node (control plane + worker)
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

The Zulip bot credentials must exist before deploying all services. This is a two-step process: deploy Zulip first, create the bot, then fill in credentials and deploy everything else.

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
- Creates Chameleon Swift object store containers (`proj09_object_store`, `proj09_Data`) if they don't exist
- Copies all k8s manifests to the VM at `~/k8s/`
- Substitutes the floating IP placeholder in all manifests
- Creates all k8s secrets from vault values
- Deploys all services in dependency order
- Generates a self-signed TLS cert for the Zulip HTTPS ingress
- Prints access URLs at the end

**Access URLs** (printed at end of playbook):

| Service | URL | Notes |
|---|---|---|
| Zulip | `https://zulip.<IP>.nip.io` | Self-signed cert — click Advanced → Proceed |
| MLflow | `http://mlflow.<IP>.nip.io` | Experiment tracking |
| RabbitMQ | `http://rabbitmq.<IP>.nip.io` | Management UI |
| Adminer | `http://adminer.<IP>.nip.io` | PostgreSQL web UI |
| ChatSentry | `http://chatsentry.<IP>.nip.io` | Moderation dashboard |
| Prometheus | `http://prometheus.<IP>.nip.io` | Metrics explorer |
| Grafana | `http://grafana.<IP>.nip.io` | `admin` / `vault_grafana_admin_password` |
| GE Viewer | `http://ge-viewer.<IP>.nip.io` | Data quality reports |

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

Verify monitoring stack:
```bash
kubectl get pods -n platform | grep -E 'prometheus|grafana|node-exporter|kube-state-metrics'
```

All should be `Running`. Open Grafana at `http://grafana.<IP>.nip.io` and navigate to **Dashboards → ChatSentry Platform** — this is the pre-built dashboard showing:
- Service health (all pods UP/DOWN)
- Moderation request rate and action breakdown
- Inference latency percentiles (p50/p95/p99)
- Toxicity and self-harm score distributions
- Node CPU, memory, and disk usage for both nodes
- Training job status

The inference service exposes the following custom Prometheus metrics:
- `inference_toxicity_score` — histogram of toxicity scores per request
- `inference_self_harm_score` — histogram of self-harm scores per request
- `inference_action_total{action="ALLOW|WARN_AND_OBSCURE|HIDE_AND_STRIKE|ALERT_ADMIN"}` — counter of moderation decisions
- `inference_latency_ms` — model inference latency histogram

---

## Phase 7 — Training pipeline

### 7.1 How training works

Training runs on the GPU node via a Kubernetes CronJob (`training-cronjob.yaml`) that fires daily at 01:00 UTC. The CronJob uses `nsenter` to break into the host network namespace and run `scripts/retrain_latest.sh` as the `cc` user directly on the GPU node. This is necessary because AMD ROCm GPU passthrough works with Docker's `--device` flags but not through Kubernetes containerd.

The script:
1. Finds the latest versioned dataset folder in `rclone_s3:proj09_Data/zulip-training-data/`
2. Downloads it locally to the GPU node
3. Pulls the trainer Docker image
4. Runs `docker run` with `--device=/dev/kfd --device=/dev/dri` for AMD GPU access
5. On success + quality gate pass: uploads `best_model.pt` to `rclone_s3:proj09_object_store/`

### 7.2 Upload training data to Chameleon S3

Each CSV must have columns: `text`, `is_suicide`, `is_toxicity`.

```bash
VERSION="v$(date +%Y%m%d-%H%M%S)"

export AWS_ACCESS_KEY_ID=<vault_chameleon_ec2_access>
export AWS_SECRET_ACCESS_KEY=<vault_chameleon_ec2_secret>
export AWS_DEFAULT_REGION=us-east-1

for f in train.csv val.csv test.csv; do
  aws s3 cp $f s3://proj09_Data/zulip-training-data/$VERSION/$f \
    --endpoint-url https://chi.tacc.chameleoncloud.org:7480
done
```

### 7.3 Trigger a manual training run

```bash
ssh -i ~/.ssh/id_rsa_chameleon cc@$APP_NODE_IP
kubectl create job --from=cronjob/training-cronjob manual-training-$(date +%s) -n platform
```

### 7.4 Monitor the job

```bash
kubectl get pods -n platform -w
kubectl logs -n platform -l job-name=<job-name> -f
```

For detailed GPU utilisation, SSH to the GPU node:
```bash
ssh -i ~/.ssh/id_rsa_chameleon cc@$GPU_NODE_IP
rocm-smi
```

### 7.5 Automatic model promotion

The `inference-monitor` CronJob runs every 30 minutes. It:
- Checks if `best_model.pt` in S3 is newer than what the inference pod has loaded → triggers a rolling restart to pick up the new weights
- Checks for score drift (24h avg confidence > 10 points above all-time baseline) or excessive ALLOW rate (>95%) → rolls back to `best_model_backup.pt`

No manual steps are needed after a successful training run — the inference pod will automatically reload the new model within 30 minutes.

### 7.6 Re-run a failed job

```bash
kubectl delete job <job-name> -n platform
kubectl create job --from=cronjob/training-cronjob manual-training-$(date +%s) -n platform
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
export GPU_NODE_IP=$(terraform output -raw gpu_node_floating_ip)

cd ../ansible
ansible-playbook -i inventory.yml pre_k8s.yml --vault-password-file ~/.vault_pass
ansible-playbook -i inventory.yml install_k3s.yml --vault-password-file ~/.vault_pass
ansible-playbook -i inventory.yml post_k8s.yml --vault-password-file ~/.vault_pass
```

`post_k8s.yml` is idempotent — it detects an existing Zulip realm and skips realm creation on redeployment.

The Cinder volume persists across redeployments (`prevent_destroy = true` in Terraform) — all PostgreSQL data, MLflow artifacts, and PVC data survive.

**Note:** After redeployment the bot credentials stay the same (stored in vault), but the `#moderation` stream and bot moderator role must already exist in the persisted Zulip data — no manual steps needed on redeployment. rclone is installed and configured automatically by `post_k8s.yml`.

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

**Training job stuck / not using GPU** — SSH to the GPU node and check running Docker containers:
```bash
ssh -i ~/.ssh/id_rsa_chameleon cc@$GPU_NODE_IP
sudo docker ps
rocm-smi   # GPU utilisation should be ~100% during training
```

**rclone: remote not configured** — re-run `post_k8s.yml`. It installs rclone and writes the config from vault credentials automatically.

**Re-check all pod status:**
```bash
kubectl get pods -A
```

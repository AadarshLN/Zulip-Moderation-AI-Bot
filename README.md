# ChatSentry

AI-powered content moderation for Zulip chat.

---

## What it does

Every message posted in a Zulip organisation is automatically scored for toxicity and self-harm content by a fine-tuned [hateBERT](https://huggingface.co/GroNLP/hateBERT) model. Based on the scores, the bot takes one of four actions:

| Action | Trigger | Bot behaviour |
|---|---|---|
| `ALLOW` | toxicity < 0.60 and self_harm < 0.30 | No action |
| `WARN_AND_OBSCURE` | toxicity 0.60–0.85 | Post to `#moderation` stream for human review |
| `HIDE_AND_STRIKE` | toxicity > 0.85 | Delete the message |
| `ALERT_ADMIN` | self_harm > 0.30 | Post to `#moderation`, DM sender with crisis resources |

All decisions are logged to PostgreSQL and visible in the ChatSentry dashboard. The model retrains daily on the GPU node and auto-promotes if it passes the quality gate.

---

## Architecture

```
Zulip org (300 members)
        │  event stream
        ▼
  ┌─────────────┐
  │  zulip-bot  │  Listens for messages, takes moderation actions
  └──────┬──────┘
         │ POST /messages        │ POST /moderate
         ▼                       ▼
  ┌─────────────┐       ┌─────────────────┐
  │  chatsentry │       │    inference    │  hateBERT on CPU
  │  (Flask API)│       │  (FastAPI)      │  toxicity + self_harm scores
  └──────┬──────┘       └────────┬────────┘
         │                       │ metrics
         │ store messages        ▼
         ▼              ┌─────────────────┐
  ┌─────────────┐       │   Prometheus    │◄── node-exporter, kube-state-metrics
  │  PostgreSQL │       │   + Grafana     │
  └──────┬──────┘       └─────────────────┘
         │
         │ compile training data
         ▼
  ┌──────────────────────────────────────────┐
  │           Training Pipeline              │
  │  K8s CronJob (daily 01:00 UTC)           │
  │  → nsenter → retrain_latest.sh           │
  │  → docker run (AMD ROCm, MI100 GPU)      │
  │  → quality gate → upload best_model.pt  │
  └──────────────────────────────────────────┘
         │
         │ S3 (Chameleon object store)
         ▼
  ┌─────────────────┐
  │inference-monitor│  CronJob every 30 min
  │  auto-promote   │  rolling restart when new model detected
  │  auto-rollback  │  reverts on score drift or excessive ALLOW rate
  └─────────────────┘
```

---

## Infrastructure

Two Chameleon Cloud nodes:

| Node | Type | Role |
|---|---|---|
| app-node | KVM@TACC (m1.xlarge, 8 vCPU / 16 GB) | k3s control plane + all services |
| gpu-node | CHI@TACC bare metal (AMD MI100 GPU) | k3s agent, training jobs only |

All services run in Kubernetes (k3s). Persistent data lives on a Cinder block volume that survives redeployments.

---

## Data Flow

```
1. User posts message in Zulip
         │
2. zulip-bot receives event
         │
3. ──────┬──────────────────────────────────────────
         │                                          │
   POST /messages (chatsentry)              POST /moderate (inference)
   • clean + store message in PostgreSQL    • tokenize with hateBERT
   • run Great Expectations validation      • return toxicity + self_harm scores
         │                                  • log to PostgreSQL moderation table
         └──────────────────────────────────────────
         │
4. zulip-bot applies action (ALLOW / WARN / HIDE / ALERT)
         │
5. ChatSentry periodically compiles new training data:
   • pulls flagged messages from PostgreSQL
   • merges with synthetic data from S3
   • runs data quality checks (Great Expectations)
   • uploads versioned CSVs to S3 (proj09_Data)
         │
6. Daily CronJob on GPU node:
   • downloads latest versioned dataset via rclone
   • trains hateBERT with AMD ROCm Docker container
   • quality gate: F1 ≥ threshold required to promote
   • on pass: uploads best_model.pt to S3 (proj09_object_store)
         │
7. inference-monitor (every 30 min):
   • detects new model in S3 → rolling restart of inference pod
   • detects score drift or too-permissive model → rollback to backup
```

---

## Directory Structure

```
├── .github/
│   └── workflows/ci.yml          Ruff lint + format check on PRs
│
├── config/
│   ├── experiments.yaml          Training experiment definitions (runs, hyperparams)
│   └── pipeline.yaml             Data pipeline configuration
│
├── docker/                       Local development only
│   ├── docker-compose.yaml       Postgres + API + GE Viewer for local dev
│   ├── docker-compose-mlflow.yaml  Standalone MLflow for local dev
│   ├── Dockerfile.api            API Dockerfile for local dev
│   └── init_sql/                 PostgreSQL schema + seed data
│
├── docs/
│   └── instructions.md           Full deployment guide (Terraform → Ansible → K8s)
│
├── infra/
│   ├── ansible/
│   │   ├── inventory.yml         Node definitions (app-node, gpu-node)
│   │   ├── pre_k8s.yml           Docker install, firewall, storage setup
│   │   ├── install_k3s.yml       k3s install, ROCm runtime, GPU device plugin
│   │   ├── post_k8s.yml          Secrets, manifest deploy, service bootstrap
│   │   └── group_vars/all/       Vault-encrypted secrets
│   ├── k8s/
│   │   ├── namespaces.yaml
│   │   ├── platform/             All platform service manifests
│   │   └── zulip/                Zulip StatefulSet + services
│   └── terraform/                Chameleon Cloud VMs, networking, Cinder volume
│
├── scripts/
│   ├── retrain_latest.sh         Training entrypoint — runs on GPU node via CronJob
│   ├── train.py                  PyTorch training loop (hateBERT multi-head)
│   ├── split_data.py             Dataset splitting utility
│   └── send_test_messages.py     Dev utility: send synthetic messages to Zulip
│
├── services/
│   ├── chatsentry/               Flask API: message storage, data pipeline, dashboard
│   ├── ge-viewer/                Great Expectations HTML report viewer (Flask)
│   ├── inference/                FastAPI: hateBERT inference + Prometheus metrics
│   └── zulip-bot/                Zulip event listener + moderation action handler
│
├── src/                          Shared Python source (gpu_service, data, utils)
│
├── tests/
│   ├── test_dashboard.py
│   ├── test_ingest_and_expand.py
│   └── evaluate.sh               Apache Bench performance test against /moderate
│
├── train/
│   ├── Dockerfile                CUDA/NVIDIA training container (alternate backend)
│   └── requirements.txt
│
├── Dockerfile.data               Data pipeline container (CPU, no GPU needed)
├── Dockerfile.gpu-service        GPU service container (AMD ROCm)
├── Dockerfile.training           Training container (AMD ROCm — used on MI100)
├── pyproject.toml                Python project config + ruff settings
└── requirements.txt              Shared Python dependencies
```

---

## Service Endpoints

All endpoints use `nip.io` wildcard DNS — no DNS setup needed.

| Service | URL |
|---|---|
| Zulip | `https://zulip.<IP>.nip.io` |
| Grafana | `http://grafana.<IP>.nip.io` |
| Prometheus | `http://prometheus.<IP>.nip.io` |
| MLflow | `http://mlflow.<IP>.nip.io` |
| ChatSentry | `http://chatsentry.<IP>.nip.io` |
| GE Viewer | `http://ge-viewer.<IP>.nip.io` |
| Adminer (DB UI) | `http://adminer.<IP>.nip.io` |
| RabbitMQ | `http://rabbitmq.<IP>.nip.io` |

---

## Deployment

See [docs/instructions.md](docs/instructions.md) for the full step-by-step guide covering:
- Terraform provisioning
- Ansible configuration (k3s, Docker, ROCm, secrets)
- Zulip bot setup
- Service deployment
- Training pipeline setup (rclone, data upload)
- Redeployment when reservations expire

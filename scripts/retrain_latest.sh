#!/usr/bin/env bash
set -euo pipefail

REMOTE="rclone_s3:proj09_Data/zulip-training-data"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
LOCAL_DATA_ROOT="$REPO_ROOT/retraining-data"
IMAGE_NAME="zulip-moderation"
RUN_NAME="${1:-distilbert_multihead}"
MLFLOW_URI="${MLFLOW_TRACKING_URI:-http://mlflow.129.114.26.93.nip.io/}"
SESSION_NAME="retraining_$(date +%F_%H-%M-%S)"
echo "== Checking rclone =="
if ! command -v rclone >/dev/null 2>&1; then
  echo "rclone is not installed."
  echo "Install it with: curl https://rclone.org/install.sh | sudo bash"
  exit 1
fi

if ! rclone listremotes | grep -q '^rclone_s3:$'; then
  echo "rclone remote 'rclone_s3' is not configured."
  exit 1
fi

echo "== Finding latest retraining dataset =="
LATEST_FOLDER=$(
  rclone lsf "$REMOTE" \
    | grep -E '^v[0-9]{8}-[0-9]{6}/$' \
    | sort \
    | tail -n 1 \
    | sed 's:/$::'
)

if [[ -z "${LATEST_FOLDER:-}" ]]; then
  echo "No versioned retraining folders found under $REMOTE"
  exit 1
fi

echo "Latest folder: $LATEST_FOLDER"

mkdir -p "$LOCAL_DATA_ROOT"

echo "== Copying latest dataset locally =="
if [[ -d "$LOCAL_DATA_ROOT/$LATEST_FOLDER" ]]; then
  echo "Local copy already exists: $LOCAL_DATA_ROOT/$LATEST_FOLDER"
else
  rclone copy "$REMOTE/$LATEST_FOLDER" "$LOCAL_DATA_ROOT/$LATEST_FOLDER"
fi

echo "== Verifying files =="
ls -lah "$LOCAL_DATA_ROOT/$LATEST_FOLDER"

if [[ ! -f "$LOCAL_DATA_ROOT/$LATEST_FOLDER/train.csv" ]]; then
  echo "Missing train.csv"
  exit 1
fi

if [[ ! -f "$LOCAL_DATA_ROOT/$LATEST_FOLDER/test.csv" ]]; then
  echo "Missing test.csv"
  exit 1
fi

echo "== Building Docker image =="
cd "$REPO_ROOT"
sudo docker build -t "$IMAGE_NAME" .

echo "== Running training =="
sudo docker run --rm -it \
  --device=/dev/kfd \
  --device=/dev/dri \
  --group-add video \
  --ipc=host \
  -v "$REPO_ROOT:/workspace" \
  -e GIT_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)" \
  -e MLFLOW_TRACKING_URI="$MLFLOW_URI" \
  "$IMAGE_NAME" \
  python -u scripts/train.py --config configs/experiments.yaml --run "$RUN_NAME"

echo "== Done =="
echo "== Backing up MLflow runs and models =="



if [[ -d "$REPO_ROOT/configs/mlflow_data" ]]; then
  rclone sync --s3-no-check-bucket "$REPO_ROOT/configs/mlflow_data" \
  "rclone_s3:proj09-retrain-runs-al9581/mlflow-backup/$SESSION_NAME/mlflow_data"
else
  echo "No mlflow_data directory found at $REPO_ROOT/configs/mlflow_data"
fi


BEST_MODEL_PATH="$REPO_ROOT/outputs/$RUN_NAME/best_model.pt"
if [[ -f "$BEST_MODEL_PATH" ]]; then
  echo "Copying $BEST_MODEL_PATH to container storage..."
  rclone copyto --s3-no-check-bucket "$BEST_MODEL_PATH" \
    "rclone_s3:proj09-retrain-runs-al9581/model_backups/$SESSION_NAME/best_models/$RUN_NAME/best_model.pt"
else
  echo "No best_model.pt found at $BEST_MODEL_PATH"
fi

echo "Backup completed for session: $SESSION_NAME"


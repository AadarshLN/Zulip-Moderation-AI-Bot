import argparse
import os
import re
import subprocess
import time
from pathlib import Path

import mlflow
import mlflow.pytorch
import mlflow.sklearn
import numpy as np
import torch
import torch.multiprocessing as mp
import yaml
from torch.utils.data import DataLoader
from tqdm import tqdm
from transformers import AutoTokenizer, get_linear_schedule_with_warmup

from src.data import MultiTaskTextDataset, load_splits
from src.metrics import compute_multitask_metrics, flatten_metrics, save_json
from src.models import TfidfLogRegMultiOutput, TransformerMultiHeadModel

mp.set_sharing_strategy("file_system")

"""def upload_to_object_store(local_path: str, remote_path: str):
    subprocess.run(
        ["rclone", "copy", local_path, remote_path],
        check=True,
    )"""


def passes_quality_gates(test_metrics: dict):
    reasons = []

    suicide = test_metrics["suicide"]
    toxicity = test_metrics["toxicity"]

    # Self-harm / suicide: prioritize recall
    if suicide["recall"] < 0.85:
        reasons.append(f"suicide recall too low: {suicide['recall']:.4f} < 0.85")

    if suicide["f1"] < 0.75:
        reasons.append(f"suicide f1 too low: {suicide['f1']:.4f} < 0.75")

    if suicide["precision"] < 0.75:
        reasons.append(f"suicide precision too low: {suicide['precision']:.4f} < 0.75")

    # Toxicity: balanced performance
    if toxicity["f1"] < 0.80:
        reasons.append(f"toxicity f1 too low: {toxicity['f1']:.4f} < 0.80")

    if toxicity["precision"] < 0.75:
        reasons.append(f"toxicity precision too low: {toxicity['precision']:.4f} < 0.75")

    if toxicity["recall"] < 0.75:
        reasons.append(f"toxicity recall too low: {toxicity['recall']:.4f} < 0.75")

    passed = len(reasons) == 0
    return passed, reasons


def _find_latest_dataset_dir(dataset_root: str) -> Path:
    root = Path(dataset_root)

    if not root.exists():
        raise FileNotFoundError(f"Root not found :{root}")

    subdirs = [p for p in root.iterdir() if p.is_dir() and p.name.startswith("v")]

    if not subdirs:
        raise FileNotFoundError(f"No dataset folders found inside: {root}")

    # Prefer most recently modified folder
    latest_dir = sorted(subdirs, key=lambda p: p.name)[-1]
    print(f"Using latest dataset folder: {latest_dir}")
    return latest_dir


def _find_split_file(folder: Path, split_name: str, required: bool = True):
    candidates = [
        folder / f"{split_name}.csv",
        folder / f"{split_name}.parquet",
        folder / split_name / f"{split_name}.csv",
        folder / split_name / f"{split_name}.parquet",
    ]

    for path in candidates:
        if path.exists():
            return path

    # looser fallback: match filenames containing split name
    pattern = re.compile(rf".*{split_name}.*\.(csv|parquet)$", re.IGNORECASE)
    for path in folder.rglob("*"):
        if path.is_file() and pattern.match(path.name):
            return path

    if required:
        raise FileNotFoundError(f"Could not find required split '{split_name}' in {folder}")

    return None


def resolve_data_paths(cfg):
    data_cfg = cfg["data"]
    root = data_cfg["dataset_root"]
    latest_dir = _find_latest_dataset_dir(root)

    train_path = latest_dir / "train.csv"
    test_path = latest_dir / "test.csv"

    if not train_path.exists() or not test_path.exists():
        raise FileNotFoundError(f"train/test missing in {latest_dir}")

    val_path = None
    if (latest_dir / "val.csv").exists():
        val_path = latest_dir / "val.csv"
    elif (latest_dir / "validation.csv").exists():
        val_path = latest_dir / "validation.csv"

    print(f"Train: {train_path}")
    print(f"Val: {val_path}")
    print(f"Test: {test_path}")

    return str(train_path), str(val_path) if val_path else None, str(test_path)


def deep_update(base: dict, updates: dict):
    result = dict(base)
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = deep_update(result[k], v)
        else:
            result[k] = v
    return result


def resolve_run_config(full_cfg: dict, run_name: str):
    if "runs" not in full_cfg:
        raise ValueError("Config must contain a top-level 'runs' section")

    if run_name not in full_cfg["runs"]:
        raise ValueError(f"Run '{run_name}' not found in config. Available: {list(full_cfg['runs'].keys())}")

    base_cfg = {k: v for k, v in full_cfg.items() if k != "runs"}
    run_overrides = full_cfg["runs"][run_name]

    cfg = deep_update(base_cfg, run_overrides)

    if "experiment" not in cfg:
        cfg["experiment"] = {}
    cfg["experiment"]["run_name"] = run_name

    return cfg


def load_config(path: str):
    with open(path, "r") as f:
        return yaml.safe_load(f)


def get_git_sha():
    env_sha = os.getenv("GIT_SHA")
    if env_sha:
        return env_sha

    candidate_paths = [
        Path.cwd(),
        Path(__file__).resolve().parents[1],
        Path("/home/cc/Zulip-Moderation-AI-Bot"),
    ]
    for path in candidate_paths:
        try:
            print("Trying sha")
            sha = (
                subprocess.check_output(
                    ["git", "-C", str(path), "rev-parse", "HEAD"],
                    stderr=subprocess.STDOUT,
                )
                .decode()
                .strip()
            )
            print(f"Resolved sha : {sha}")
            return sha
        except Exception as e:
            print(f"Git SHA lookup failed for {path}: {e}")
            return "unknown"


def get_device():
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def collect_env_info():
    info = {
        "git_sha": get_git_sha(),
        "torch_version": torch.__version__,
        "cuda_available": torch.cuda.is_available(),
    }
    if torch.cuda.is_available():
        info["gpu_name"] = torch.cuda.get_device_name(0)
    return info


def build_dataloaders(cfg):
    train_path, val_path, test_path = resolve_data_paths(cfg)
    bundle = load_splits(
        train_path,
        val_path,
        test_path,
    )

    tokenizer = AutoTokenizer.from_pretrained(cfg["model"]["encoder_name"], use_fast=True)

    train_ds = MultiTaskTextDataset(bundle.train_df, tokenizer, cfg["data"]["max_length"])
    test_ds = MultiTaskTextDataset(bundle.test_df, tokenizer, cfg["data"]["max_length"])

    val_ds = None
    if bundle.val_df is not None:
        val_ds = MultiTaskTextDataset(bundle.val_df, tokenizer, cfg["data"]["max_length"])

    train_loader = DataLoader(
        train_ds,
        batch_size=cfg["training"]["batch_size"],
        shuffle=True,
        num_workers=cfg["training"].get("num_workers", 2),
    )
    val_loader = None
    if val_ds is not None:
        val_loader = DataLoader(
            val_ds,
            batch_size=cfg["training"]["batch_size"],
            shuffle=False,
            num_workers=cfg["training"].get("num_workers", 2),
        )

    test_loader = DataLoader(
        test_ds,
        batch_size=cfg["training"]["batch_size"],
        shuffle=False,
        num_workers=cfg["training"].get("num_workers", 2),
    )
    return bundle, train_loader, val_loader, test_loader


def evaluate_transformer(model, dataloader, device, thresholds):
    model.eval()
    all_probs = []
    all_true = []

    with torch.no_grad():
        for batch in dataloader:
            input_ids = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)
            labels = batch["labels"].cpu().numpy()

            logits = model(input_ids=input_ids, attention_mask=attention_mask)
            probs = torch.sigmoid(logits).cpu().numpy()

            all_probs.append(probs)
            all_true.append(labels)

    y_prob = np.concatenate(all_probs, axis=0)
    y_true = np.concatenate(all_true, axis=0)

    metrics = compute_multitask_metrics(y_true, y_prob, thresholds)
    return metrics, y_true, y_prob


def tune_thresholds(y_true, y_prob):
    best = {"suicide": 0.5, "toxicity": 0.5}
    search_space = np.arange(0.1, 0.91, 0.05)

    for task_idx, task_name in enumerate(["suicide", "toxicity"]):
        best_f1 = -1.0
        best_t = 0.5
        for t in search_space:
            thresholds = {"suicide": 0.5, "toxicity": 0.5}
            thresholds[task_name] = float(t)
            metrics = compute_multitask_metrics(y_true, y_prob, thresholds)
            f1 = metrics[task_name]["f1"]
            if f1 > best_f1:
                best_f1 = f1
                best_t = float(t)
        best[task_name] = best_t

    return best


def run_tfidf_baseline(cfg):
    train_path, val_path, test_path = resolve_data_paths(cfg)
    bundle = load_splits(
        train_path,
        val_path,
        test_path,
    )

    train_x = bundle.train_df["text"].tolist()
    test_x = bundle.test_df["text"].tolist()

    train_y = bundle.train_df[["is_suicide", "is_toxicity"]].values
    test_y = bundle.test_df[["is_suicide", "is_toxicity"]].values

    val_x = None
    val_y = None
    if bundle.val_df is not None:
        val_x = bundle.val_df["text"].tolist()
        val_y = bundle.val_df[["is_suicide", "is_toxicity"]].values

    model = TfidfLogRegMultiOutput(
        max_features=cfg["baseline"]["max_features"],
        ngram_range=tuple(cfg["baseline"]["ngram_range"]),
        c=cfg["baseline"]["c"],
        max_iter=cfg["baseline"]["max_iter"],
    )

    start = time.time()
    model.fit(train_x, train_y)
    train_time = time.time() - start

    thresholds = {"suicide": 0.5, "toxicity": 0.5}
    val_metrics = None

    if val_x is not None:
        val_s_prob, val_t_prob = model.predict_proba(val_x)
        val_prob = np.stack([val_s_prob, val_t_prob], axis=1)
        if cfg["evaluation"].get("threshold_tuning", False):
            thresholds = tune_thresholds(val_y, val_prob)
        val_metrics = compute_multitask_metrics(val_y, val_prob, thresholds)

    test_s_prob, test_t_prob = model.predict_proba(test_x)
    test_prob = np.stack([test_s_prob, test_t_prob], axis=1)
    test_metrics = compute_multitask_metrics(test_y, test_prob, thresholds)

    return model, train_time, thresholds, val_metrics, test_metrics


def run_transformer(cfg):
    device = get_device()
    bundle, train_loader, val_loader, test_loader = build_dataloaders(cfg)

    model = TransformerMultiHeadModel(
        encoder_name=cfg["model"]["encoder_name"],
        dropout=cfg["model"].get("dropout", 0.1),
    ).to(device)
    resume_ckpt = os.getenv("RESUME_FROM_CHECKPOINT") or cfg["training"].get("resume_from_checkpoint")

    print(f"RESUME_FROM_CHECKPOINT resolved to: {resume_ckpt}")
    if resume_ckpt and Path(resume_ckpt).exists():
        print(f"Loading checkpoint from {resume_ckpt}")
        model.load_state_dict(torch.load(resume_ckpt, map_location=device))
    else:
        print("No checkpoint loaded. Training from scratch.")

    train_y = bundle.train_df[["is_suicide", "is_toxicity"]].values
    pos_counts = train_y.sum(axis=0)
    neg_counts = len(train_y) - pos_counts
    pos_weight = torch.tensor(
        [
            neg_counts[0] / max(pos_counts[0], 1),
            neg_counts[1] / max(pos_counts[1], 1),
        ],
        dtype=torch.float32,
        device=device,
    )

    if cfg["training"].get("weighted_bce", False):
        criterion = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    else:
        criterion = torch.nn.BCEWithLogitsLoss()

    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=cfg["training"]["lr"],
        weight_decay=cfg["training"].get("weight_decay", 0.01),
    )

    total_steps = len(train_loader) * cfg["training"]["epochs"]
    warmup_steps = int(total_steps * cfg["training"].get("warmup_ratio", 0.1))
    scheduler = get_linear_schedule_with_warmup(
        optimizer,
        num_warmup_steps=warmup_steps,
        num_training_steps=total_steps,
    )

    best_val_f1 = -1.0
    best_model_path = Path(cfg["output"]["dir"]) / "best_model.pt"
    best_model_path.parent.mkdir(parents=True, exist_ok=True)

    start = time.time()

    for epoch in range(cfg["training"]["epochs"]):
        model.train()
        epoch_loss = 0.0

        loop = tqdm(train_loader, desc=f"Epoch {epoch + 1}")
        for batch in loop:
            input_ids = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)
            labels = batch["labels"].to(device)

            optimizer.zero_grad()
            logits = model(input_ids=input_ids, attention_mask=attention_mask)
            loss = criterion(logits, labels)
            loss.backward()
            optimizer.step()
            scheduler.step()

            epoch_loss += loss.item()
            loop.set_postfix(loss=loss.item())

        mlflow.log_metric("train_loss", epoch_loss / max(len(train_loader), 1), step=epoch)

        if val_loader is not None:
            val_metrics, _, _ = evaluate_transformer(model, val_loader, device, {"suicide": 0.5, "toxicity": 0.5})

            # flat_val = {f"val_{k}": v for k, v in flatten_metrics(val_metrics).items()}
            for k, v in flatten_metrics(val_metrics).items():
                if v is not None and not np.isnan(v):
                    mlflow.log_metric(f"val_{k}", float(v), step=epoch)
            if val_metrics["avg_f1"] > best_val_f1:
                best_val_f1 = val_metrics["avg_f1"]
                torch.save(model.state_dict(), best_model_path)
        else:
            torch.save(model.state_dict(), best_model_path)

    train_time = time.time() - start

    model.load_state_dict(torch.load(best_model_path, map_location=device))

    thresholds = {"suicide": 0.5, "toxicity": 0.5}

    val_metrics = None
    if val_loader is not None:
        val_metrics, val_y, val_prob = evaluate_transformer(
            model, val_loader, device, {"suicide": 0.5, "toxicity": 0.5}
        )
        if cfg["evaluation"].get("threshold_tuning", False):
            thresholds = tune_thresholds(val_y, val_prob)
        val_metrics, _, _ = evaluate_transformer(model, val_loader, device, thresholds)
    test_metrics, _, _ = evaluate_transformer(model, test_loader, device, thresholds)

    return model, train_time, thresholds, val_metrics, test_metrics, best_model_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, required=True)
    parser.add_argument("--run", type=str, required=True)
    args = parser.parse_args()

    full_cfg = load_config(args.config)
    cfg = resolve_run_config(full_cfg, args.run)

    tracking_uri = os.getenv("MLFLOW_TRACKING_URI", cfg["mlflow"]["tracking_uri"])
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(cfg["experiment"]["name"])
    mlflow.enable_system_metrics_logging()
    with mlflow.start_run(run_name=cfg["experiment"]["run_name"]):
        mlflow.log_param("selected_run", args.run)
        mlflow.log_artifact(args.config)

        print("CWD:", Path.cwd())
        print("__file__:", Path(__file__).resolve())
        print("Repo guess:", Path(__file__).resolve().parents[1])

        for k, v in collect_env_info().items():
            mlflow.log_param(k, v)

        mlflow.log_params(
            {
                "model_type": cfg["model"]["type"],
                "encoder_name": cfg["model"].get("encoder_name", "n/a"),
                "epochs": cfg["training"].get("epochs", 0),
                "batch_size": cfg["training"].get("batch_size", 0),
                "lr": cfg["training"].get("lr", 0.0),
                "weighted_bce": cfg["training"].get("weighted_bce", False),
                "threshold_tuning": cfg["evaluation"].get("threshold_tuning", False),
            }
        )

        if cfg["model"]["type"] == "tfidf_logreg":
            model, train_time, thresholds, val_metrics, test_metrics = run_tfidf_baseline(cfg)
            # mlflow.sklearn.log_model(model.model, artifact_path="model")
        else:
            model, train_time, thresholds, val_metrics, test_metrics, best_model_path = run_transformer(cfg)

        passed_gate, gate_reasons = passes_quality_gates(test_metrics)
        mlflow.log_param("quality_gate_passed", passed_gate)
        mlflow.set_tag("quality_gate_passed", str(passed_gate).lower())

        if gate_reasons:
            mlflow.set_tag("quality_gate_reasons", " | ".join(gate_reasons))

            # mlflow.log_artifact(str(best_model_path))
            # print(f"Model saved locally at: {best_model_path}")

        # mlflow.log_metric("train_time_sec", train_time)
        # print(f"Model saved locally at: {best_model_path}")
        if cfg["model"]["type"] != "tfidf_logreg":
            print(f"Model saved locally at: {best_model_path}")

        # mlflow.log_metric({f"test_{k}": v for k, v in flatten_metrics(test_metrics).items()})
        for k, v in flatten_metrics(test_metrics).items():
            if v is not None and not np.isnan(v):
                mlflow.log_metric(f"test_{k}", float(v))

        Path(cfg["output"]["dir"]).mkdir(parents=True, exist_ok=True)
        thresholds_path = Path(cfg["output"]["dir"]) / "thresholds.json"
        metrics_path = Path(cfg["output"]["dir"]) / "final_metrics.json"
        quality_gate_path = Path(cfg["output"]["dir"]) / "quality_gate.json"

        save_json(thresholds, thresholds_path)
        save_json({"val": val_metrics, "test": test_metrics}, metrics_path)
        save_json({"passed": passed_gate, "reasons": gate_reasons}, quality_gate_path)
        mlflow.log_artifact(str(thresholds_path))
        mlflow.log_artifact(str(metrics_path))
        mlflow.log_artifact(str(quality_gate_path))
        print("Thresholds:", thresholds)
        print("Validation metrics:", val_metrics)
        print("Test metrics:", test_metrics)

        if passed_gate:
            print("Model passed quality gates. Logging model artifact.")
            if cfg["model"]["type"] == "tfidf_logreg":
                mlflow.sklearn.log_model(model.model, artifact_path="model")
            else:
                try:
                    mlflow.pytorch.log_model(model, artifact_path="model")
                except Exception as e:
                    print(e)
                approved_remote = "proj09_object_store:"
                print(f"Uploading approved checkpoint to {approved_remote}")
                # upload_to_object_store(str(best_model_path), approved_remote)
        else:
            print("Model failed quality gates. Skipping model logging.")
            for reason in gate_reasons:
                print(" -", reason)


if __name__ == "__main__":
    main()

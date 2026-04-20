from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from sklearn.metrics import (
    accuracy_score,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)


def compute_binary_metrics(y_true, y_prob, threshold=0.5):
    y_pred = (y_prob >= threshold).astype(int)

    metrics = {
        "accuracy": accuracy_score(y_true, y_pred),
        "precision": precision_score(y_true, y_pred, zero_division=0),
        "recall": recall_score(y_true, y_pred, zero_division=0),
        "f1": f1_score(y_true, y_pred, zero_division=0),
    }

    if len(np.unique(y_true)) > 1:
        metrics["auroc"] = roc_auc_score(y_true, y_prob)
    else:
        metrics["auroc"] = float("nan")

    return metrics


def compute_multitask_metrics(y_true, y_prob, thresholds):
    suicide_true = y_true[:, 0]
    toxicity_true = y_true[:, 1]

    suicide_prob = y_prob[:, 0]
    toxicity_prob = y_prob[:, 1]

    suicide_metrics = compute_binary_metrics(suicide_true, suicide_prob, threshold=thresholds["suicide"])
    toxicity_metrics = compute_binary_metrics(toxicity_true, toxicity_prob, threshold=thresholds["toxicity"])

    avg_f1 = (suicide_metrics["f1"] + toxicity_metrics["f1"]) / 2.0

    return {
        "suicide": suicide_metrics,
        "toxicity": toxicity_metrics,
        "avg_f1": avg_f1,
    }


def flatten_metrics(metrics_dict):
    flat = {}
    for task, vals in metrics_dict.items():
        if isinstance(vals, dict):
            for k, v in vals.items():
                flat[f"{task}_{k}"] = v
        else:
            flat[task] = vals
    return flat


def save_json(obj, path: str | Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)

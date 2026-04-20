from dataclasses import dataclass

import pandas as pd
import torch
from torch.utils.data import Dataset
from transformers import AutoTokenizer


@dataclass
class TabularDataBundle:
    train_df: pd.DataFrame
    val_df: pd.DataFrame | None
    test_df: pd.DataFrame


def _standardize_columns(df):
    if "cleaned_text" in df.columns and "text" not in df.columns:
        df = df.rename(columns={"cleaned_text": "text"})

    required = ["text", "is_suicide", "is_toxicity"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    return df


def _clean_df(df):
    df["text"] = df["text"].fillna("").astype(str)
    df["is_suicide"] = df["is_suicide"].astype(int)
    df["is_toxicity"] = df["is_toxicity"].astype(int)
    return df


def load_splits(train_path: str, val_path: str | None, test_path: str) -> TabularDataBundle:
    train_df = _clean_df(_standardize_columns(pd.read_csv(train_path)))
    test_df = _clean_df(_standardize_columns(pd.read_csv(test_path)))

    val_df = None
    if val_path is not None:
        val_df = _clean_df(_standardize_columns(pd.read_csv(val_path)))

    return TabularDataBundle(train_df=train_df, val_df=val_df, test_df=test_df)


class MultiTaskTextDataset(Dataset):
    def __init__(self, df: pd.DataFrame, tokenizer, max_length: int):
        self.df = df.reset_index(drop=True)
        self.tokenizer = tokenizer
        self.max_length = max_length

    def __len__(self):
        return len(self.df)

    def __getitem__(self, idx):
        row = self.df.iloc[idx]

        enc = self.tokenizer(
            str(row["text"]),
            truncation=True,
            padding="max_length",
            max_length=self.max_length,
            return_tensors="pt",
        )

        return {
            "input_ids": enc["input_ids"].squeeze(0),
            "attention_mask": enc["attention_mask"].squeeze(0),
            "labels": torch.tensor(
                [float(row["is_suicide"]), float(row["is_toxicity"])],
                dtype=torch.float32,
            ),
        }


def build_tokenizer(model_name: str):
    return AutoTokenizer.from_pretrained(model_name, use_fast=True)

import argparse
from pathlib import Path

import pandas as pd
from sklearn.model_selection import train_test_split


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=str, required=True)
    parser.add_argument("--output_dir", type=str, required=True)
    parser.add_argument("--train_size", type=float, default=0.8)
    parser.add_argument("--val_size", type=float, default=0.1)
    parser.add_argument("--test_size", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    if abs(args.train_size + args.val_size + args.test_size - 1.0) > 1e-8:
        raise ValueError("train_size + val_size + test_size must sum to 1.0")

    df = pd.read_csv(args.input).copy()
    required_cols = {"text", "is_suicide", "is_toxicity"}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    df["text"] = df["text"].astype(str).fillna("")
    df["is_suicide"] = df["is_suicide"].astype(int)
    df["is_toxicity"] = df["is_toxicity"].astype(int)

    df = df.dropna(subset=["text"]).reset_index(drop=True)

    # joint stratification label
    df["stratify_label"] = df["is_suicide"].astype(str) + "_" + df["is_toxicity"].astype(str)

    train_df, temp_df = train_test_split(
        df,
        test_size=(1.0 - args.train_size),
        random_state=args.seed,
        stratify=df["stratify_label"],
    )

    relative_test_size = args.test_size / (args.val_size + args.test_size)

    val_df, test_df = train_test_split(
        temp_df,
        test_size=relative_test_size,
        random_state=args.seed,
        stratify=temp_df["stratify_label"],
    )

    for split_df in (train_df, val_df, test_df):
        split_df.drop(columns=["stratify_label"], inplace=True)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    train_df.to_csv(out_dir / "train.csv", index=False)
    val_df.to_csv(out_dir / "val.csv", index=False)
    test_df.to_csv(out_dir / "test.csv", index=False)

    print("Saved:")
    print(out_dir / "train.csv", len(train_df))
    print(out_dir / "val.csv", len(val_df))
    print(out_dir / "test.csv", len(test_df))


if __name__ == "__main__":
    main()

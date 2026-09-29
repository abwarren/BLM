#!/usr/bin/env python3
"""Build leakage-safe Qwen SFT data from an audited BLM decision table."""

from __future__ import annotations
import argparse, json
from pathlib import Path
from typing import Any
import pandas as pd

AS_OF_FEATURES = [
    "league", "checkpoint", "home_score", "away_score", "period", "clock",
    "pace", "league_avg_pace", "required_pts_per_min", "trap_meter",
    "momentum", "market_move", "confidence", "market_line", "blm_prediction",
]
REQUIRED = {"game_id", "timestamp", "market_line", "blm_prediction", "actual_total"}

def read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    raise ValueError("Input must be .csv or .parquet")

def clean_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        try: return value.item()
        except Exception: pass
    return value

def position(blm: float | None, market: float | None) -> str:
    if blm is None or market is None: return "NO_EDGE"
    if blm < market: return "UNDER"
    if blm > market: return "OVER"
    return "NO_EDGE"

def settlement(actual: float | None, market: float | None) -> str:
    if actual is None or market is None: return "UNKNOWN"
    if actual < market: return "UNDER"
    if actual > market: return "OVER"
    return "PUSH"

def make_example(row: pd.Series, idx: int) -> dict[str, Any]:
    prompt = {
        k: clean_value(row[k]) for k in AS_OF_FEATURES
        if k in row.index and clean_value(row[k]) is not None
    }
    pos = position(clean_value(row.get("blm_prediction")), clean_value(row.get("market_line")))
    result = settlement(clean_value(row.get("actual_total")), clean_value(row.get("market_line")))
    custom = clean_value(row.get("analyst_response"))
    completion = str(custom) if custom else (
        f"POSITION: {pos}\nSETTLEMENT: {result}\n"
        "Use the deterministic BLM engine as the source of truth for "
        "mathematical calculations; do not invent missing values."
    )
    return {
        "id": f"{row['game_id']}-{idx}",
        "messages": [
            {"role": "system", "content":
             "You are the BLM analyst. Interpret only the supplied as-of "
             "decision state. Do not invent data or change BLM mathematical rules."},
            {"role": "user", "content":
             "Analyze this BLM decision state. Return the structured position "
             "and explain the state without using future information.\n\nBLM_STATE:\n"
             + json.dumps(prompt, sort_keys=True)},
            {"role": "assistant", "content": completion},
        ],
        "target": {"position": pos, "settlement": result},
        "game_id": str(row["game_id"]),
        "timestamp": str(row["timestamp"]),
    }

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--output-dir", required=True, type=Path)
    ap.add_argument("--train-fraction", type=float, default=0.8)
    args = ap.parse_args()
    df = read_table(args.input)
    missing = REQUIRED - set(df.columns)
    if missing: raise ValueError(f"Missing required columns: {sorted(missing)}")
    if not 0.5 <= args.train_fraction < 1: raise ValueError("--train-fraction must be >=0.5 and <1")
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="raise")
    df["game_id"] = df["game_id"].astype(str)

    games = (df.groupby("game_id", as_index=False)["timestamp"].min()
             .sort_values(["timestamp", "game_id"]))
    cut = max(1, int(len(games) * args.train_fraction))
    if cut >= len(games): raise ValueError("Not enough games for validation")
    train_games = set(games.iloc[:cut]["game_id"])
    val_games = set(games.iloc[cut:]["game_id"])
    train_df = df[df["game_id"].isin(train_games)]
    val_df = df[df["game_id"].isin(val_games)]
    args.output_dir.mkdir(parents=True, exist_ok=True)

    def write(frame: pd.DataFrame, path: Path) -> None:
        with path.open("w", encoding="utf-8") as f:
            for idx, (_, row) in enumerate(frame.sort_values("timestamp").iterrows()):
                f.write(json.dumps(make_example(row, idx), ensure_ascii=False) + "\n")
    write(train_df, args.output_dir / "train.jsonl")
    write(val_df, args.output_dir / "validation.jsonl")
    manifest = {
        "source": str(args.input),
        "train_fraction": args.train_fraction,
        "train_games": len(train_games),
        "validation_games": len(val_games),
        "train_rows": len(train_df),
        "validation_rows": len(val_df),
        "train_end": games.iloc[cut - 1]["timestamp"].isoformat(),
        "validation_start": games.iloc[cut]["timestamp"].isoformat(),
        "as_of_features": AS_OF_FEATURES,
        "forbidden_prompt_fields": ["actual_total"],
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()

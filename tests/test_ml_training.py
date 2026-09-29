from __future__ import annotations
import json
import pandas as pd
from ml_training.build_sft import make_example

def test_sft_prompt_excludes_future_actual_total():
    row = pd.Series({
        "game_id": "g1", "timestamp": "2026-01-01T12:00:00Z",
        "league": "NBA", "checkpoint": 75, "home_score": 80, "away_score": 72,
        "market_line": 221.5, "blm_prediction": 216.5, "actual_total": 230,
        "trap_meter": 82})
    example = make_example(row, 0)
    user_text = example["messages"][1]["content"]
    assert "actual_total" not in user_text
    assert "230" not in user_text
    assert example["target"] == {"position": "UNDER", "settlement": "OVER"}

def test_sft_has_qwen_messages():
    row = pd.Series({
        "game_id": "g2", "timestamp": "2026-01-02T12:00:00Z",
        "market_line": 220.5, "blm_prediction": 224.5, "actual_total": 220})
    example = make_example(row, 0)
    assert [m["role"] for m in example["messages"]] == ["system", "user", "assistant"]
    json.dumps(example)

# BLM Qwen Training Pipeline

This is an offline, reproducible training lab for adapting an open-weight Qwen model to BLM analyst behavior.

## Architecture

audited BLM decision rows -> time-safe game split -> Qwen SFT JSONL -> Qwen3 QLoRA -> adapter -> held-out evaluation

The LLM is not the BLM mathematical engine. Production calculations remain deterministic in the existing BLM code.

## Default experiment

- Qwen/Qwen3-8B
- QLoRA, adapter-only
- 2048 token context
- 2 epochs
- chronological game-level train/validation split
- no model weights or private datasets committed to Git

A larger Qwen3 model can be supplied with --model, but must be evaluated as a separate experiment.

## Input contract

The source CSV/Parquet must already be Phase-1/audit approved and contain:
game_id, timestamp, market_line, blm_prediction, actual_total

Optional as-of fields include:
league, checkpoint, home_score, away_score, period, clock, pace, league_avg_pace, required_pts_per_min, trap_meter, momentum, market_move, confidence

You may provide analyst_response for curated completions. Otherwise the builder creates a deterministic structured completion.

## Leakage boundary

Only AS_OF_FEATURES enter the user prompt. actual_total never enters the prompt; it is used only as supervised target metadata/completion.

Games are split chronologically, so snapshots from the same game cannot land in both train and validation.

Do not start training until the BLM dataset gate is PASS.

## Commands

Install:
  pip install -r ml_training/requirements.txt

Build:
  python ml_training/build_sft.py --input /path/to/audited_decision_rows.parquet --output-dir ml_training/data/qwen3_8b

Train:
  python ml_training/train_qwen.py --config ml_training/config/qwen3-8b-qlora.yaml

Evaluate:
  python ml_training/evaluate.py --base-model Qwen/Qwen3-8B --adapter ml_training/artifacts/qwen3-8b-qlora/final_adapter --validation ml_training/data/qwen3_8b/validation.jsonl

The output is an adapter. Merge/deploy only after an explicit out-of-sample comparison against v4-pace-1.

## Safety boundaries

This pipeline does not change the production collector, alert engine, betting execution or dashboard. It does not place bets, deploy a model, read secrets, or upload BLM data.

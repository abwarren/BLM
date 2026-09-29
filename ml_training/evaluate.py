#!/usr/bin/env python3
"""Evaluate structured BLM POSITION/SETTLEMENT accuracy on held-out JSONL."""

from __future__ import annotations
import argparse, json, re
import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

def extract(text: str, key: str):
    m = re.search(rf"\b{key}\s*:\s*([A-Z_]+)", text.upper())
    return m.group(1) if m else None

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-model", required=True)
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--validation", required=True)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(args.base_model, use_fast=True)
    dtype = torch.bfloat16 if torch.cuda.is_available() and torch.cuda.is_bf16_supported() else torch.float16
    base = AutoModelForCausalLM.from_pretrained(
        args.base_model, torch_dtype=dtype, device_map="auto")
    model = PeftModel.from_pretrained(base, args.adapter)
    model.eval()

    rows = [json.loads(x) for x in open(args.validation, encoding="utf-8") if x.strip()]
    if args.limit:
        rows = rows[:args.limit]

    pos_ok = set_ok = 0
    for row in rows:
        prompt = tok.apply_chat_template(
            row["messages"][:-1], tokenize=False, add_generation_prompt=True)
        inputs = tok(prompt, return_tensors="pt").to(model.device)
        with torch.no_grad():
            out = model.generate(**inputs, max_new_tokens=128, do_sample=False)
        generated = tok.decode(
            out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
        pos_ok += extract(generated, "POSITION") == row["target"]["position"]
        set_ok += extract(generated, "SETTLEMENT") == row["target"]["settlement"]

    n = max(len(rows), 1)
    print(json.dumps({
        "rows": len(rows),
        "position_exact_match": pos_ok / n,
        "settlement_exact_match": set_ok / n,
    }, indent=2))

if __name__ == "__main__":
    main()

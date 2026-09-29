#!/usr/bin/env python3
"""Explicit Qwen QLoRA SFT launcher for the BLM training lab."""

from __future__ import annotations
import argparse
from pathlib import Path
import torch, yaml
from datasets import load_dataset
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from transformers import DataCollatorForLanguageModeling, Trainer, TrainingArguments

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--model", default=None)
    args = ap.parse_args()
    cfg = yaml.safe_load(args.config.read_text())
    model_name = args.model or cfg["model"]

    ds = load_dataset("json", data_files={
        "train": cfg["train_file"], "validation": cfg["validation_file"]
    })
    tokenizer = AutoTokenizer.from_pretrained(model_name, use_fast=True)
    if tokenizer.pad_token is None: tokenizer.pad_token = tokenizer.eos_token
    max_len = int(cfg.get("max_seq_length", 2048))

    def tokenize(example):
        text = tokenizer.apply_chat_template(
            example["messages"], tokenize=False, add_generation_prompt=False)
        return tokenizer(text, truncation=True, max_length=max_len)

    tokenized = ds.map(tokenize, remove_columns=ds["train"].column_names)
    use_bf16 = (bool(cfg.get("bf16", True)) and torch.cuda.is_available()
                and torch.cuda.is_bf16_supported())
    use_fp16 = torch.cuda.is_available() and not use_bf16
    dtype = torch.bfloat16 if use_bf16 else torch.float16

    quant = BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=dtype)
    model = AutoModelForCausalLM.from_pretrained(
        model_name, quantization_config=quant, device_map="auto", torch_dtype=dtype)
    model.config.use_cache = False
    model = prepare_model_for_kbit_training(model)
    model = get_peft_model(model, LoraConfig(
        r=int(cfg.get("lora_r", 16)), lora_alpha=int(cfg.get("lora_alpha", 32)),
        lora_dropout=float(cfg.get("lora_dropout", 0.05)),
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        bias="none", task_type="CAUSAL_LM"))
    model.print_trainable_parameters()

    out = Path(cfg["output_dir"]); out.mkdir(parents=True, exist_ok=True)
    train_args = TrainingArguments(
        output_dir=str(out), num_train_epochs=float(cfg.get("epochs", 2)),
        learning_rate=float(cfg.get("learning_rate", 1e-4)),
        warmup_ratio=float(cfg.get("warmup_ratio", 0.05)),
        weight_decay=float(cfg.get("weight_decay", 0.01)),
        per_device_train_batch_size=int(cfg.get("per_device_train_batch_size", 1)),
        per_device_eval_batch_size=int(cfg.get("per_device_eval_batch_size", 1)),
        gradient_accumulation_steps=int(cfg.get("gradient_accumulation_steps", 16)),
        logging_steps=int(cfg.get("logging_steps", 10)), eval_strategy="steps",
        eval_steps=int(cfg.get("eval_steps", 100)), save_steps=int(cfg.get("save_steps", 100)),
        save_total_limit=int(cfg.get("save_total_limit", 2)), bf16=use_bf16, fp16=use_fp16,
        gradient_checkpointing=True, report_to="none")
    trainer = Trainer(
        model=model, args=train_args, train_dataset=tokenized["train"],
        eval_dataset=tokenized["validation"],
        data_collator=DataCollatorForLanguageModeling(tokenizer, mlm=False))
    trainer.train()
    trainer.save_model(str(out / "final_adapter"))
    tokenizer.save_pretrained(str(out / "final_adapter"))

if __name__ == "__main__":
    main()

"""两个神经方法共用的显式训练循环；固定 epoch，不用 final test 选 checkpoint。"""

import json
import math
import os
import time
from pathlib import Path

from data import DataError, data_directory, file_sha256


def model_directory(config):
    try:
        manifest = json.loads((data_directory() / "model_manifest.json").read_text(encoding="utf-8"))
        if manifest["model_name"] != config["model_name"]:
            raise DataError("模型名与准备记录不同")
        directory = Path(os.environ.get("AIR_MODEL_DIR", manifest["local_dir"])).expanduser().resolve()
        for name, checksum in manifest["files"].items():
            if file_sha256(directory / name) != checksum:
                raise DataError(f"模型文件校验失败：{name}")
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise DataError("缺少完整的本地模型。请先运行 prepare_model.py。") from exc
    return directory, manifest


def run_neural(train, evaluation, config, out_dir, use_lora):
    import torch
    from torch.utils.data import DataLoader
    from transformers import AutoModelForSequenceClassification, AutoTokenizer, set_seed

    set_seed(config["seed"])
    device = torch.device(config["device"])
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("配置要求 CUDA，但当前 Python 的 torch.cuda.is_available() 为 False。")
    local, manifest = model_directory(config)
    tokenizer = AutoTokenizer.from_pretrained(local, local_files_only=True)
    model = AutoModelForSequenceClassification.from_pretrained(
        local, local_files_only=True, num_labels=2, use_safetensors=True,
    )
    if model.config.model_type != "distilbert":
        raise ValueError("当前模板的 LoRA 目标层专用于 DistilBERT。")
    if use_lora:
        from peft import LoraConfig, TaskType, get_peft_model

        model = get_peft_model(model, LoraConfig(
            task_type=TaskType.SEQ_CLS, r=config["lora_rank"], lora_alpha=config["lora_alpha"],
            lora_dropout=config["lora_dropout"], target_modules=["q_lin", "v_lin"], bias="none",
            modules_to_save=["pre_classifier", "classifier"],
        ))
    model.to(device)
    if device.type == "cuda":
        torch.cuda.reset_peak_memory_stats(device)

    def encoded_rows(rows):
        tokens = tokenizer([row["text"] for row in rows], truncation=True,
                           padding="max_length", max_length=config["max_length"], return_tensors="pt")
        return [{**{name: tensor[index] for name, tensor in tokens.items()},
                 "labels": torch.tensor(row["label"], dtype=torch.long)}
                for index, row in enumerate(rows)]

    training = encoded_rows(train)
    testing = encoded_rows(evaluation)
    generator = torch.Generator().manual_seed(config["seed"])
    loader = DataLoader(training, batch_size=config["batch_size"], shuffle=True, generator=generator, num_workers=0)
    eval_loader = DataLoader(testing, batch_size=config["eval_batch_size"], shuffle=False, num_workers=0)
    parameters = [parameter for parameter in model.parameters() if parameter.requires_grad]
    trainable = sum(parameter.numel() for parameter in parameters)
    total = sum(parameter.numel() for parameter in model.parameters())
    optimizer = torch.optim.AdamW(parameters, lr=config["learning_rate"], weight_decay=config["weight_decay"])
    accumulation = config["grad_accum_steps"]
    # 保留短尾组真实样本数，使梯度累积不会把最后一个不足批次当成完整批次。
    updates_per_epoch = math.ceil(len(loader) / accumulation)
    history = []
    if device.type == "cuda":
        torch.cuda.synchronize(device)
    started = time.perf_counter()
    for epoch in range(config["epochs"]):
        model.train()
        optimizer.zero_grad(set_to_none=True)
        summed_loss = 0.0
        for index, batch in enumerate(loader):
            batch = {name: tensor.to(device) for name, tensor in batch.items()}
            group_start = (index // accumulation) * accumulation
            group_samples = min(len(training) - group_start * config["batch_size"],
                                accumulation * config["batch_size"])
            loss = model(**batch).loss
            if not torch.isfinite(loss).item():
                raise RuntimeError("训练 loss 非有限值，停止并保留失败记录。")
            batch_samples = batch["labels"].shape[0]
            (loss * batch_samples / group_samples).backward()
            summed_loss += loss.detach().item() * batch_samples
            if (index + 1) % accumulation == 0 or index + 1 == len(loader):
                torch.nn.utils.clip_grad_norm_(parameters, max_norm=1.0)
                optimizer.step()
                optimizer.zero_grad(set_to_none=True)
        average = summed_loss / len(training)
        history.append({"epoch": epoch + 1, "train_loss": average})
        print(f"epoch={epoch + 1}/{config['epochs']} train_loss={average:.6f}", flush=True)
    if device.type == "cuda":
        torch.cuda.synchronize(device)
    train_seconds = time.perf_counter() - started

    model.eval()
    started = time.perf_counter()
    predictions = []
    with torch.no_grad():
        for batch in eval_loader:
            batch = {name: tensor.to(device) for name, tensor in batch.items() if name != "labels"}
            predictions.extend(model(**batch).logits.argmax(dim=-1).cpu().tolist())
    if device.type == "cuda":
        torch.cuda.synchronize(device)
    eval_seconds = time.perf_counter() - started
    if config["save_model"]:
        model.save_pretrained(out_dir / "model", safe_serialization=True)
        tokenizer.save_pretrained(out_dir / "model")
    metadata = {
        "train_seconds": train_seconds, "eval_seconds": eval_seconds, "device": str(device),
        "model_revision": manifest["resolved_revision"], "trainable_parameters": trainable, "total_parameters": total,
        "effective_batch_size": config["batch_size"] * accumulation,
        "optimizer_updates": updates_per_epoch * config["epochs"], "history": history,
        "gpu_name": torch.cuda.get_device_name(device) if device.type == "cuda" else None,
        "peak_allocated_mb": torch.cuda.max_memory_allocated(device) / 1024**2 if device.type == "cuda" else None,
    }
    return predictions, metadata

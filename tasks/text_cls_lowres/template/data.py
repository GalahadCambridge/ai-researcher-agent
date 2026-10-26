"""受保护的数据读取、哈希检查及配对抽样；不下载数据。"""

import hashlib
import json
import os
from pathlib import Path


class DataError(ValueError):
    """数据缺失、校验不通过或数据划分不符合约定。"""


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_key(text):
    """规范化大小写、空白后比较精确重复，不保证检测语义或原电影级泄漏。"""
    return hashlib.sha256(" ".join(text.casefold().split()).encode("utf-8")).hexdigest()


def data_directory():
    if os.environ.get("AIR_DATA_DIR"):
        return Path(os.environ["AIR_DATA_DIR"]).expanduser().resolve()
    home = Path(os.environ.get("AIR_HOME", str(Path.home() / "air"))).expanduser()
    return (home / "data" / "sst2").resolve()


def check_dataset(directory):
    directory = Path(directory)
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        splits = {}
        for split in ("train", "dev", "test"):
            filename = f"{split}.jsonl"
            path = directory / filename
            if file_sha256(path) != manifest["files"][filename]["sha256"]:
                raise DataError(f"{filename} 的 sha256 与准备时记录不一致")
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if not rows or len(rows) != manifest["files"][filename]["rows"]:
                raise DataError(f"{filename} 为空或行数不一致")
            if any(not isinstance(r["text"], str) or not r["text"].strip() or r["label"] not in (0, 1)
                   for r in rows):
                raise DataError(f"{filename} 包含空文本或非法标签")
            if len({r["id"] for r in rows}) != len(rows):
                raise DataError(f"{filename} 内的样本 ID 重复")
            splits[split] = rows
        keys = {split: {text_key(r["text"]) for r in rows} for split, rows in splits.items()}
        for a, b in (("train", "dev"), ("train", "test"), ("dev", "test")):
            if keys[a] & keys[b]:
                raise DataError(f"{a}/{b} 有规范化后相同的文本")
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise DataError(f"数据不完整或格式错误：{directory}。先运行 prepare_data.py。{exc}") from exc
    return manifest, splits


def sample_rows(rows, count, seed):
    """同一种子产生同一子集，各方法共用它；小规模是大规模的前缀。"""
    import numpy as np

    if count > len(rows):
        raise DataError(f"需要 {count} 条训练样本，但只有 {len(rows)} 条")
    rng = np.random.default_rng(seed)
    queues = [rng.permutation([i for i, row in enumerate(rows) if row["label"] == label]).tolist()
              for label in (0, 1)]
    order = []
    while any(queues):
        for queue in queues:
            if queue:
                order.append(queue.pop())
    selected = [rows[index] for index in order[:count]]
    if {row["label"] for row in selected} != {0, 1}:
        raise DataError("训练子集必须包含两个类别")
    return selected


def load_inputs(config, trial=False):
    directory = data_directory()
    manifest, splits = check_dataset(directory)
    count = min(config["train_size"], 50) if trial else config["train_size"]
    train = sample_rows(splits["train"], count, config["seed"])
    evaluation = splits[config["eval_split"]]
    if trial:
        evaluation = evaluation[:64]
    return train, evaluation, manifest, directory

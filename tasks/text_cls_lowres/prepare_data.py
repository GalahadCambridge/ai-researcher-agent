"""联网准备一次 SST-2；固定 dev/test，排除精确重复，并记录来源和文件哈希。"""

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from template.data import DataError, check_dataset, data_directory, file_sha256, text_key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=data_directory())
    parser.add_argument("--dev-size", type=int, default=1000)
    parser.add_argument("--split-seed", type=int, default=42)
    parser.add_argument("--revision", default="main", help="首次准备时解析成明确的 Hub commit")
    parser.add_argument("--check", action="store_true", help="只检查现有数据，不联网")
    args = parser.parse_args()
    directory = args.out.expanduser().resolve()
    if args.check or (directory / "manifest.json").exists():
        manifest, splits = check_dataset(directory)
        if not args.check and (manifest["dev_size"] != args.dev_size or manifest["split_seed"] != args.split_seed):
            raise DataError("现有划分参数不同，请选用新的 --out 目录，保留已有数据。")
        if not args.check and args.revision != "main" and args.revision not in (
            manifest["requested_revision"], manifest["resolved_revision"]
        ):
            raise DataError("现有数据版本与 --revision 不符，请使用新的 --out 目录。")
        print(json.dumps({"status": "checked", "rows": {k: len(v) for k, v in splits.items()},
                          "resolved_revision": manifest["resolved_revision"]}, ensure_ascii=False, indent=2))
        return 0
    if args.dev_size < 2 or args.split_seed < 0:
        parser.error("--dev-size 至少为 2，--split-seed 必须非负")
    if any((directory / name).exists() for name in ("train.jsonl", "dev.jsonl", "test.jsonl")):
        raise DataError("目录中有不完整的旧数据，请选用空目录；程序不会覆盖它们。")

    # 只在显式准备时联网；训练入口不导入 datasets，也不访问 Hub。
    from datasets import load_dataset
    from huggingface_hub import HfApi
    from sklearn.model_selection import train_test_split

    revision = HfApi().dataset_info("stanfordnlp/sst2", revision=args.revision).sha
    if not revision:
        raise DataError("无法确认数据集的具体版本")
    dataset = load_dataset("stanfordnlp/sst2", revision=revision)
    test = [{"id": f"validation-{row['idx']}", "text": row["sentence"], "label": int(row["label"])}
            for row in dataset["validation"]]
    final_keys = {text_key(row["text"]) for row in test}
    kept, conflicts = {}, set()
    removed_overlap = removed_duplicates = 0
    for row in dataset["train"]:
        item = {"id": f"train-{row['idx']}", "text": row["sentence"], "label": int(row["label"])}
        if not item["text"].strip() or item["label"] not in (0, 1):
            raise DataError("原始训练集有空文本或非法标签")
        key = text_key(item["text"])
        if key in final_keys:
            removed_overlap += 1
        elif key in conflicts:
            removed_duplicates += 1
        elif key in kept:
            removed_duplicates += 1
            if kept[key]["label"] != item["label"]:
                del kept[key]
                conflicts.add(key)
        else:
            kept[key] = item
    pool = list(kept.values())
    if len(pool) <= args.dev_size + 1:
        raise DataError("去重后的训练池不足以划分 dev")
    train, dev = train_test_split(pool, test_size=args.dev_size, random_state=args.split_seed,
                                 stratify=[row["label"] for row in pool])
    splits = {"train": train, "dev": dev, "test": test}
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=directory) as temporary:
        staging = Path(temporary)
        for split, rows in splits.items():
            text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
            (staging / f"{split}.jsonl").write_text(text, encoding="utf-8")
        manifest = {
            "schema_version": 1, "dataset": "stanfordnlp/sst2",
            "requested_revision": args.revision, "resolved_revision": revision,
            "prepared_at": datetime.now(UTC).isoformat(),
            "license": "unknown; see https://huggingface.co/datasets/stanfordnlp/sst2",
            "split_seed": args.split_seed, "dev_size": args.dev_size,
            "split_policy": "dev from source train; source validation retained as local final test",
            "deduplication": {"scope": "casefolded whitespace-normalized exact text",
                              "removed_train_test_overlap": removed_overlap,
                              "removed_duplicate_rows": removed_duplicates, "conflicting_texts": len(conflicts)},
            "fingerprints": {name: dataset[name]._fingerprint for name in ("train", "validation")},
            "files": {f"{name}.jsonl": {"sha256": file_sha256(staging / f"{name}.jsonl"), "rows": len(rows)}
                      for name, rows in splits.items()},
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")
        check_dataset(staging)
        for filename in (*manifest["files"], "manifest.json"):
            os.replace(staging / filename, directory / filename)
    print(f"准备完成：{directory}；train={len(train)} dev={len(dev)} test={len(test)}", flush=True)
    print("提示：SST-2 数据卡的许可为 unknown，后续计划预检查应保留警告。", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DataError as exc:
        print(f"data_error: {exc}", file=sys.stderr)
        sys.exit(3)

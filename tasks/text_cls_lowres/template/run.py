"""统一入口：校验配置 -> 固定数据 -> 训练 -> 真实评价 -> 上报指标。"""

import argparse
import importlib
import importlib.metadata
import json
import math
import os
import platform
import sys
import time
import traceback
from pathlib import Path

# 必须在加载 transformers/huggingface_hub 之前设置；下载只由 prepare 脚本完成。
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from air_report import metric  # noqa: E402
from data import DataError, file_sha256, load_inputs  # noqa: E402
from evaluate import classification_metrics  # noqa: E402


class ConfigError(ValueError):
    """配置或输出路径错误。"""


def dump_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def load_config(path):
    import yaml
    from jsonschema import Draft202012Validator

    try:
        text = path.read_text(encoding="utf-8")
        try:
            raw = json.loads(text)  # 执行器生成的 JSON 配置保留 3e-05 等数值类型
        except json.JSONDecodeError:
            raw = yaml.safe_load(text)  # 仍兼容手动运行的普通 YAML
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件不是合法 YAML/JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置文件必须是 YAML/JSON 对象")
    schema = json.loads(Path(__file__).with_name("config_schema.json").read_text(encoding="utf-8"))
    config = {name: spec["default"] for name, spec in schema["properties"].items() if "default" in spec}
    config.update(raw)
    if any(isinstance(value, float) and not math.isfinite(value) for value in config.values()):
        raise ConfigError("配置数值不能包含 NaN 或无穷大")
    return config, Draft202012Validator(schema)


def environment_snapshot():
    packages = {}
    for name in ("torch", "transformers", "peft", "datasets", "huggingface-hub",
                 "scikit-learn", "numpy", "PyYAML", "jsonschema", "safetensors"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    source = Path(__file__).resolve().parent
    return {
        "python": sys.version, "python_executable": sys.executable, "os": platform.platform(),
        "cpu": platform.processor(), "cpu_count": os.cpu_count(), "packages": packages,
        "code_sha256": {path.relative_to(source).as_posix(): file_sha256(path)
                        for path in sorted(source.rglob("*.py"))},
        "schema_sha256": file_sha256(source / "config_schema.json"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--trial", action="store_true")
    parser.add_argument("--seed", type=int, help="覆盖配置中的种子，便于手动进行三次重复")
    parser.add_argument("--eval-split", choices=("dev", "test"), help="正式方案冻结后才使用 test")
    args = parser.parse_args()
    output = args.out.expanduser().resolve()
    output_reserved = False
    started = time.perf_counter()
    try:
        config_path = args.config.expanduser().resolve()
        config, validator = load_config(config_path)
        if args.seed is not None:
            config["seed"] = args.seed
        if args.eval_split is not None:
            config["eval_split"] = args.eval_split
        errors = sorted(validator.iter_errors(config), key=lambda error: str(list(error.path)))
        if errors:
            raise ConfigError("; ".join(error.message for error in errors))
        requested = dict(config)
        if args.trial:
            if config["eval_split"] != "dev":
                raise ConfigError("试运行仅允许 eval_split=dev，不能使用最终测试集调试。")
            config["train_size"] = min(config["train_size"], 50)
            config["epochs"] = 1
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ConfigError(f"输出目录必须为空，已有结果不会覆盖：{output}")
        output.mkdir(parents=True, exist_ok=True)
        output_reserved = True
        dump_json(output / "requested_config.json", requested)
        dump_json(output / "effective_config.json", config)
        dump_json(output / "environment.json", environment_snapshot())
        train, evaluation, manifest, directory = load_inputs(config, trial=args.trial)
        dump_json(output / "train_ids.json", [row["id"] for row in train])
        dump_json(output / "inputs.json", {
            "dataset": manifest["dataset"], "dataset_revision": manifest["resolved_revision"],
            "data_dir": str(directory), "files": manifest["files"],
            "manifest_sha256": file_sha256(directory / "manifest.json"),
            "config_source_sha256": file_sha256(config_path),
            "eval_split": config["eval_split"], "train_rows": len(train), "eval_rows": len(evaluation),
            "trial": args.trial,
        })
        print(f"method={config['method']} seed={config['seed']} train={len(train)} "
              f"eval={config['eval_split']}:{len(evaluation)} trial={args.trial}", flush=True)
        method = importlib.import_module(f"methods.{config['method']}")
        predictions, details = method.run(train, evaluation, config, output)
        scores = classification_metrics([row["label"] for row in evaluation], predictions)
        with (output / "predictions.jsonl").open("w", encoding="utf-8") as stream:
            for row, prediction in zip(evaluation, predictions, strict=True):
                stream.write(json.dumps({"id": row["id"], "gold": row["label"], "prediction": int(prediction)})
                             + "\n")
        result = {
            "method": config["method"], "seed": config["seed"], "trial": args.trial,
            "eval_split": config["eval_split"], "metrics": scores, **details,
            "wall_seconds": time.perf_counter() - started,
        }
        dump_json(output / "result.json", result)
        # standalone 模式只输出协议行；接 LocalExecutor 后由 wrapper 创建 metrics.jsonl。
        for name, value in scores.items():
            metric(name, value, split=config["eval_split"])
        metric("train_seconds", details["train_seconds"], split="train")
        print(f"完成，结果目录：{output}", flush=True)
        return 0
    except Exception as exc:
        if isinstance(exc, DataError):
            code = 3
        elif isinstance(exc, (ConfigError, FileNotFoundError, ModuleNotFoundError)):
            code = 2
        else:
            code = 1
        if output_reserved:
            dump_json(output / "failure.json", {"error_class": type(exc).__name__, "message": str(exc),
                                               "exit_code": code, "wall_seconds": time.perf_counter() - started})
        traceback.print_exc()
        return code


if __name__ == "__main__":
    raise SystemExit(main())

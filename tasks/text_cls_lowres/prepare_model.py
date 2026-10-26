"""联网下载并固定预训练 DistilBERT；不下载已经在 SST-2 上微调的分类器。"""

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from template.data import data_directory, file_sha256

MODEL = "distilbert/distilbert-base-uncased"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=data_directory() / "model_manifest.json")
    parser.add_argument("--revision", default="main")
    args = parser.parse_args()
    output = args.out.expanduser().resolve()
    if output.exists():
        manifest = json.loads(output.read_text(encoding="utf-8"))
        if args.revision != "main" and args.revision not in (
            manifest["requested_revision"], manifest["resolved_revision"]
        ):
            raise ValueError("已有模型版本不同，请用新的 --out 路径。")
        local = Path(manifest["local_dir"])
        for name, checksum in manifest["files"].items():
            if file_sha256(local / name) != checksum:
                raise ValueError(f"本地模型校验不通过：{name}")
        print(f"已有模型校验通过：{local}")
        return 0

    from huggingface_hub import HfApi, snapshot_download

    home = Path(os.environ.get("AIR_HOME", str(Path.home() / "air"))).expanduser()
    cache = Path(os.environ.get("HF_HOME", str(home / "data" / "hf_cache"))) / "hub"
    revision = HfApi().model_info(MODEL, revision=args.revision).sha
    if not revision:
        raise ValueError("无法确认模型的具体版本")
    local = Path(snapshot_download(
        repo_id=MODEL, revision=revision, cache_dir=cache,
        allow_patterns=["*.json", "*.txt", "*.safetensors"],
    )).resolve()
    if not list(local.glob("*.safetensors")):
        raise ValueError("没有下载到 safetensors 权重")
    files = {path.relative_to(local).as_posix(): file_sha256(path)
             for path in sorted(local.rglob("*")) if path.is_file()}
    manifest = {"model_name": MODEL, "requested_revision": args.revision, "resolved_revision": revision,
                "prepared_at": datetime.now(UTC).isoformat(), "local_dir": str(local),
                "license": "apache-2.0", "files": files}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, output)
    print(f"模型准备完成：{local}\n模型记录：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

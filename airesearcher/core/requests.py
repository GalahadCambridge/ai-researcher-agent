"""已处理的写请求（详细设计 1 第 9.1 节：同一 request_id 重复提交返回第一次的结果）。

存在文件里，后台重启后仍然有效（GUI 网络超时后用同一个 request_id 重试时，不会重复执行）：
- 创建项目：${AIR_HOME}/requests.jsonl
- 项目内的操作（暂停/继续/取消/重新打开/回滚、上传模板、人工编辑产物）：<项目>/.state/requests.jsonl

审批和问题的 request_id 记在各自的文件里（approvals/、questions/），运行标注记在 run_labels.jsonl 里，不经过这里。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .fsutil import append_jsonl, atomic_write_text, now, read_jsonl

KEEP = 1000  # 只保留最近这么多条；文件行数超过两倍时压缩一次


class RequestLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._rows: dict[str, dict] | None = None

    def _load(self) -> dict[str, dict]:
        if self._rows is None:
            self._rows = {r["request_id"]: r for r in read_jsonl(self.path) if r.get("request_id")}
        return self._rows

    def get(self, request_id: str) -> dict | None:
        """返回第一次处理时记下的结果（dict），没处理过返回 None。"""
        if not request_id:
            return None
        with self._lock:
            row = self._load().get(request_id)
        return row["result"] if row else None

    def record(self, request_id: str, kind: str, result: dict[str, Any]) -> None:
        if not request_id:
            return
        with self._lock:
            rows = self._load()
            if request_id in rows:
                return
            row = {"request_id": request_id, "kind": kind, "result": result, "ts": now().isoformat()}
            rows[request_id] = row
            self.path.parent.mkdir(parents=True, exist_ok=True)
            append_jsonl(self.path, row)
            if len(rows) > 2 * KEEP:
                keep = list(rows.values())[-KEEP:]
                self._rows = {r["request_id"]: r for r in keep}
                atomic_write_text(self.path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep))

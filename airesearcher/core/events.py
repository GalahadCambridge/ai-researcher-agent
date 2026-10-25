"""事件日志 research_log.jsonl（详细设计 1 第 3.5、4.2 节）。只追加。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from .fsutil import append_jsonl, iter_jsonl_reverse, now, read_jsonl
from .models.common import VersionRef
from .models.event import Event


class Events:
    def __init__(self, root: Path):
        self.path = Path(root) / "research_log.jsonl"
        self._lock = threading.RLock()
        self._seq = max((int(e.get("seq", 0)) for e in read_jsonl(self.path)), default=0)
        self._listeners: list[Callable[[Event], None]] = []

    @property
    def last_seq(self) -> int:
        return self._seq

    def subscribe(self, fn: Callable[[Event], None]) -> None:
        """注册监听（SSE 推送、引擎唤醒用）。监听函数出错不影响写日志。"""
        self._listeners.append(fn)

    def append(
        self,
        type: str,
        summary: str,
        actor: str,
        refs: list[VersionRef] | None = None,
        run_id: str | None = None,
        data: dict | None = None,
    ) -> Event:
        with self._lock:
            self._seq += 1
            ev = Event(
                seq=self._seq, ts=now(), type=type, actor=actor, refs=refs or [], run_id=run_id,
                summary=summary, data=data or {},
            )
            append_jsonl(self.path, ev)
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:  # 监听者的错误不能影响主流程
                pass
        return ev

    def all(self) -> list[Event]:
        return [Event.model_validate(e) for e in read_jsonl(self.path)]

    def query(
        self,
        types: list[str] | None = None,
        run_id: str | None = None,
        artifact_id: str | None = None,
        text: str | None = None,
        after_seq: int = 0,
        limit: int = 200,
        tail: int | None = None,
    ) -> list[Event]:
        """按条件查事件，按 seq 正序返回。

        默认从 after_seq 之后往后取前 limit 条；给了 tail 时改为取符合条件的最后 tail 条
        （从文件末尾往前读，读够就停，不扫全量），此时忽略 limit。
        """
        # 本期没有 SQLite 索引（见详细设计 1 第 3.9 节的说明）：直接顺序扫描文件，几千条以内足够快
        def ok(raw: dict) -> bool:
            if types and not any(_type_matches(raw.get("type", ""), t) for t in types):
                return False
            if run_id and raw.get("run_id") != run_id:
                return False
            if artifact_id and not any(r.get("artifact_id") == artifact_id for r in raw.get("refs", [])):
                return False
            return not (text and text.lower() not in raw.get("summary", "").lower())

        out: list[Event] = []
        if tail is not None:
            for raw in iter_jsonl_reverse(self.path):
                if len(out) >= tail or raw.get("seq", 0) <= after_seq:  # seq 单调递增，再往前都更旧
                    break
                if ok(raw):
                    out.append(Event.model_validate(raw))
            return out[::-1]
        for raw in read_jsonl(self.path):
            if raw.get("seq", 0) <= after_seq or not ok(raw):
                continue
            out.append(Event.model_validate(raw))
            if len(out) >= limit:
                break
        return out


def _type_matches(actual: str, pattern: str) -> bool:
    """"run.*" 匹配所有 run. 开头的类型；否则要求完全相同。"""
    if pattern.endswith("*"):
        return actual.startswith(pattern[:-1])
    return actual == pattern

"""FakeLLM：按 {prompt_id: [响应1, 响应2, ...]} 依次返回，不需要 API Key（详细设计 1 第 10.3 节）。

    llm = fake_gateway(project, {"example/summarize": [{"title": "...", "points": [...]}]})
    ctx.llm = llm      # 或传给 ProjectEngine(project, llm=llm)

响应可以是：
- 字符串，或 dict / list（自动转成 JSON 文本）；
- FakeToolCalls([...])：模拟模型调用工具（用于测试 tool_loop），如
  FakeToolCalls([("write_file", {"path": "src/a.py", "content": "..."})])
bad_json_once=True 时，每个 prompt 的第一次调用先返回一段不合法的 JSON，用来测试网关的修正重试。
"""

from __future__ import annotations

import json
from typing import Any

from airesearcher.llm.gateway import LLMGateway, LLMRequest, RawCompletion, ToolCall


class FakeToolCalls:
    def __init__(self, calls: list[tuple[str, dict | str]], text: str = ""):
        self.calls = calls
        self.text = text


class FakeLLM:
    simulation = True

    def __init__(self, script: dict[str, list[Any]] | None = None, bad_json_once: bool = False,
                 default: Any = None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.bad_json_once = bad_json_once
        self.default = default
        self.calls: list[LLMRequest] = []
        self._bad_done: set[str] = set()
        self._n = 0

    def call(self, req: LLMRequest) -> RawCompletion:
        self.calls.append(req)
        tokens_in = sum(len(str(m.get("content") or "")) for m in req.messages) // 4
        if self.bad_json_once and req.prompt_id not in self._bad_done:
            self._bad_done.add(req.prompt_id)
            return RawCompletion(text="这不是 JSON {", input_tokens=10, output_tokens=5, usd=0.0, model=req.model)
        queue = self.script.get(req.prompt_id)
        if queue:
            resp = queue.pop(0) if len(queue) > 1 else queue[0]  # 最后一个响应重复使用
        elif self.default is not None:
            resp = self.default
        else:
            raise AssertionError(f"FakeLLM 没有为 {req.prompt_id} 准备响应")
        if isinstance(resp, FakeToolCalls):
            calls = []
            for name, args in resp.calls:
                self._n += 1
                calls.append(ToolCall(id=f"call_{self._n}", name=name,
                                      arguments=args if isinstance(args, str) else json.dumps(args)))
            assistant = {"role": "assistant", "content": resp.text or None,
                         "tool_calls": [{"id": c.id, "type": "function",
                                         "function": {"name": c.name, "arguments": c.arguments}} for c in calls]}
            return RawCompletion(text=resp.text, input_tokens=tokens_in, output_tokens=20, usd=0.0, model=req.model,
                                 tool_calls=calls, finish_reason="tool_calls", assistant_message=assistant)
        text = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)
        return RawCompletion(text=text, input_tokens=tokens_in, output_tokens=len(text) // 4, usd=0.0,
                             model=req.model, finish_reason="stop",
                             assistant_message={"role": "assistant", "content": text})


def fake_gateway(project: Any, script: dict[str, list[Any]] | None = None, **kw: Any) -> LLMGateway:
    return LLMGateway(project, backend=FakeLLM(script, **kw), backoff_base=0.0)

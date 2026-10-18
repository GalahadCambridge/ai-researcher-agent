"""开发演示：可选真实检索或快照回放，模型回复与筛选评分均为模拟。"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from pathlib import Path

from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.project import Project
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import describe
from airesearcher.llm.gateway import LLMGateway
from airesearcher.testing.fake_literature import FakeLiterature
from airesearcher.testing.fake_llm import FakeLLM

from .stage import IdeaStage


def demo_responses() -> dict:
    return {
        "idea/parse_input": [{
            "research_object": "低资源文本分类中的参数高效微调",
            "task": "tasks/smoke", "target_variables": ["accuracy", "训练资源"],
            "data_or_workload": [], "constraints": {},
            "keywords": ["LoRA", "low-resource", "text classification"],
            "missing_info": ["数据集、样本规模和预算待确认；smoke 仅用于调试，尚非真实分类任务"],
        }],
        "idea/plan_queries": [{"subquestions": [
            {"question": "参数高效微调方法", "purpose": "寻找方法来源", "queries": ["LoRA fine tuning"]},
            {"question": "基线与比较", "purpose": "寻找对照方法", "queries": ["BERT pretraining"]},
            {"question": "低资源设置", "purpose": "寻找实验设置", "queries": ["low resource text classification"]},
        ]}],
    }


class DemoLLM(FakeLLM):
    """并发安全的演示后端；不进行论文语义分析，不输出模拟研究结论。"""

    def __init__(self, script=None):
        super().__init__(script if script is not None else demo_responses())
        self._demo_lock = threading.Lock()
        self._dynamic_screen = "idea/screen" not in self.script
        self._fixed_prompts = set(self.script)

    def call(self, req):
        with self._demo_lock:
            text = "\n".join(str(m.get("content") or "") for m in req.messages)
            if req.prompt_id == "idea/screen" and self._dynamic_screen:
                blocks = re.findall(r'<untrusted_document source="[^"]*">\s*(.*?)\s*</untrusted_document>', text, re.S)
                self.script[req.prompt_id] = [{"papers": [
                    {"paper_id": json.loads(block)["paper_id"], "score": 3,
                     "reason": "模拟固定评分：仅用于流程演示，尚未经真实模型评估相关性。"}
                    for block in blocks
                ]}]
            elif req.prompt_id in {"idea/methods_reader", "idea/feasibility_reader", "idea/single_reader"} and (
                    req.prompt_id not in self._fixed_prompts):
                fields = json.loads(re.search(r"需要提取的字段\(JSON\)：(\[[^\n]*\])", text).group(1))
                based_on = re.search(r"based_on：([^\n]+)", text).group(1).strip()
                self.script[req.prompt_id] = [{"items": [
                    {"field": field, "content": "未提及（模拟阅读占位，尚未进行语义提取）",
                     "kind": "agent_inference",
                     "source_loc": "abstract" if based_on == "abstract_only" else "全文片段（模拟）",
                     "quote": None, "confidence": "low"} for field in fields
                ], "relevance_to_question": "模拟占位；相关性和论文内容需要真实模型及人工核验。"}]
            elif req.prompt_id == "idea/gap_analyst" and req.prompt_id not in self._fixed_prompts:
                self.script[req.prompt_id] = [{"gaps": []}]
            return super().call(req)


def demo_gateway(project, script=None):
    return LLMGateway(project, backend=DemoLLM(script), backoff_base=0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live-retrieval", action="store_true", help="真实检索，仍使用假模型，不需要模型 Key")
    mode.add_argument("--replay", type=Path, help="从指定目录回放检索快照，不联网")
    parser.add_argument("--timeout", type=float, default=180, help="演示最多运行秒数")
    parser.add_argument("--fetch-fulltext", action="store_true", help="明确启用入选 arXiv 论文的真实 PDF 下载")
    parser.add_argument("--read-papers", type=int, default=8, help="本次选择的精读篇数")
    parser.add_argument("--reading-mode", choices=["multi", "single"], default="multi", help="多角色或单角色阅读")
    args = parser.parse_args()
    if args.workspace.exists() and any(args.workspace.iterdir()):
        parser.error("请使用空目录，演示不会覆盖已有项目")
    project = Project.create(
        goal="比较 LoRA 和全量微调在低资源文本分类中的效果",
        task="tasks/smoke", root=args.workspace, register=False, reading_mode=args.reading_mode,
        dev={"stage_impl": {"idea": "real"}}, limits={"read_papers": args.read_papers}, literature={"year_from": 2018,
                    "replay_snapshots": str(args.replay.resolve()) if args.replay else None},
    )
    source = None if args.live_retrieval or args.replay else FakeLiterature()
    engine = ProjectEngine(project, llm=demo_gateway(project),
                           stages={"idea": IdeaStage(source, fulltext_download=args.fetch_fulltext)}, retry_delay=0)
    print("模型回复为预置数据；检索模式：" +
          ("真实线上检索" if args.live_retrieval else "本地快照回放" if args.replay else "本地 fixture"))
    print("相关性评分固定为 3 分，仅演示筛选流程，不代表论文实际相关性。")
    print("PDF 获取：" + ("真实下载已启用" if args.fetch_fulltext else "未启用下载，保留摘要"))
    print("阅读卡为明确标注的模拟占位；不进行语义分析，不生成研究差距结论。")
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        tick = engine.tick()
        if tick.result is not None:
            print(describe(tick.result))
        if engine.state in (S.Paused, S.Failed):
            break
        if tick.seconds:
            time.sleep(min(tick.seconds, max(0, deadline - time.monotonic())))
    if engine.state != S.Paused or engine.ck.scratch.get("idea", {}).get("phase") != "merge":
        raise RuntimeError(f"演示未完成阅读卡准备：{engine.state} {engine.ck.reason}")
    print(json.dumps(engine.ck.scratch["idea"], ensure_ascii=False, indent=2))
    print(f"产物目录：{project.root / 'idea'}")


if __name__ == "__main__":
    main()

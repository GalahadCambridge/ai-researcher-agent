"""Demo with live retrieval or snapshot replay; model responses and screening scores are simulated."""

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
from airesearcher.stages.idea.stage import IdeaStage
from airesearcher.testing.fake_literature import FakeLiterature
from airesearcher.testing.fake_llm import FakeLLM


def demo_responses() -> dict:
    return {
        "idea/parse_input": [{
            "research_object": "Parameter-efficient fine-tuning for low-resource text classification",
            "task": "tasks/smoke", "target_variables": ["accuracy", "training resources"],
            "data_or_workload": [], "constraints": {},
            "keywords": ["LoRA", "low-resource", "text classification"],
            "missing_info": [(
                "Dataset, sample size, and budget require confirmation; smoke is a debugging "
                "task, not a real classification task"
            )],
        }],
        "idea/plan_queries": [{"subquestions": [
            {"question": (
                "Parameter-efficient fine-tuning methods"
            ), "purpose": "Find original method papers", "queries": ["LoRA fine tuning"]},
            {"question": (
                "Baselines and comparisons"
            ), "purpose": "Find baseline methods", "queries": ["BERT pretraining"]},
            {"question": "Low-resource settings", "purpose": "Find experimental settings", "queries": [(
                "low resource text classification"
            )]},
        ]}],
    }


class DemoLLM(FakeLLM):
    """Thread-safe demo backend; performs no semantic paper analysis and produces no research conclusions."""

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
                     "reason": (
                         "Fixed simulated score for workflow demonstration; relevance has not been "
                         "assessed by a real model."
                     )}
                    for block in blocks
                ]}]
            elif req.prompt_id in {"idea/methods_reader", "idea/feasibility_reader", "idea/single_reader"} and (
                    req.prompt_id not in self._fixed_prompts):
                fields = json.loads(re.search(r"Required fields \(JSON\): (\[[^\n]*\])", text).group(1))
                based_on = re.search(r"based_on: ([^\n]+)", text).group(1).strip()
                self.script[req.prompt_id] = [{"items": [
                    {"field": field, "content": (
                        "Not analyzed (simulated reading placeholder; no semantic extraction performed)"
                    ),
                     "kind": "agent_inference",
                     "source_loc": "abstract" if based_on == "abstract_only" else "full-text excerpt (simulated)",
                     "quote": None, "confidence": "low"} for field in fields
                ], "relevance_to_question": (
                    "Simulated placeholder; relevance and paper content require a real model and "
                    "human verification."
                )}]
            elif req.prompt_id == "idea/gap_analyst" and req.prompt_id not in self._fixed_prompts:
                self.script[req.prompt_id] = [{"gaps": []}]
            elif req.prompt_id == "idea/coordinator_merge" and req.prompt_id not in self._fixed_prompts:
                block = re.search(
                    r'<untrusted_document source="merge_input">\s*(.*?)\s*</untrusted_document>', text, re.S
                ).group(1)
                material = json.loads(block)
                points = [{"text": "Simulated synthesis placeholder; no semantic literature analysis performed.",
                           "evidence": [card["items"][0]["ref"]], "to_verify": True}
                          for card in material["cards"]]
                self.script[req.prompt_id] = [{
                    "themes": [{"theme": "Simulated reading coverage", "points": points}] if points else [],
                    "feasibility_notes": [], "gaps": material["gap_candidates"], "conflicts": [],
                }]
            elif req.prompt_id == "idea/adjudicate_conflict" and req.prompt_id not in self._fixed_prompts:
                self.script[req.prompt_id] = [{
                    "resolution": "needs_human", "accepted_evidence": [], "quotes": [],
                    "explanation": "Simulated review placeholder; no semantic conflict resolution performed.",
                }]
            elif req.prompt_id in {"idea/refine", "idea/score", "idea/gap_table", "idea/revise"} and (
                    req.prompt_id not in self._fixed_prompts):
                block = re.search(
                    r'<untrusted_document source="selection_context">\s*(.*?)\s*</untrusted_document>', text, re.S
                ).group(1)
                context = json.loads(block)
                if req.prompt_id in {"idea/refine", "idea/revise"}:
                    value = simulated_selection(context)
                    if req.prompt_id == "idea/revise":
                        value.update(needs_more_literature=False, new_queries=[])
                elif req.prompt_id == "idea/score":
                    value = {name: {"score": 3, "rationale": "Simulated score; research quality is unverified.",
                                    "evidence": [], "to_verify": True}
                             for name in ("novelty", "feasibility", "impact", "testability", "risk")}
                else:
                    value = {"rows": [{"paper_id": pid, "cells": {
                        name: {"text": "To verify", "evidence": [], "to_verify": True}
                        for name in ("task_data", "method", "setting", "compute", "evaluation", "difference")
                    }} for pid in context["idea"]["citations"]]}
                self.script[req.prompt_id] = [value]
            return super().call(req)




def simulated_selection(context):
    """Build an explicitly simulated draft from supplied task metadata."""
    task = context["task"]
    primary = next((m for m in task["metrics"] if m["required"]), task["metrics"][0])
    methods = task["methods_available"] or ["baseline", "main"]
    targets = context.get("goal_scope", {}).get("comparison_targets", [])
    if targets:
        methods = targets
    question = ("Simulated comparison of " + " and ".join(targets) + "; no research conclusion is established."
                if targets else "Simulated workflow question; research validity has not been established.")
    hypothesis = ("The proposed comparison of " + " and ".join(targets) + " improves the task metric (unverified)."
                  if targets else "Proposed method improves the task metric (unverified).")
    return {
        "idea": {
            "idea_id": "idea-demo", "title": "Simulated LoRA workflow draft",
            "research_question": question,
            "motivation": "Exercise the literature-stage handoff using simulated model output.",
            "hypotheses": [{"id": "H1", "statement": hypothesis,
                            "falsified_if": "The mean improvement across three seeds is non-positive."}],
            "contributions": ["Simulated comparison proposal requiring real literature and task verification."],
            "task": {}, "data": [],
            "baselines": [{"id": "B1", "name": methods[0], "why": "Task-provided baseline.", "citations": []}],
            "methods": [{"id": "M1", "name": methods[-1], "description": "Task-provided method.", "citations": []}],
            "metrics": [{**{k: m[k] for k in ("name", "direction")}, "primary": m["name"] == primary["name"]}
                        for m in task["metrics"]],
            "budget": {}, "stopping_conditions": ["Stop at the budget limit.", "Stop after the approved comparisons."],
            "expected_difficulties": ["Simulation cannot establish research quality."],
            "open_questions": ["Simulated output: verify all research claims before approval."],
            "citations": [p["paper_id"] for p in context["literature"]["papers"]],
            "scores_ref": "idea/scores.json", "gap_table_ref": "idea/gap_table.md",
        },
        "task_fit": "unknown", "task_fit_reason": "The simulation does not verify LoRA training readiness.",
    }


def demo_gateway(project, script=None):
    return LLMGateway(project, backend=DemoLLM(script), backoff_base=0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live-retrieval", action="store_true", help=(
        "Use live retrieval with a simulated model; no model API key is required"
    ))
    mode.add_argument("--replay", type=Path, help="Replay retrieval snapshots from a directory without network access")
    parser.add_argument("--timeout", type=float, default=180, help="Maximum demo runtime in seconds")
    parser.add_argument("--fetch-fulltext", action="store_true", help=(
        "Enable actual PDF downloads for selected arXiv papers"
    ))
    parser.add_argument("--read-papers", type=int, default=8, help="Number of papers selected for reading")
    parser.add_argument("--reading-mode", choices=["multi", "single"], default="multi", help=(
        "Use multi-role or single-role reading"
    ))
    parser.add_argument("--stop-after", choices=["merge", "gap_table", "submit"], default="submit",
                        help="Stop at a requested development checkpoint")
    args = parser.parse_args()
    if args.workspace.exists() and any(args.workspace.iterdir()):
        parser.error("Use an empty directory; the demo will not overwrite an existing project")
    project = Project.create(
        goal="Compare LoRA and full fine-tuning for low-resource text classification",
        task="tasks/smoke", root=args.workspace, register=False, reading_mode=args.reading_mode,
        dev={"stage_impl": {"idea": "real"}}, limits={"read_papers": args.read_papers}, literature={"year_from": 2018,
                    "replay_snapshots": str(args.replay.resolve()) if args.replay else None},
    )
    source = None if args.live_retrieval or args.replay else FakeLiterature()
    engine = ProjectEngine(project, llm=demo_gateway(project),
                           stages={"idea": IdeaStage(source, fulltext_download=args.fetch_fulltext,
                                                     stop_after=args.stop_after)}, retry_delay=0)
    print("Model responses are preset; retrieval mode: " +
          ("live retrieval" if args.live_retrieval else "local snapshot replay" if args.replay else "local fixtures"))
    print("Relevance scores are fixed at 3 for workflow demonstration and do not represent actual paper relevance.")
    print("PDF retrieval: " + ("actual downloads enabled" if args.fetch_fulltext else (
        "downloads disabled; abstracts retained"
    )))
    print(
        "Reading cards are explicitly labeled simulated placeholders; no semantic "
        "analysis or research-gap conclusions are produced."
    )
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        tick = engine.tick()
        if tick.result is not None:
            print(describe(tick.result))
        if engine.state in (S.Paused, S.Failed, S.IdeaPending):
            break
        if tick.seconds:
            time.sleep(min(tick.seconds, max(0, deadline - time.monotonic())))
    expected_phase = {"merge": "refine", "gap_table": "write", "submit": "submit"}[args.stop_after]
    expected_state = S.IdeaPending if args.stop_after == "submit" else S.Paused
    if engine.state != expected_state or engine.ck.scratch.get("idea", {}).get("phase") != expected_phase:
        raise RuntimeError(f"The demo did not reach the requested checkpoint: {engine.state} {engine.ck.reason}")
    print(json.dumps(engine.ck.scratch["idea"], ensure_ascii=False, indent=2))
    print(f"Artifact directory: {project.root / 'idea'}")


if __name__ == "__main__":
    main()

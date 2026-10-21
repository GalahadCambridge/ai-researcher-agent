"""Run the real WritingStage with scripted sample prose and actual gateway calls."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path

from airesearcher.core.models.aggregate import Aggregate
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.workspace import repo_root
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import Error, Stop
from airesearcher.llm.gateway import LLMGateway
from airesearcher.stages.writing.claims import PROMPT_ID as CLAIM_PROMPT
from airesearcher.stages.writing.figures import default_spec
from airesearcher.stages.writing.stage import FIGURE_PROMPT
from airesearcher.stages.writing.writer import PROMPT_ID
from airesearcher.testing.fake_llm import FakeLLM
from airesearcher.testing.sample import copy_sample_project


def context_of(request):
    text = request.messages[1]["content"]
    return json.loads(re.search(r"<untrusted_document[^>]*>\s*(.*?)\s*</untrusted_document>", text, re.S)[1])


class SampleWritingLLM(FakeLLM):
    """Explicit synthetic answers for the public smoke sample, never a real model."""

    def call(self, request):
        context = context_of(request)
        if request.prompt_id == FIGURE_PROMPT:
            aggregate = Aggregate.model_validate(context["aggregate"])
            # Example author declares the synthetic smoke score dimensionless, not production inference.
            spec = default_spec(aggregate, fig_id=context["fig_id"], ylabel="Score (dimensionless)")
            # This English demo chooses an English presentation title; source data stays intact.
            prefix = "Exploratory: " if aggregate.eval_condition == "explore" else ""
            spec.title = prefix + "Comparison " + aggregate.aggregate_id
            self.default = spec.model_dump()
        elif request.prompt_id == CLAIM_PROMPT:
            claims = []
            for i, source in enumerate(context["aggregates"]):
                aggregate = source["data"]
                for contrast in aggregate["contrasts"]:
                    selector = {"a": contrast["a"], "b": contrast["b"]}
                    claims.append(
                        {
                            "section": "results",
                            "type": "comparative",
                            "statement_kind": "observation",
                            "hedge": "none",
                            "importance": "major",
                            "hypothesis": "H1" if i == 0 else None,
                            "text_template": f"Comparison {aggregate['aggregate_id']}: the observed mean difference "
                            "is [[D]], with p-value [[P]] and [[N]] seeds in the first group.",
                            "values": [
                                {
                                    "name": name,
                                    "aggregate_id": aggregate["aggregate_id"],
                                    "group": {},
                                    "metric": contrast["metric"],
                                    "stat": stat,
                                    "contrast": selector,
                                }
                                for name, stat in (("D", "diff_mean"), ("P", "p_value"))
                            ]
                            + [
                                {
                                    "name": "N",
                                    "aggregate_id": aggregate["aggregate_id"],
                                    "group": contrast["a"],
                                    "metric": contrast["metric"],
                                    "stat": "n",
                                }
                            ],
                            "comparison": {
                                "aggregate_id": aggregate["aggregate_id"],
                                **selector,
                                "metric": contrast["metric"],
                                "asserted": "a>b" if contrast["diff_mean"] > 0 else "a<b",
                            },
                            "evidence": [
                                {"type": "aggregate", "ref": aggregate["aggregate_id"]},
                                {"type": "figure", "ref": "fig_" + aggregate["aggregate_id"].lower()},
                            ],
                        }
                    )
            self.default = {"claims": claims}
        elif request.prompt_id == PROMPT_ID:
            section = context["section"]
            content = {
                "abstract": "We compare baseline, main and ablation conditions on a synthetic smoke task. "
                "The supplied observations motivate cautious follow-up within the recorded scope.",
                "introduction": r"\section{Introduction}" + "\n"
                r"The approved smoke comparison illustrates the workflow. Pretraining and adaptation "
                r"motivate the background\cite{devlin2019bert,hu2022lora}; these runs do not reproduce those methods.",
                "related_work": r"\section{Related Work}" + "\n"
                r"BERT describes pretraining\cite{devlin2019bert}, while LoRA studies low-rank adaptation"
                r"\cite{hu2022lora}. Abstract-only access limits reconstruction of experimental details.",
                "method": r"\section{Method}" + "\n"
                "The approved plan compares baseline, main and ablation conditions. "
                "The frozen task configuration identifies a smoke test; "
                "unavailable implementation details remain unspecified.",
                "setup": r"\section{Experimental Setup}" + "\n"
                "Only runs supporting the selected comparisons enter the frozen environment summary. ",
                "results": r"\section{Results}" + "\n",
                "discussion": r"\section{Discussion}" + "\n"
                "The observed differences may motivate further evaluation; "
                "they do not establish a general causal effect.\n"
                r"\subsection{Limitations}" + "\n"
                "This synthetic smoke task and its limited seeds cannot establish performance on real tasks. "
                "Local offline citation checks do not verify scientific claims.",
                "conclusion": r"\section{Conclusion}" + "\n"
                "The recorded comparisons illustrate a reproducible pipeline within the approved scope. "
                "No generalization beyond these observations is established.",
            }
            if section in {"method", "setup"} and not context.get("evidence_check"):
                runtime = context.get("runtime", {})
                versions = sorted({r["environment"].get("python", "unknown") for r in runtime.get("runs", [])})
                content[section] += " The frozen supporting runs record Python " + ", ".join(versions) + "."
            if section == "results":
                for source in context.get("aggregates", []):
                    aggregate = source["data"]
                    content[section] += f"\nComparison {aggregate['aggregate_id']} ({aggregate['eval_condition']}): "
                    content[section] += (
                        "; ".join(
                            f"{row['group']['method']} mean {row['mean']:.6f}, n={row['n']}"
                            for row in aggregate["rows"]
                        )
                        + ".\n"
                    )
                for fid, path in context["figures"].items():
                    content[section] += (
                        r"\begin{figure}[ht]\centering" + "\n"
                        rf"\includegraphics[width=4in]{{{path}}}" + "\n"
                        r"\caption{Recorded comparison; error bars show confidence intervals.}"
                        + rf"\label{{fig:{fid}}}\end{{figure}}"
                        + "\n"
                    )
            if context.get("evidence_check"):
                content[section] += "\n" + "\n".join(context["claim_markers"]) + "\n"
            if context.get("user_writing_guidance"):
                content[section] += (
                    "\n% Feedback applied: "
                    + ", ".join(g["approval_id"] for g in context["user_writing_guidance"])
                    + "\n"
                )
            self.default = {
                "section": section,
                "latex": content[section].rstrip() if context.get("evidence_check") else content[section],
            }
        return super().call(request)


def prepare_sample(destination: Path, *, compile_enabled: bool = True, evidence_check: bool = False):
    """Copy public inputs and initialize the documented Writing entry checkpoint."""
    project = copy_sample_project(destination)
    (project.root / "paper").rename(project.root / "fixture-paper")
    (project.root / "artifacts/figures").rename(project.root / "fixture-figures")
    config = project.config.model_copy(deep=True)
    config.title = "Sample Project: Control-Arm Writing Stage"
    config.evidence_check = evidence_check
    config.dev.stage_impl["writing"] = "real"
    config.dev.compile = compile_enabled
    project.save_config(config, "Disposable zero-cost WritingStage demonstration")
    llm = LLMGateway(project, backend=SampleWritingLLM(), backoff_base=0)
    engine = ProjectEngine(project, llm=llm, retry_delay=0)
    engine.ck.state = S.Writing
    engine.ck.pending_approval = None
    engine.ck.pending_question = None
    engine.ck.answer = None
    engine.ck.scratch = {}
    engine.ck.feedback_approval_ids = []
    engine.ck.approved = {kind: project.approvals.latest_approved(kind).target for kind in ("idea", "plan")}
    engine._save()  # Harness-only initialization, same entry mechanism as dev run-stage.
    return project, engine


def run(engine):
    for _ in range(150):
        tick = engine.tick()
        if isinstance(tick.result, Error | Stop):
            raise RuntimeError(str(tick.result))
        if engine.state == S.ManuscriptPending:
            return engine.project.approvals.get(engine.ck.pending_approval)
    raise RuntimeError(f"WritingStage did not finish: {engine.state}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--no-compile", action="store_true")
    parser.add_argument("--evidence-check", action="store_true")
    args = parser.parse_args()
    target = args.workspace.resolve()
    if target.exists() or target == repo_root() or repo_root() in target.parents:
        parser.error("choose a fresh workspace outside the repository")
    if not args.no_compile and not shutil.which("latexmk"):
        parser.error("latexmk unavailable; use --no-compile for honest source-only development")
    os.environ["AIR_REAL_LLM"] = "0"
    project, engine = prepare_sample(target, compile_enabled=not args.no_compile, evidence_check=args.evidence_check)
    approval = run(engine)
    print(
        json.dumps(
            {
                "workspace": str(project.root),
                "state": engine.state.value,
                "calls": len(engine.llm.backend.calls),
                "approval": approval.model_dump(mode="json"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

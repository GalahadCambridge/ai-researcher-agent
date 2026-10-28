"""【论文】阶段的假实现（Writing）。不调用模型。

按详细设计 4 的格式生成：
- artifacts/figures/fig_<cid>/   spec.json、data.csv、figure.json、fig_<cid>.svg（简单柱状图，不依赖 matplotlib）
- paper/evidence_bundle.json、paper/sections/*.tex、paper/main.tex
- paper/air_values.tex           \\airval 数字宏（6.3），数字全部来自汇总结果
- paper/claims.jsonl             Claim（论断-证据矩阵）
- paper/references.bib           services.literature.bibtex_for()
- paper/build/                   有 latexmk 且 dev.compile 为真时真实编译；否则写占位 PDF，并在编译报告中说明
- paper/review/review_v<N>.json  services.evidence.check() 的 CheckReport（确定性桩）
登记 paper/（排除 build/**、review/**）与 PDF 为两个产物，然后提交 manuscript 审批。
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from airesearcher.core.fsutil import atomic_write_text, dumps, now, sha256_bytes
from airesearcher.core.models.aggregate import Aggregate
from airesearcher.core.models.claim import Claim, CompileReport, EvidenceLink, ValueRef
from airesearcher.core.models.claim import Comparison as ClaimComparison
from airesearcher.core.models.common import VersionRef
from airesearcher.core.models.figure import Figure, FigureSpec
from airesearcher.engine.stage import Continue, NeedsApproval, StageContext, StepResult
from airesearcher.services import evidence, literature

from .common import feedback_note, new_feedback

PAPER_EXCLUDE = ["build/**", "review/**", ".template_check_default.json"]
PCT = {"scale": 100, "decimals": 1, "suffix": ""}
PVAL = {"scale": 1, "decimals": 4, "suffix": ""}  # 与真实 Writer（writing/claims.py）的 p_value 格式一致
COUNT = {"scale": 1, "decimals": 0, "suffix": ""}  # 样本量 n


# ---------------------------------------------------------------- 小工具
def _tex_escape(s: str) -> str:
    return re.sub(r"([_%&#$])", r"\\\1", str(s))


def _gkey(group: dict) -> str:
    return ".".join(str(v) for v in group.values())


def svg_bar_chart(rows: list[dict], title: str, ylabel: str) -> str:
    """不依赖 matplotlib 的简单柱状图：rows = [{label, mean, lo, hi}]。"""
    w, h, pad = 360, 240, 40
    vmax = max([r["hi"] if r["hi"] is not None else r["mean"] for r in rows] + [1e-9])
    bw = (w - 2 * pad) / max(1, len(rows)) * 0.6
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" font-family="sans-serif" '
             f'font-size="11"><text x="{w / 2}" y="16" text-anchor="middle">{title}</text>',
             f'<text x="12" y="{h / 2}" transform="rotate(-90 12 {h / 2})" text-anchor="middle">{ylabel}</text>',
             f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" stroke="black"/>']
    for i, r in enumerate(rows):
        x = pad + (i + 0.2) * (w - 2 * pad) / len(rows)
        y = h - pad - (h - 2 * pad) * r["mean"] / vmax
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{h - pad - y:.1f}" fill="#4C72B0"/>')
        if r["lo"] is not None:
            y1 = h - pad - (h - 2 * pad) * r["lo"] / vmax
            y2 = h - pad - (h - 2 * pad) * r["hi"] / vmax
            cx = x + bw / 2
            parts.append(f'<line x1="{cx:.1f}" y1="{y1:.1f}" x2="{cx:.1f}" y2="{y2:.1f}" stroke="black"/>')
        parts.append(f'<text x="{x + bw / 2:.1f}" y="{h - pad + 14}" text-anchor="middle">{r["label"]}</text>')
    return "\n".join(parts) + "\n</svg>\n"


def minimal_pdf(lines: list[str]) -> bytes:
    """生成一页只含 ASCII 文本的合法 PDF（没有 latexmk 时的占位文件）。"""
    def esc(s: str) -> str:
        s = s.encode("ascii", "replace").decode()
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    text = "BT /F1 11 Tf 50 780 Td 14 TL " + " ".join(f"({esc(line)}) Tj T*" for line in lines[:50]) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(text)} >>\nstream\n{text}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{off:010d} 00000 n \n" for off in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def compile_paper(paper: Path, enabled: bool = True) -> CompileReport:
    """编译 paper/main.tex（7.4）。没有 latexmk 或 enabled=False 时写占位 PDF，并在报告中说明。"""
    build = paper / "build"
    build.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    latexmk = shutil.which("latexmk")
    if enabled and latexmk:
        env = {**os.environ, "openin_any": "p", "openout_any": "p"}
        try:
            r = subprocess.run([latexmk, "-pdf", "-interaction=nonstopmode", "-halt-on-error",
                                "-no-shell-escape", "-outdir=build", "main.tex"], cwd=paper, env=env,
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=180)
            ok = r.returncode == 0 and (build / "main.pdf").exists()
            main_log = build / "main.log"
            log = main_log.read_text(encoding="utf-8", errors="replace") if main_log.exists() else ""
            errors = [{"file": "main.tex", "line": None, "message": ln} for ln in log.splitlines()
                      if ln.startswith("!")][:20]
            undefined = sorted(set(re.findall(r"Citation `([^']+)' .*undefined", log)))
            pages = re.search(r"Output written on .*\((\d+) page", log)
            return CompileReport(ok=ok, engine="latexmk -pdf", entry="main.tex", errors=errors,
                                 undefined_citations=undefined, log_path="paper/build/main.log",
                                 pages=int(pages.group(1)) if pages else None,
                                 seconds=round(time.monotonic() - t0, 2))
        except subprocess.TimeoutExpired:
            pass
    reason = "latexmk 不可用" if not latexmk else "dev.compile=false 或编译超时"
    text = ["AI Researcher Agent - placeholder PDF", f"({reason}; LaTeX sources are in paper/)", ""]
    for name in ("abstract", "results", "conclusion"):
        p = paper / "sections" / f"{name}.tex"
        if p.exists():
            body = [ln for ln in p.read_text(encoding="utf-8").splitlines() if not ln.startswith("%")]
            text += [f"[{name}]", *body[:12], ""]
    (build / "main.pdf").write_bytes(minimal_pdf(text))
    (build / "main.md").write_text("\n".join(text) + "\n", encoding="utf-8")
    return CompileReport(ok=False, engine="placeholder", entry="main.tex",
                         errors=[{"file": "main.tex", "line": None, "message": f"{reason}：生成了占位 PDF"}],
                         log_path="paper/build/main.md", seconds=round(time.monotonic() - t0, 2))


# ---------------------------------------------------------------- 阶段
class FakeWritingStage:
    name = "writing"

    def step(self, ctx: StageContext) -> StepResult:
        s = ctx.scratch.setdefault("writing", {})
        fb = new_feedback(ctx, s, "manuscript")
        if fb is not None:
            s.setdefault("notes", []).append(feedback_note(fb))
            s["phase"] = "figures"
        basis = self._basis(ctx)
        if s.get("basis") != basis:  # 汇总结果或获批日志变了（含手稿获批后的 stale）→ 重新生成
            s["basis"] = basis
            s["phase"] = "figures"
        latest_paper = ctx.archive.latest("paper")
        if s.get("phase") == "submit" and latest_paper and s.get("paper_ref", {}).get("sha256") != latest_paper.sha256:
            s["phase"] = "compile"  # 用户手工改过 paper/：重新编译、核验后再提交
        phase = s.get("phase", "figures")

        if phase == "figures":
            ctx.progress("论文（假实现）：画图与证据包", 1, 4, "steps")
            self._bundle_and_figures(ctx, s)
            s["phase"] = "write"
            return Continue("证据包与图表完成")
        if phase == "write":
            ctx.progress("论文（假实现）：撰写章节与论断", 2, 4, "steps")
            self._write(ctx, s)
            s["phase"] = "compile"
            return Continue("章节已写好")
        if phase == "compile":
            ctx.progress("论文（假实现）：编译与核验", 3, 4, "steps")
            self._compile_register_review(ctx, s)
            s["phase"] = "submit"
            return Continue("编译与核验完成")

        ctx.progress("论文（假实现）：等待审批", 4, 4, "steps")
        r = s["refs"]
        report = json.loads((ctx.root / r["review_path"]).read_text(encoding="utf-8"))
        counts = report["counts"]
        ok = json.loads((ctx.root / "paper/build/compile_report.json").read_text(encoding="utf-8"))["ok"]
        paper_ref = VersionRef(**s["paper_ref"])
        return NeedsApproval(
            target=paper_ref, kind="manuscript",
            summary=f"（假实现）论文草稿 v{paper_ref.version}：{len(report['claims'])} 条论断"
                    f"（{counts.get('supported', 0)} supported），{counts.get('blocker', 0)} 个 blocker；"
                    + ("编译成功" if ok else "未真实编译（占位 PDF）"),
            impact="批准后项目标记为完成；批准不等于授权投稿",
            extra_refs=[VersionRef(**r[k]) for k in ("review", "compile", "claims", "pdf")],
        )

    # ------------------------------------------------------------ 依据
    def _basis(self, ctx: StageContext) -> str:
        refs = [r.sha256 for v in ctx.archive.list_latest("aggregate") if (r := v.ref)]
        logs = [a.target.sha256 for a in ctx.approvals.list("log", "approved")]
        return sha256_bytes(json.dumps([sorted(refs), logs]).encode())[:16]

    def _aggregates(self, ctx: StageContext) -> list[Aggregate]:
        out = []
        for v in ctx.archive.list_latest("aggregate"):
            if v.path.endswith(".json") and not v.path.endswith("/all.json"):
                out.append(Aggregate.model_validate_json((ctx.root / v.path).read_text(encoding="utf-8")))
        return out

    # ------------------------------------------------------------ 1 证据包与图表
    def _bundle_and_figures(self, ctx: StageContext, s: dict) -> None:
        aggs = self._aggregates(ctx)
        logs = {}
        for a in ctx.approvals.list("log", "approved"):  # 同一日志取最新获批版本
            logs[a.target.artifact_id] = a.target.model_dump(mode="json")
        papers = literature.load_papers(ctx.root)
        bundle = {
            "idea_ref": ctx.approved.get("idea").model_dump(mode="json") if ctx.approved.get("idea") else None,
            "plan_ref": ctx.approved.get("plan").model_dump(mode="json") if ctx.approved.get("plan") else None,
            "aggregates": [{"aggregate_id": a.aggregate_id,
                            "ref": ctx.archive.latest(f"artifacts/aggregates/{a.aggregate_id}.json")
                            .model_dump(mode="json")} for a in aggs],
            "logs": list(logs.values()),
            "papers": [{"paper_id": pid, "status": literature.verify_citation(ctx.root, pid).status}
                       for pid in papers],
        }
        atomic_write_text(ctx.root / "paper" / "evidence_bundle.json", dumps(bundle) + "\n")
        fig_refs = []
        for agg in aggs:
            fid = f"fig_{agg.aggregate_id.lower()}"
            d = ctx.root / "artifacts" / "figures" / fid
            d.mkdir(parents=True, exist_ok=True)
            metric = agg.comparison["metric"]
            spec = FigureSpec(fig_id=fid, kind="bar", aggregate_id=agg.aggregate_id, x=agg.comparison["group_by"][0],
                              metric=metric, error="ci95", title=agg.comparison.get("question", ""),
                              xlabel=agg.comparison["group_by"][0], ylabel=f"{metric} (%)", scale=100,
                              caption=f"{metric} 的均值，误差条为 95% 置信区间（每组 n 见 data.csv）。")
            rows = [{"label": _gkey(r.group), "mean": r.mean * 100,
                     "lo": r.ci95[0] * 100 if r.ci95 else None, "hi": r.ci95[1] * 100 if r.ci95 else None,
                     "n": r.n} for r in agg.rows]
            (d / "spec.json").write_text(spec.model_dump_json(indent=2), encoding="utf-8")
            (d / "data.csv").write_text("label,mean,ci95_low,ci95_high,n\n" + "".join(
                f"{r['label']},{r['mean']:.4f},{r['lo']},{r['hi']},{r['n']}\n" for r in rows), encoding="utf-8")
            (d / f"{fid}.svg").write_text(svg_bar_chart(rows, spec.title, spec.ylabel), encoding="utf-8")
            agg_ref = ctx.archive.latest(f"artifacts/aggregates/{agg.aggregate_id}.json")
            fig = Figure(fig_id=fid, spec=spec, aggregate_ref=agg_ref,
                         run_ids=[x for r in agg.rows for x in r.run_ids], skill_ref="fake-plotting@0",
                         script_sha256=sha256_bytes(inspect.getsource(svg_bar_chart).encode()),
                         outputs=[f"artifacts/figures/{fid}/{fid}.svg"],
                         n_per_group={_gkey(r.group): r.n for r in agg.rows}, created_at=now())
            (d / "figure.json").write_text(fig.model_dump_json(indent=2), encoding="utf-8")
            fig_refs.append(ctx.archive.put(f"artifacts/figures/{fid}", "figure", d, parents=[agg_ref],
                                            producer="agent:writing/figures"))
        s["fig_refs"] = [r.model_dump(mode="json") for r in fig_refs]

    # ------------------------------------------------------------ 2 论断与章节
    def _write(self, ctx: StageContext, s: dict) -> None:
        paper = ctx.root / "paper"
        (paper / "sections").mkdir(parents=True, exist_ok=True)
        aggs = self._aggregates(ctx)
        claims: list[Claim] = []
        results_tex: list[str] = []
        for agg in aggs:
            aid = agg.aggregate_id
            version = ctx.archive.latest(f"artifacts/aggregates/{aid}.json").version
            metric = agg.comparison["metric"]
            fig = f"fig_{aid.lower()}"
            for c in agg.contrasts:
                ra = next(r for r in agg.rows if r.group == c.a)
                rb = next(r for r in agg.rows if r.group == c.b)
                va = ValueRef(key=f"{aid}.{_gkey(c.a)}.{metric}.mean", aggregate_id=aid, aggregate_version=version,
                              group=c.a, metric=metric, stat="mean", fmt=PCT,
                              rendered=evidence.render_value(ra.mean, PCT))
                vb = ValueRef(key=f"{aid}.{_gkey(c.b)}.{metric}.mean", aggregate_id=aid, aggregate_version=version,
                              group=c.b, metric=metric, stat="mean", fmt=PCT,
                              rendered=evidence.render_value(rb.mean, PCT))
                vn = ValueRef(key=f"{aid}.{_gkey(c.a)}.{metric}.n", aggregate_id=aid, aggregate_version=version,
                              group=c.a, metric=metric, stat="n", fmt=COUNT,
                              rendered=evidence.render_value(ra.n, COUNT))
                contrast = {"a": c.a, "b": c.b}
                vd = ValueRef(key=f"{aid}.{_gkey(c.a)}-vs-{_gkey(c.b)}.{metric}.diff_mean", aggregate_id=aid,
                              aggregate_version=version, group={}, metric=metric, stat="diff_mean",
                              contrast=contrast, fmt=PCT, rendered=evidence.render_value(c.diff_mean, PCT))
                ev = [EvidenceLink(type="aggregate", ref=aid, version=version, selector={"metric": metric}),
                      EvidenceLink(type="figure", ref=fig)]
                n1 = f"CL{len(claims) + 1}"
                t1 = (f"\\claim{{{n1}}}{{With \\airval{{{vn.key}}} seeds each, "
                      f"{_tex_escape(_gkey(c.a))} reaches a mean "
                      f"{_tex_escape(metric)} of \\airval{{{va.key}}}\\% compared with \\airval{{{vb.key}}}\\% "
                      f"for {_tex_escape(_gkey(c.b))}.}}")
                claims.append(Claim(claim_id=n1, section="results", text=t1, type="numeric",
                                    statement_kind="observation", hedge="none", importance="major",
                                    hypothesis="H1" if aid == "C1" else None, values=[va, vb, vn], evidence=ev))
                vp = None
                if c.p_value is not None:
                    vp = ValueRef(key=f"{aid}.{_gkey(c.a)}-vs-{_gkey(c.b)}.{metric}.p_value", aggregate_id=aid,
                                  aggregate_version=version, group={}, metric=metric, stat="p_value",
                                  contrast=contrast, fmt=PVAL, rendered=evidence.render_value(c.p_value, PVAL))
                p_text = f"; Welch p = \\airval{{{vp.key}}}, reported for reference only" if vp else ""
                n2 = f"CL{len(claims) + 1}"
                asserted = "a>b" if c.diff_mean > 0 else ("a<b" if c.diff_mean < 0 else "a≈b")
                word = "higher" if c.diff_mean > 0 else "lower"
                t2 = (f"\\claim{{{n2}}}{{The mean difference is \\airval{{{vd.key}}} percentage points "
                      f"({_tex_escape(_gkey(c.a))} {word} than {_tex_escape(_gkey(c.b))}{p_text}).}}")
                claims.append(Claim(claim_id=n2, section="results", text=t2, type="comparative",
                                    statement_kind="observation", hedge="none", importance="major",
                                    values=[vd, *([vp] if vp else [])], evidence=ev,
                                    comparison=ClaimComparison(a=c.a, b=c.b, metric=metric, asserted=asserted)))
                results_tex += [t1, "", t2, ""]
        papers = literature.load_papers(ctx.root)
        cited = list(papers)[:2]
        keys = [papers[p].citation_key for p in cited]
        n3 = f"CL{len(claims) + 1}"
        t3 = (f"\\claim{{{n3}}}{{We compare a baseline with a main method and an ablation, following common "
              f"fine-tuning practice~\\cite{{{','.join(keys)}}}.}}")
        claims.append(Claim(claim_id=n3, section="introduction", text=t3, type="citation",
                            statement_kind="observation", hedge="none", importance="minor",
                            evidence=[EvidenceLink(type="paper", ref=p) for p in cited]))

        # air_values.tex（6.3）
        macros = {v.key: v.rendered for c in claims for v in c.values}
        av = ["% 由 airesearcher 自动生成，请勿手工修改", "\\makeatletter",
              "\\newcommand{\\airval}[1]{\\@ifundefined{airval@#1}{\\textbf{??#1??}}{\\@nameuse{airval@#1}}}",
              "\\makeatother"]
        av += [f"\\expandafter\\def\\csname airval@{k}\\endcsname{{{v}}}" for k, v in sorted(macros.items())]
        (paper / "air_values.tex").write_text("\n".join(av) + "\n", encoding="utf-8")
        (paper / "claims.jsonl").write_text("".join(c.model_dump_json() + "\n" for c in claims), encoding="utf-8")
        (paper / "references.bib").write_text(literature.bibtex_for(ctx.root, cited), encoding="utf-8")

        title = _tex_escape(ctx.project.title) if ctx.project.title.isascii() else "AI Researcher Draft"
        notes = s.get("notes", [])
        sec = {
            "abstract": "This draft was generated by the fake writing stage of the AI Researcher Agent. "
                        "All numbers are bound to aggregate results through \\texttt{\\textbackslash airval} macros.",
            "introduction": t3,
            "method": "Each experiment from the approved plan was run with three seeds using the task template. "
                      "Aggregation uses the sample standard deviation and t-based "
                      "ninety-five percent confidence intervals.",
            "results": "\n".join(results_tex),
            "conclusion": "These results come from a small number of seeds on a single task and should be read as "
                          "preliminary." + ("\n\nThis version was revised according to reviewer comments.\n"
                                            + "".join(f"% 修订说明：{n.replace(chr(10), ' ')}\n" for n in notes)
                                            if notes else ""),
        }
        for name, body in sec.items():
            (paper / "sections" / f"{name}.tex").write_text(f"% {name}\n{body}\n", encoding="utf-8")
        main = [
            "\\documentclass{article}", "\\usepackage[T1]{fontenc}", "\\usepackage{url}",
            "\\newcommand{\\claim}[2]{#2}", "\\input{air_values}",
            f"\\title{{{title}}}", "\\author{AI Researcher Agent (fake stages)}", "\\date{}",
            "\\begin{document}", "\\maketitle",
            "\\begin{abstract}", "\\input{sections/abstract}", "\\end{abstract}",
            "\\section{Introduction}", "\\input{sections/introduction}",
            "\\section{Method}", "\\input{sections/method}",
            "\\section{Results}", "\\input{sections/results}",
            "\\section{Conclusion}", "\\input{sections/conclusion}",
            "\\bibliographystyle{plain}", "\\bibliography{references}", "\\end{document}",
        ]
        (paper / "main.tex").write_text("\n".join(main) + "\n", encoding="utf-8")

    # ------------------------------------------------------------ 3 编译、登记、核验
    def _compile(self, ctx: StageContext) -> CompileReport:
        return compile_paper(ctx.root / "paper", enabled=ctx.project.dev.compile)

    def _compile_register_review(self, ctx: StageContext, s: dict) -> None:
        paper = ctx.root / "paper"
        report = self._compile(ctx)
        (paper / "build" / "compile_report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
        agg_refs = [v.ref for v in ctx.archive.list_latest("aggregate") if v.path.endswith(".json")]
        fig_refs = [VersionRef(**r) for r in s.get("fig_refs", [])]
        paper_ref = ctx.archive.put("paper", "paper", paper, exclude=PAPER_EXCLUDE,
                                    parents=[*agg_refs, *fig_refs], producer="agent:writing",
                                    note=(s.get("notes") or ["初稿"])[-1])
        claims_ref = ctx.archive.put("paper/claims.jsonl", "claims", paper / "claims.jsonl", parents=[paper_ref],
                                     producer="agent:writing")
        pdf_ref = ctx.archive.put("paper/build/main.pdf", "paper", paper / "build" / "main.pdf",
                                  parents=[paper_ref], producer="agent:writing")
        compile_ref = ctx.archive.put("paper/build/compile_report.json", "other",
                                      paper / "build" / "compile_report.json", parents=[paper_ref],
                                      producer="agent:writing")
        check = evidence.check(ctx.root, paper_ref)
        review_dir = paper / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        n = len(list(review_dir.glob("review_v*.json"))) + 1
        rp = review_dir / f"review_v{n}.json"
        rp.write_text(check.model_dump_json(indent=2), encoding="utf-8")
        md = [f"# 核验报告 v{n}（确定性检查，假实现阶段）", "", f"- 论文版本：paper v{paper_ref.version}",
              f"- 统计：{check.counts}", f"- 编译：{'成功' if check.compile_ok else '未成功'}", ""]
        for c in check.claims:
            md.append(f"- {c.claim_id}：{c.support}" + "".join(f"；{i.code}（{i.detail}）" for i in c.issues))
        md += [f"- {i.code}：{i.detail}" for i in check.unbound_issues]
        (review_dir / f"review_v{n}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        review_ref = ctx.archive.put(f"paper/review/review_v{n}.json", "review_report", rp, parents=[paper_ref],
                                     producer="agent:writing/review")
        s["paper_ref"] = paper_ref.model_dump(mode="json")
        s["refs"] = {"review": review_ref.model_dump(mode="json"), "compile": compile_ref.model_dump(mode="json"),
                     "claims": claims_ref.model_dump(mode="json"), "pdf": pdf_ref.model_dump(mode="json"),
                     "review_path": rp.relative_to(ctx.root).as_posix()}
        ctx.events.append("stage.decision", f"（假实现）论文 v{paper_ref.version} 核验完成：{check.counts}",
                          actor="agent:writing", refs=[paper_ref])


def create_stage() -> FakeWritingStage:
    return FakeWritingStage()

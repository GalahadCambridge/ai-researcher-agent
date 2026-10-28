"""Self-contained deterministic renderer, also copied verbatim into each artifact.

Requires the installed airesearcher models and matplotlib, but no services, skills,
network, style files or original workspace. Source values remain unscaled in CSV.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
from matplotlib import font_manager  # noqa: E402
from matplotlib import pyplot as plt  # noqa: E402

from airesearcher.core.figure_errors import PresentationError, UnitLabelError  # noqa: E402
from airesearcher.core.models.aggregate import Aggregate, AggregateRow  # noqa: E402
from airesearcher.core.models.figure import FigureSpec  # noqa: E402

# Embedded for artifact replay; test verifies agreement with templates/air.mplstyle.
STYLE = {
    "font.family": ["DejaVu Sans"], "font.size": 10, "axes.titlesize": 11, "axes.labelsize": 10,
    "axes.spines.top": False, "axes.spines.right": False, "axes.axisbelow": True,
    "lines.linewidth": 1.5, "lines.markersize": 5, "figure.figsize": [6.4, 4.2],
    "figure.dpi": 100, "savefig.dpi": 150, "pdf.fonttype": 42,
}
COLORS = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#000000"]
MARKERS = ["o", "s", "^", "D", "v", "P", "X"]
HATCHES = ["", "//", "xx", "..", "\\\\", "++", "oo"]
BASE_COLUMNS = ["group", "metric", "eval_condition", "mean", "std", "ci95_low", "ci95_high",
                "min", "max", "n", "values", "run_ids", "display_mean", "display_std",
                "display_ci95_low", "display_ci95_high", "display_values"]


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def is_numeric(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def ordered(values: list) -> list:
    unique = {canonical(v): v for v in values}
    return sorted(unique.values()) if all(is_numeric(v) for v in unique.values()) else [
        unique[k] for k in sorted(unique)
    ]


# Extract count expressions independently of their semantic role. Bare English
# counts are observable execution/ambiguous quantities, not a new common-n form.
_CAPTION_COUNTS = re.compile(
    r"\bn\s*=\s*(?P<n>\d+)|(?P<shorthand>\b(?:over|across)\s+)?"
    r"(?P<count>\d+)\s*(?:(?P<modifier>total|failed|excluded|discarded|trial|pilot|smoke)\s+)?"
    r"(?P<unit>seeds\b|runs\b|observations\b|(?:个)?(?:种子|次运行|个观测))", re.I,
)
# Decimal points belong to rates, not sentence boundaries.
_CLAUSE_BREAK = r"[,，;；。!?！？\n]|(?<!\d)\.|\.(?!\d)"
_EXECUTION_TOPIC = (
    r"\b(?:failures?|failed|exclusions?|excluded|discarded|trial|pilot|smoke|total)\b|"
    r"失败|排除|剔除|试运行|总计|总共|共计|累计"
)


def _caption_count_role(match: re.Match, before: str, after: str) -> str:
    """Classify an unbound count by its local subject/relation/predicate.

    Context stops at adjacent count expressions and clause boundaries. A rate
    subject can govern a run denominator across/among/in those runs; conversely
    a run count can introduce a relative clause about failures or exclusions.
    Neither is a statement of plotted sample size. Unknown bare English counts
    stay ambiguous; only the established shorthand grammar supplies common n.
    Explicit n and group bindings are handled before this classifier.
    """
    left = re.split(_CLAUSE_BREAK, before)[-1]
    # A comma can introduce the predicate of the current count, but the predicate
    # must be complete: "failures occurred across <next count>" belongs to that
    # next denominator, not to the current "means over 3 runs".
    continuation = re.split(r"[;；。!?！？\n]|(?<!\d)\.|\.(?!\d)", after, maxsplit=1)[0]
    denominator = re.search(
        r"\b(?:failures?|exclusions?)(?:\s+rates?)?"
        r"(?:\s+(?:was|were|is|are|of|occurred|\d+(?:\.\d+)?))*"
        r"\s+(?:over|across|among|in|of|from)\s*$", left + (match["shorthand"] or ""), re.I,
    )
    modifier = re.search(
        r"\b(?:total(?:\s+of)?|failures?(?:\s+occurred)?|failed|excluded|discarded|trial|pilot|smoke)"
        r"(?:\s+(?:runs|seeds|observations|execution))?\s*[:：]?\s*$|"
        r"(?:总计|总共|共计|共|累计)(?:执行|进行)?了?\s*$|"
        r"(?:排除|剔除|失败|试运行|筛选后剩余)了?\s*$", left, re.I,
    )
    relative = re.match(
        r"\s*(?:中|之中|当中)(?:的|有)?\s*[,，]?\s*(?:\d+\s*次\s*)?(?:" + _EXECUTION_TOPIC + r")",
        continuation, re.I,
    )
    predicate = re.match(
        r"\s*[,，]?\s*(?:(?:which|that)\s+)?"
        r"(?:(?:were|was|are|is|have been|had been)\s+)?"
        r"(?:in\s+total\b|overall\b|(?:excluded|discarded|failed)\b(?!\s+(?:runs|seeds|observations))|"
        r"(?:(?:one|a|\d+)\s+)?failures?\s+occurred\s*$|filtered\s+out\b|"
        r"(?:executed|attempted|performed)\s+in\s+total\b|remained\s+after\s+filtering\b|"
        r"被(?:排除|剔除)|(?:失败|总计)(?!\s*\d)|用于试运行)", continuation, re.I,
    )
    if match["modifier"] or denominator or modifier or relative or predicate:
        return "execution"
    if match["shorthand"] or re.search(r"种子|运行|观测", match["unit"]):
        return "common"
    return "ambiguous"


def _caption_group(caption: str, match: re.Match, expected: dict[str, int],
                   aliases: dict[str, set[str]]) -> set[str]:
    """Resolve the existing canonical/unique-short-label binding grammar first."""
    tail, candidates = caption[match.end():].lstrip(), set()
    if tail.startswith("("):
        label = tail[1:].lstrip()
        if label.startswith("{"):
            try:
                group, end = json.JSONDecoder().raw_decode(label)
            except ValueError as exc:
                raise PresentationError("invalid caption group label") from exc
            if not label[end:].lstrip().startswith(")"):
                raise PresentationError("unclosed caption group label")
            candidates = {canonical(group)} & expected.keys()
        elif ")" in label:
            candidates = aliases.get(label.split(")", 1)[0].strip(), set())
        if len(candidates) != 1:
            raise PresentationError("caption group label is unknown or ambiguous")
    else:
        # Also accept A: n=3 / A (n=3), with a unique short group label.
        before = caption[:match.start()]
        for label, groups in aliases.items():
            if re.search(r"(?<!\w)" + re.escape(label) + r"\s*[:,]?\s*\(?\s*$", before):
                candidates.update(groups)
    if len(candidates) > 1:
        raise PresentationError("caption group label is ambiguous")
    return candidates


def validate_caption_counts(caption: str, rows: list[AggregateRow]) -> None:
    """Validate sample counts after binding and classifying each expression.

    Explicit n and explicit group bindings always participate, even alongside
    execution vocabulary. Homogeneous groups retain common-n shorthand; once a
    group is named all groups need coverage. Heterogeneous n needs binding.
    """
    expected = {canonical(r.group): r.n for r in rows}
    aliases: dict[str, set[str]] = {}
    for row in rows:
        for value in row.group.values():
            aliases.setdefault(str(value), set()).add(canonical(row.group))
    counts = list(_CAPTION_COUNTS.finditer(caption))
    samples = []
    for index, match in enumerate(counts):
        # Newly observed bare English quantities must not acquire the binding
        # semantics of the established n=/over/across/Chinese sample grammar.
        supported = match["n"] is not None or match["shorthand"] or re.search(r"种子|运行|观测", match["unit"])
        candidates = _caption_group(caption, match, expected, aliases) if supported else set()
        if match["n"] is not None:
            role = "explicit"
        elif candidates:
            role = "group"
        else:
            start = counts[index - 1].end() if index else 0
            # Include the next expression as lookahead: cutting just before it
            # would turn "failures occurred across 2 runs" into the apparently
            # complete predicate "failures occurred" (across is in the match).
            end = counts[index + 1].end() if index + 1 < len(counts) else len(caption)
            role = _caption_count_role(match, caption[start:match.start()], caption[match.end():end])
        if role in {"explicit", "group", "common"}:
            samples.append((int(match["n"] or match["count"]), candidates))
    if {count for count, _ in samples} != {r.n for r in rows}:
        raise PresentationError("caption must state the actual n for every group")
    bound = set()
    for count, candidates in samples:
        if candidates:
            key = next(iter(candidates))
            if count != expected[key]:
                raise PresentationError("caption n does not match its group")
            bound.add(key)
        elif len(set(expected.values())) > 1:
            raise PresentationError("heterogeneous caption counts require group-specific n")
    if bound and bound != expected.keys():
        raise PresentationError("caption must state the actual n for every group; group omitted")


def validate_rows(spec: FigureSpec, rows: list[AggregateRow], condition: str) -> None:
    """Check plot semantics without estimating or repairing experimental statistics."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", spec.fig_id):
        raise PresentationError("fig_id must be a safe filename identifier")
    if not math.isfinite(spec.scale) or spec.scale <= 0:
        raise PresentationError("scale must be positive and finite")
    if not spec.xlabel.strip() or not spec.ylabel.strip():
        raise UnitLabelError("xlabel and ylabel must be nonempty")
    units = re.findall(r"[\[(]([^\])]+)[\])]", spec.ylabel)
    has_unit = any(u.strip().lower() not in {"", "?", "unit", "units", "unknown", "n/a", "none", "单位"}
                   for u in units)
    if not (has_unit or re.search(r"%|dimensionless|unitless|无量纲", spec.ylabel, re.I)):
        raise UnitLabelError("ylabel must state a unit in parentheses/brackets or be explicitly dimensionless")
    if not rows:
        raise PresentationError(f"metric {spec.metric!r} has no rows")
    if spec.hue == spec.x:
        raise PresentationError("x and hue must be distinct grouping fields")
    keys = set(rows[0].group)
    axes = {spec.x} | ({spec.hue} if spec.hue else set())
    if len(set(spec.facets)) != len(spec.facets) or axes.intersection(spec.facets):
        raise PresentationError("facets must be distinct grouping fields, separate from x/hue")
    axes.update(spec.facets)
    if not axes <= keys:
        raise PresentationError("unknown grouping field in x/hue")
    if len({canonical({k: r.group[k] for k in spec.facets}) for r in rows}) > 16:
        raise PresentationError("facet presentation exceeds 16 panels")
    coords, runs = set(), set()
    for row in rows:
        if set(row.group) != keys or row.metric != spec.metric:
            raise ValueError("inconsistent grouping fields or metric")
        for value in row.group.values():
            if (isinstance(value, bool) or not isinstance(value, str | int | float)
                    or (isinstance(value, str) and not value.strip())
                    or (is_numeric(value) and not math.isfinite(value))):
                raise ValueError("group values must be nonempty strings or finite numbers")
        coordinate = canonical({k: row.group[k] for k in sorted(axes)})
        if coordinate in coords:
            raise PresentationError("duplicate plotted group; refusing to pool hidden dimensions")
        coords.add(coordinate)
        if row.n < 1 or len(row.run_ids) != row.n or any(not r.strip() for r in row.run_ids):
            raise ValueError("n and run provenance must agree")
        if len(set(row.run_ids)) != row.n or runs.intersection(row.run_ids):
            raise ValueError("duplicate run provenance")
        runs.update(row.run_ids)
        if row.values and len(row.values) != row.n:
            raise ValueError("n and raw values must agree")
        if spec.kind in ("point", "box") and len(row.values) != row.n:
            raise PresentationError("point/box requires actual per-run values")
        numbers = [row.mean, row.min, row.max, *row.values]
        if row.std is not None:
            if row.std < 0:
                raise ValueError("std must be nonnegative")
            numbers.append(row.std)
        if not all(math.isfinite(v) and math.isfinite(v * spec.scale) for v in numbers):
            raise ValueError("non-finite source or scaled data")
        if row.ci95 is not None:
            if len(row.ci95) != 2 or not row.ci95[0] <= row.mean <= row.ci95[1]:
                raise ValueError("invalid CI bounds")
            numbers.extend(row.ci95)
        if not all(math.isfinite(v) and math.isfinite(v * spec.scale) for v in numbers):
            raise ValueError("non-finite source or scaled data")
        if not row.min <= row.mean <= row.max or any(not row.min <= v <= row.max for v in row.values):
            raise ValueError("values/mean outside source min/max")
        if row.n >= 2 and spec.error == "none":
            raise PresentationError("uncertainty required for n >= 2")
        if spec.error == "ci95" and row.ci95 is None:
            raise PresentationError("requested CI is unavailable")
        if spec.error == "std" and row.std is None:
            raise PresentationError("requested std is unavailable")
        if spec.error == "ci95" and not all(math.isfinite(abs(v - row.mean) * spec.scale) for v in row.ci95):
            raise ValueError("non-finite scaled CI error length")
    for key in keys - axes:
        if len({canonical(r.group[key]) for r in rows}) > 1:
            raise PresentationError(f"unplotted grouping field varies: {key}")
    numeric_x = all(is_numeric(r.group[spec.x]) for r in rows)
    if spec.kind == "line" and not numeric_x:
        raise PresentationError("line plot requires numeric x")
    if spec.log_x and (not numeric_x or spec.kind == "bar" or any(r.group[spec.x] <= 0 for r in rows)):
        raise PresentationError("log_x requires positive numeric x on line/point/box")
    validate_caption_counts(spec.caption, rows)
    if spec.error == "ci95" and not re.search(r"ci95|95\s*%\s*(?:CI|confidence|置信)", spec.caption, re.I):
        raise PresentationError("caption must define 95% CI error bars")
    if spec.error == "std" and not re.search(r"\bstd\b|\bSD\b|standard deviation|标准差", spec.caption, re.I):
        raise PresentationError("caption must define standard deviation error bars")
    if condition not in ("final", "explore"):
        raise ValueError("unknown eval_condition")
    if condition == "explore" and not re.search(r"exploratory|探索性", spec.title + " " + spec.caption, re.I):
        raise PresentationError("exploratory result must be labelled")


def select_rows(aggregate: Aggregate, spec: FigureSpec) -> list[AggregateRow]:
    if spec.aggregate_id != aggregate.aggregate_id:
        raise PresentationError("spec aggregate_id does not match source Aggregate")
    rows = [r for r in aggregate.rows if r.metric == spec.metric]
    validate_rows(spec, rows, aggregate.eval_condition)
    xs = ordered([r.group[spec.x] for r in rows])
    hs = ordered([r.group[spec.hue] for r in rows]) if spec.hue else [None]
    return sorted(rows, key=lambda r: (canonical({k: r.group[k] for k in spec.facets}),
                                       hs.index(r.group[spec.hue]) if spec.hue else 0,
                                       xs.index(r.group[spec.x])))


def csv_text(rows: list[AggregateRow], spec: FigureSpec, condition: str) -> str:
    keys = sorted(rows[0].group)
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=[*(f"group.{k}" for k in keys), *BASE_COLUMNS], lineterminator="\n")
    writer.writeheader()
    for row in rows:
        low, high = row.ci95 if row.ci95 is not None else (None, None)
        fields = {"mean": row.mean, "std": row.std, "ci95_low": low, "ci95_high": high}
        record = {f"group.{k}": canonical(row.group[k]) for k in keys}
        record.update({"group": canonical(row.group), "metric": row.metric, "eval_condition": condition,
                       "n": str(row.n), "min": repr(row.min), "max": repr(row.max),
                       "values": canonical(row.values), "run_ids": canonical(row.run_ids),
                       "display_values": canonical([v * spec.scale for v in row.values])})
        for key, value in fields.items():
            record[key] = "" if value is None else repr(value)
            record[f"display_{key}"] = "" if value is None else repr(value * spec.scale)
        writer.writerow(record)
    return buf.getvalue()


def read_data(path: Path, spec: FigureSpec) -> tuple[list[AggregateRow], str]:
    text = path.read_text(encoding="utf-8")
    reader = csv.DictReader(io.StringIO(text))
    records = list(reader)
    if not records or not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ValueError("empty or invalid data.csv")
    rows, conditions = [], set()
    for record in records:
        if None in record or any(v is None for v in record.values()):
            raise ValueError("malformed CSV record")
        group = json.loads(record["group"])
        ci = None if record["ci95_low"] == record["ci95_high"] == "" else [
            float(record["ci95_low"]), float(record["ci95_high"])
        ]
        rows.append(AggregateRow(group=group, metric=record["metric"], n=int(record["n"]),
                                 mean=float(record["mean"]), std=float(record["std"]) if record["std"] else None,
                                 sem=None, ci95=ci, min=float(record["min"]), max=float(record["max"]),
                                 values=json.loads(record["values"]), run_ids=json.loads(record["run_ids"])))
        conditions.add(record["eval_condition"])
    if len(conditions) != 1:
        raise ValueError("mixed eval_condition")
    condition = conditions.pop()
    validate_rows(spec, rows, condition)
    expected = list(csv.DictReader(io.StringIO(csv_text(rows, spec, condition))))
    if expected != records or reader.fieldnames != list(expected[0]):
        raise ValueError("CSV source/display values, grouping, scale or columns are inconsistent")
    return rows, condition


def errors(row: AggregateRow, spec: FigureSpec) -> list[list[float]] | None:
    if spec.error == "none":
        return None
    if spec.error == "std":
        return [[row.std * spec.scale], [row.std * spec.scale]]
    return [[(row.mean - row.ci95[0]) * spec.scale], [(row.ci95[1] - row.mean) * spec.scale]]


def presentation_style():
    """Use installed named CJK fallbacks; never require or download a font.

    DejaVu remains first, preserving Latin glyphs. When no CJK font is present,
    Matplotlib keeps its existing missing-glyph warning rather than changing text.
    """
    names = {font.name for font in font_manager.fontManager.ttflist}
    candidates = ("Noto Sans CJK SC", "Noto Sans CJK TC", "Source Han Sans SC", "Microsoft YaHei", "PingFang SC")
    return {**STYLE, "font.family": ["DejaVu Sans", *(n for n in candidates if n in names)]}


def draw(spec: FigureSpec, rows: list[AggregateRow], *, ax=None, x_order=None, hue_order=None):
    """Return a Matplotlib Figure; callers close it. No random offsets or interpolation."""
    xs = x_order if x_order is not None else ordered([r.group[spec.x] for r in rows])
    hues = hue_order if hue_order is not None else (ordered([r.group[spec.hue] for r in rows]) if spec.hue else [None])
    if spec.facets:
        panels = sorted({canonical({k: r.group[k] for k in spec.facets}) for r in rows})
        if len(panels) > 16:
            raise PresentationError("facet presentation exceeds 16 panels")
        columns = min(3, len(panels))
        fig, axes = plt.subplots(math.ceil(len(panels) / columns), columns, squeeze=False, sharey=True,
                                 figsize=(6.4 * columns, 4.2 * math.ceil(len(panels) / columns)))
        for index, axis in enumerate(axes.flat):
            if index >= len(panels):
                axis.set_visible(False)
                continue
            panel = panels[index]
            selected = [r for r in rows if canonical({k: r.group[k] for k in spec.facets}) == panel]
            draw(spec.model_copy(update={"facets": [], "title": spec.title + "\n" + panel}), selected,
                 ax=axis, x_order=xs, hue_order=hues)
        fig.tight_layout()
        return fig
    numeric = all(is_numeric(x) for x in xs) and spec.kind != "bar"
    positions = [float(x) for x in xs] if numeric else list(range(len(xs)))
    if spec.log_x:
        left, right = math.log10(min(positions)), math.log10(max(positions))
        box_width = (right - left) / max(1, len(positions) - 1) or 0.2
    else:
        box_width = min((b - a for a, b in zip(positions, positions[1:], strict=False)), default=1.0)
    width = 0.7 * box_width / len(hues)
    own_axes = ax is None
    fig, ax = plt.subplots() if own_axes else (ax.figure, ax)
    for index, hue in enumerate(hues):
        selected = [r for r in rows if not spec.hue or r.group[spec.hue] == hue]
        selected.sort(key=lambda r: xs.index(r.group[spec.x]))
        color, marker, hatch = COLORS[index % len(COLORS)], MARKERS[index % len(MARKERS)], HATCHES[index % len(HATCHES)]
        offset = (index - (len(hues) - 1) / 2) * width
        actual = [positions[xs.index(r.group[spec.x])] for r in selected]
        shifted = [10 ** (math.log10(x) + offset) if spec.log_x else x + offset for x in actual]
        label = str(hue) if spec.hue else spec.metric
        means = [r.mean * spec.scale for r in selected]
        if spec.kind == "line":
            ax.plot(actual, means, label=label, color=color, marker=marker, linestyle=["-", "--", "-.", ":"][index % 4])
        for j, row in enumerate(selected):
            x = actual[j] if spec.kind == "line" else shifted[j]
            yerr = errors(row, spec)
            if spec.kind == "bar":
                ax.bar(x, means[j], width=width * 0.9, color=color, hatch=hatch,
                       edgecolor="black", linewidth=0.5, label=label if j == 0 else None)
            elif spec.kind in ("point", "box"):
                values = [v * spec.scale for v in row.values]
                if spec.kind == "box":
                    bw = x * (10 ** (width * 0.4) - 10 ** (-width * 0.4)) if spec.log_x else width * 0.8
                    ax.boxplot([values], positions=[x], widths=[bw], manage_ticks=False, showfliers=False,
                               patch_artist=True, boxprops={"facecolor": "none", "edgecolor": color, "hatch": hatch},
                               medianprops={"color": color})
                # Constant positions preserve exact x and show every observation without random jitter.
                ax.scatter([x] * len(values), values, color=color, marker=marker, alpha=0.65,
                           label=label if j == 0 else None)
                ax.plot([x], [means[j]], marker="_", color="black", markersize=14)
            if yerr is not None:
                ax.errorbar([x], [means[j]], yerr=yerr, fmt="none", ecolor="black", capsize=3)
    if spec.log_x:
        ax.set_xscale("log")
    ax.set_xticks(positions, [str(x) for x in xs])
    ax.set(xlabel=spec.xlabel, ylabel=spec.ylabel, title=spec.title)
    ax.grid(axis="y", alpha=0.2)
    ax.legend(frameon=False)
    if own_axes:
        fig.tight_layout()
    return fig


def render(spec: FigureSpec, data: Path, out: Path) -> None:
    rows, _condition = read_data(data, spec)
    out.mkdir(parents=True, exist_ok=True)
    with plt.rc_context(presentation_style()):
        fig = draw(spec, rows)
        try:
            fig.savefig(out / f"{spec.fig_id}.pdf", metadata={"CreationDate": None, "ModDate": None})
            fig.savefig(out / f"{spec.fig_id}.png", metadata={"Software": "airesearcher scientific-plotting"})
        finally:
            plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        spec = FigureSpec.model_validate_json(args.spec.read_text(encoding="utf-8"))
        render(spec, args.data, args.out)
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, f"scientific-plotting: {exc}\n")


if __name__ == "__main__":
    main()

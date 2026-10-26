"""Selectors are model intent; numerical values and identities are code-owned."""

import copy

import pytest

from airesearcher.stages.writing.bundle import build_bundle


@pytest.fixture
def bound_inputs(sample_project):
    from airesearcher.stages.writing.claims import load_figures

    p = sample_project
    refs = [p.archive.latest(f"artifacts/aggregates/{cid}.json") for cid in ("C1", "C2")]
    bundle = build_bundle(p.root, aggregate_refs=refs, include_runtime=True)
    figures = load_figures(p.root, {fid: p.archive.latest(f"artifacts/figures/{fid}") for fid in ("fig_c1", "fig_c2")})
    return p, bundle, figures


def valid_plan(bundle):
    claims = []
    for i, source in enumerate(bundle.aggregates):
        for contrast in source.data.contrasts:
            claims.append(
                {
                    "section": "results",
                    "text_template": "The observed mean difference is [[D]].",
                    "type": "comparative",
                    "statement_kind": "observation",
                    "hedge": "none",
                    "importance": "major",
                    "hypothesis": "H1" if i == 0 else None,
                    "values": [
                        {
                            "name": "D",
                            "aggregate_id": source.aggregate_id,
                            "group": {},
                            "metric": contrast.metric,
                            "stat": "diff_mean",
                            "contrast": {"a": contrast.a, "b": contrast.b},
                        }
                    ],
                    "comparison": {
                        "aggregate_id": source.aggregate_id,
                        "a": contrast.a,
                        "b": contrast.b,
                        "metric": contrast.metric,
                        "asserted": "a>b" if contrast.diff_mean > 0 else "a<b",
                    },
                    "evidence": [{"type": "aggregate", "ref": source.aggregate_id}],
                }
            )
    return {"claims": claims}


def test_bind_exact_values_and_macros(bound_inputs):
    from airesearcher.stages.writing.claims import bind_plan, claims_bytes, values_tex

    p, bundle, figures = bound_inputs
    claims, errors = bind_plan(p.root, valid_plan(bundle), bundle, figures)
    assert not errors and [c.claim_id for c in claims] == ["CL1", "CL2"]
    assert claims[0].values[0].aggregate_version == bundle.aggregates[0].ref.version
    assert claims[0].text.startswith(r"\claim{CL1}{") and r"\airval{" in claims[0].text
    assert claims[0].location is None
    assert claims_bytes(claims) == claims_bytes(claims)
    assert values_tex(claims) == values_tex(list(reversed(claims)))


@pytest.mark.parametrize(
    "field,value", [("rendered", "999"), ("key", "invented"), ("aggregate_version", 9), ("fmt", {"scale": 999})]
)
def test_planner_cannot_supply_trusted_value_fields(bound_inputs, field, value):
    from airesearcher.stages.writing.claims import bind_plan

    p, bundle, figures = bound_inputs
    plan = valid_plan(bundle)
    plan["claims"][0]["values"][0][field] = value
    _, errors = bind_plan(p.root, plan, bundle, figures)
    assert errors


@pytest.mark.parametrize(
    "damage",
    [
        "group",
        "metric",
        "stat",
        "contrast",
        "direction",
        "coverage",
        "hypothesis",
        "literal",
        "results_interpretation",
        "unhedged",
    ],
)
def test_invalid_plan_fails_binding_or_coverage(bound_inputs, damage):
    from airesearcher.stages.writing.claims import bind_plan

    p, bundle, figures = bound_inputs
    plan = copy.deepcopy(valid_plan(bundle))
    c = plan["claims"][0]
    v = c["values"][0]
    if damage == "group":
        v.update(stat="mean", contrast=None, group={"method": "missing"})
    elif damage in {"metric", "stat"}:
        v[damage] = "missing"
    elif damage == "contrast":
        v["contrast"] = None
    elif damage == "direction":
        c["comparison"]["asserted"] = "a<b"
    elif damage == "coverage":
        plan["claims"].pop()
    elif damage == "hypothesis":
        c["hypothesis"] = None
    elif damage == "literal":
        c["text_template"] = "The score is 99.9."
    elif damage == "results_interpretation":
        c.update(statement_kind="interpretation", hedge="possible")
    else:
        c.update(section="discussion", statement_kind="interpretation", hedge="none")
    _, errors = bind_plan(p.root, plan, bundle, figures)
    assert errors


@pytest.mark.parametrize("damage", ["unknown", "duplicate", "missing", "key", "direct", "wrong_section"])
def test_marker_insertion_rejects_untrusted_edits(bound_inputs, damage):
    from airesearcher.stages.writing.claims import bind_plan, insert_markers, validate_bound_section

    p, bundle, figures = bound_inputs
    claims, errors = bind_plan(p.root, valid_plan(bundle), bundle, figures)
    assert not errors
    text = "[[CLAIM:CL1]]\n[[CLAIM:CL2]]"
    if damage == "unknown":
        text += " [[CLAIM:CL99]]"
    elif damage == "duplicate":
        text += " [[CLAIM:CL1]]"
    elif damage == "missing":
        text = "[[CLAIM:CL1]]"
    elif damage == "key":
        text += r"\airval{invented}"
    elif damage == "direct":
        text = claims[0].text + " [[CLAIM:CL2]]"
    with pytest.raises(ValueError):
        insert_markers(text, claims, "discussion" if damage == "wrong_section" else "results")
    good = insert_markers("[[CLAIM:CL1]]\n[[CLAIM:CL2]]", claims, "results")
    validate_bound_section("Human context.\n" + good, claims, "results")
    with pytest.raises(ValueError, match="altered"):
        validate_bound_section(good.replace("observed", "invented"), claims, "results")


def test_macro_collision_and_comment_locations(bound_inputs, monkeypatch):
    from airesearcher.stages.writing import claims as module

    p, bundle, figures = bound_inputs
    claims, errors = module.bind_plan(p.root, valid_plan(bundle), bundle, figures)
    assert not errors
    for section in module.SECTIONS:
        path = p.root / f"paper/sections/{section}.tex"
        path.write_text(
            "% " + claims[0].text + "\n" + "\n".join(c.text for c in claims if c.section == section), encoding="utf-8")
    located = module.locate_claims(p.root, claims)
    assert located[0].location["line"] == 2
    monkeypatch.setattr(module, "value_key", lambda selector: "collision")
    _, errors = module.bind_plan(p.root, valid_plan(bundle), bundle, figures)
    assert any("collision" in err for err in errors)


@pytest.mark.parametrize(
    "kind,ref",
    [
        ("aggregate", "C99"),
        ("figure", "fig_outside"),
        ("run", "outside"),
        ("paper", "unknown"),
        ("plan", "other.md"),
        ("log", "other.md"),
    ],
)
def test_evidence_allowlists(bound_inputs, kind, ref):
    from airesearcher.stages.writing.claims import bind_plan

    p, bundle, figures = bound_inputs
    plan = valid_plan(bundle)
    plan["claims"][0]["evidence"].append({"type": kind, "ref": ref})
    _, errors = bind_plan(p.root, plan, bundle, figures)
    assert any(e.startswith("CL1:") for e in errors)


def test_integer_n_and_fixed_p_format(bound_inputs):
    from airesearcher.stages.writing.claims import bind_plan

    p, bundle, figures = bound_inputs
    plan = valid_plan(bundle)
    claim = plan["claims"][0]
    claim["text_template"] += " n=[[N]], p=[[P]]."
    source = bundle.aggregates[0]
    claim["values"] += [
        {"name": "N", "aggregate_id": "C1", "group": source.data.rows[0].group, "metric": "score", "stat": "n"},
        {**claim["values"][0], "name": "P", "stat": "p_value"},
    ]
    bound, errors = bind_plan(p.root, plan, bundle, figures)
    assert not errors
    n, prob = bound[0].values[1:]
    assert n.rendered == "3" and n.fmt["scale"] == 1 and n.fmt["decimals"] == 0
    assert prob.fmt == {"scale": 1, "decimals": 4, "suffix": ""}
    assert bound[0].values[0].fmt["scale"] == figures["fig_c1"].data.spec.scale


def test_negative_nonsignificant_coverage_is_mandatory(bound_inputs):
    from airesearcher.stages.writing.claims import bind_plan, coverage_errors

    p, bundle, figures = bound_inputs
    claims, errors = bind_plan(p.root, valid_plan(bundle), bundle, figures)
    assert not errors
    changed = bundle.model_copy(deep=True)
    changed.aggregates[1].data.contrasts[0].diff_mean = -0.1
    changed.aggregates[1].data.contrasts[0].p_value = 0.9
    assert any("C2" in e for e in coverage_errors(claims[:1], changed))
    assert not coverage_errors(claims, changed)


@pytest.mark.parametrize("definition", [r"\newcommand{\airval}[1]{0}", r"\def\claim#1#2{fake}", r"\input{air_values}"])
def test_template_reserved_macros_fail(bound_inputs, definition):
    from airesearcher.stages.writing.claims import SECTIONS, bind_plan, values_tex
    from airesearcher.stages.writing.writer import SectionOutput, Writer

    from .test_writing_stage import TEMPLATE, set_template

    p, bundle, figures = bound_inputs
    claims, errors = bind_plan(p.root, valid_plan(bundle), bundle, figures)
    assert not errors
    writer = Writer(p.root, bundle, llm=None, claims=claims)
    for section in SECTIONS:
        writer.write_section(
            SectionOutput(section=section, latex="Prose.\n" + "\n".join(c.text for c in claims if c.section == section))
        )
    (p.root / "paper/air_values.tex").write_bytes(values_tex(claims))
    set_template(p, {"main.tex": TEMPLATE.replace(r"\begin{document}", definition + r"\begin{document}")})
    from airesearcher.core.errors import ValidationFailed

    with pytest.raises(ValidationFailed, match="reserved|evidence"):
        writer.assemble_draft(template=p.config.template)

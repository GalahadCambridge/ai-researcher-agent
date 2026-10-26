"""Caption sample sizes stay separate from execution/provenance counts."""

import pytest

from airesearcher.core.models.aggregate import Aggregate
from airesearcher.core.workspace import repo_root
from airesearcher.stages.writing.figures import default_spec, load_renderer, prepare_data

from .test_plot import caption_aggregate


@pytest.fixture
def source():
    original = Aggregate.model_validate_json(
        (repo_root() / "skills/scientific-plotting/sample/input/aggregate.json").read_bytes()
    )
    result = caption_aggregate(original, (3, 3))
    result.rows[0].group = {"method": "baseline"}
    result.rows[1].group = {"method": "main"}
    return result


def validate(source, caption, error="ci95"):
    spec = default_spec(source, ylabel="Latency (s)").model_copy(update={"caption": caption, "error": error})
    return prepare_data(source, spec)


def test_second_smoke_caption_regression(source):
    # The exact response file is unavailable. These preserved diagnostic facts
    # reproduce the same contamination of {3} with execution counts {2, 8}.
    caption = (
        "baseline: n=3; main: n=3. Means over 3 runs per group; error bars: 95% CI. "
        "Failures occurred across 2 runs; execution across 8 runs in total. "
        "另有2次运行被排除，总计8次运行，筛选后剩余3次运行。"
    )
    assert validate(source, caption)


EXECUTION = [
    "2 failed runs were excluded; 8 total runs.",
    "Across 2 runs, failures occurred.",
    "Failures occurred across 2 runs.",
    "Across 2 runs were excluded.",
    "Across 8 runs were executed in total.",
    "Across 8 runs in total.",
    "Across 3 runs remained after filtering.",
    "Trial runs: over 2 runs; smoke runs: across 8 runs.",
    "Failed runs across 2 runs; excluded runs over 8 observations.",
    "2次运行被排除，8次运行总计，筛选后剩余3次运行。",
    "共执行8次运行，排除了2次运行，试运行3次运行。",
    "排除2个种子，总计8个观测。",
]


@pytest.mark.parametrize("context", EXECUTION)
@pytest.mark.parametrize("sample_sizes", ["baseline: n=3; main: n=3.", "n=3 per group."])
def test_execution_counts_do_not_contaminate_group_counts(source, sample_sizes, context):
    assert validate(source, sample_sizes + " " + context + " Error bars: 95% CI.")


@pytest.mark.parametrize("context", EXECUTION)
def test_execution_counts_alone_are_not_sample_sizes(source, context):
    with pytest.raises(ValueError, match="actual n"):
        validate(source, context + " Error bars: 95% CI.")


@pytest.mark.parametrize(
    "sample_sizes",
    [
        "baseline: n=2; main: n=3.",
        "n=2 (baseline), n=3 (main).",
        "baseline: n=3.",
        "n=3 (baseline).",
        "n=2 per group.",
        "baseline: over 2 runs; main: over 3 runs.",
    ],
)
def test_wrong_or_omitted_group_still_rejected(source, sample_sizes):
    with pytest.raises(ValueError):
        validate(source, sample_sizes + " 2 runs were excluded; across 8 runs in total. 95% CI.")


@pytest.mark.parametrize(
    "shorthand",
    [
        "n=3 per group",
        "over 3 seeds",
        "across 3 runs",
        "over 3 observations",
        "每组 n=3",
        "每组3个种子",
        "每组3次运行",
        "每组3个观测",
    ],
)
def test_supported_common_forms_with_provenance(source, shorthand):
    assert validate(source, shorthand + "; 95% CI. Excluded runs over 2 runs; 总计8次运行。")


@pytest.mark.parametrize(
    "sample_sizes,valid",
    [
        ('n=2 ({"method":"A"}), n=3 ({"method":"B"})', True),
        ("n=2 (A); n=3 (B)", True),
        ("A: n=2; B: n=3", True),
        ("n=3 per group", False),
        ("n=2; n=3", False),
        ("A: n=3; B: n=2", False),
        ("A: n=2", False),
    ],
)
def test_heterogeneous_binding_stays_strict(source, sample_sizes, valid):
    source = caption_aggregate(source, (2, 3))
    caption = sample_sizes + "; 95% CI. Across 8 runs in total; 另有2次运行被排除。"
    if valid:
        assert validate(source, caption)
    else:
        with pytest.raises(ValueError):
            validate(source, caption)


@pytest.mark.parametrize("error,description", [("ci95", ""), ("std", "95% CI")])
def test_uncertainty_semantics_unchanged(source, error, description):
    with pytest.raises(ValueError, match="define"):
        validate(source, "n=3 per group. Across 8 runs in total. " + description, error)


def test_ambiguous_short_group_label_still_fails(source):
    source.rows[0].group = {"method": "shared", "dataset": "first"}
    source.rows[1].group = {"method": "shared", "dataset": "second"}
    with pytest.raises(ValueError, match="ambiguous"):
        load_renderer().validate_caption_counts("n=3 (shared); across 8 runs in total.", source.rows)


@pytest.mark.parametrize(
    "caption",
    [
        "Means over 3 runs, excluded runs over 2 runs. 95% CI.",
        "Means over 3 runs, failures occurred across 2 runs. 95% CI.",
        "每组3次运行，失败2次运行，总计8次运行。95% CI.",
    ],
)
def test_adjacent_execution_clause_does_not_erase_real_common_n(source, caption):
    assert validate(source, caption)
    wrong = caption.replace("over 3", "over 2").replace("每组3", "每组2")
    with pytest.raises(ValueError):
        validate(source, wrong)


@pytest.mark.parametrize(
    "caption",
    [
        "Across 3 runs in total.",
        "Excluded runs over 3 seeds.",
        "Trial runs across 3 observations.",
        "总计3次运行。",
        "3个种子被排除。",
    ],
)
def test_execution_count_equal_to_actual_n_is_still_insufficient(source, caption):
    with pytest.raises(ValueError, match="actual n"):
        validate(source, caption + "95% CI.")


@pytest.mark.parametrize("label", ["trial", "total"])
def test_actual_group_label_is_not_mistaken_for_execution_qualifier(source, label):
    source.rows[0].group = {"method": label}
    caption = f"{label}: over 3 runs; main: over 3 runs; across 8 runs in total. 95% CI."
    assert validate(source, caption)
    with pytest.raises(ValueError):
        validate(source, caption.replace(f"{label}: over 3", f"{label}: over 2"))

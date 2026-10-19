"""Scientific plotting: real archived input, rendering semantics, replay and rejection."""

from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
import sys

import matplotlib
import pytest
from matplotlib import pyplot as plt
from matplotlib.collections import LineCollection, PathCollection

from airesearcher.core.archive import Archive
from airesearcher.core.events import Events
from airesearcher.core.fsutil import atomic_write_json, sha256_file
from airesearcher.core.models.aggregate import Aggregate
from airesearcher.core.models.figure import Figure
from airesearcher.core.workspace import repo_root
from airesearcher.services import runs
from airesearcher.skills.loader import SkillLoader
from airesearcher.stages.writing.figures import (
    check_output,
    default_spec,
    generate_figure,
    load_renderer,
    prepare_data,
    validate_artifact,
)

SKILL = repo_root() / "skills/scientific-plotting"


@pytest.fixture
def aggregate():
    return Aggregate.model_validate_json((SKILL / "sample/input/aggregate.json").read_text(encoding="utf-8"))


@pytest.fixture
def plot_spec(aggregate):
    return default_spec(aggregate, fig_id="fig_test", ylabel="Latency (ms)", scale=1000)


@pytest.fixture
def plotting_skill():
    return SkillLoader().load("scientific-plotting", allow_unverified=True)


def archive_source(root, aggregate):
    return Archive(root, Events(root)).put(f"artifacts/aggregates/{aggregate.aggregate_id}.json", "aggregate",
                                          aggregate.model_dump_json(), producer="system")


@pytest.mark.parametrize("kind", ["bar", "line", "point", "box"])
def test_modes_generate_complete_artifact(tmp_path, aggregate, plot_spec, plotting_skill, kind):
    source_ref = archive_source(tmp_path, aggregate)
    spec = plot_spec.model_copy(update={"kind": kind})
    figure = generate_figure(tmp_path, aggregate.aggregate_id, spec, skill=plotting_skill)
    directory = tmp_path / "artifacts/figures/fig_test"
    assert set(p.name for p in directory.iterdir()) == {
        "spec.json", "data.csv", "plot.py", "figure.json", "fig_test.pdf", "fig_test.png",
    }
    assert Figure.model_validate_json((directory / "figure.json").read_text(encoding="utf-8")) == figure
    assert figure.aggregate_ref == source_ref
    assert figure.script_sha256 == sha256_file(directory / "plot.py")
    assert len(figure.run_ids) == len(set(figure.run_ids)) == 12
    assert set(figure.n_per_group.values()) == {3}
    assert validate_artifact(directory, aggregate, source_ref) == []
    assert plotting_skill.validate({"artifact_dir": directory, "project_root": tmp_path}) == []
    info = Archive(tmp_path, Events(tmp_path)).latest_version("artifacts/figures/fig_test")
    assert info.parents == [source_ref]
    assert info.kind == "figure"
    assert not (tmp_path / "paper").exists()


def test_numeric_defaults_and_scaled_data(aggregate, plot_spec):
    assert plot_spec.kind == "line" and plot_spec.x == "size" and plot_spec.hue == "method"
    rows = list(csv.DictReader(io.StringIO(prepare_data(aggregate, plot_spec))))
    assert [json.loads(r["group.size"]) for r in rows] == [2, 10, 2, 10]
    for record in rows:
        for field in ("mean", "std", "ci95_low", "ci95_high"):
            assert float(record[f"display_{field}"]) == float(record[field]) * 1000
        assert json.loads(record["display_values"]) == [v * 1000 for v in json.loads(record["values"])]
        assert int(record["n"]) == len(json.loads(record["run_ids"])) == 3
    assert prepare_data(aggregate.model_copy(update={"rows": list(reversed(aggregate.rows))}), plot_spec) == (
        prepare_data(aggregate, plot_spec)
    )


@pytest.mark.parametrize("kind", ["line", "bar", "point", "box"])
@pytest.mark.parametrize("error", ["ci95", "std"])
def test_artist_values_and_uncertainty(aggregate, plot_spec, kind, error):
    renderer = load_renderer()
    spec = plot_spec.model_copy(update={"kind": kind, "error": error,
                                       "caption": "n=3; error bars: 95% CI" if error == "ci95"
                                       else "n=3; error bars: standard deviation"})
    rows = renderer.select_rows(aggregate, spec)
    fig = renderer.draw(spec, rows)
    try:
        ax = fig.axes[0]
        expected = [r.mean * spec.scale for r in rows]
        if kind == "line":
            lines = [line for line in ax.lines if line.get_label() in ("A", "B")]
            assert [v for line in lines for v in line.get_ydata()] == expected
            assert all(list(line.get_xdata()) == [2, 10] for line in lines)
        elif kind == "bar":
            assert [p.get_height() for p in ax.patches] == expected
        if kind in ("point", "box"):
            observed = [float(y) for col in ax.collections if isinstance(col, PathCollection)
                        for _x, y in col.get_offsets()]
            assert observed == [v * spec.scale for row in rows for v in row.values]
        segments = [s for col in ax.collections if isinstance(col, LineCollection) for s in col.get_segments()]
        for row, segment in zip(rows, segments, strict=True):
            bounds = row.ci95 if error == "ci95" else [row.mean - row.std, row.mean + row.std]
            assert list(segment[:, 1]) == pytest.approx([v * spec.scale for v in bounds])
    finally:
        plt.close(fig)


def test_categorical_default(aggregate):
    rows = [r.model_copy(update={"group": {"method": r.group["method"]}})
            for r in aggregate.rows if r.group["size"] == 2]
    aggregate = aggregate.model_copy(update={"rows": rows, "comparison": {"group_by": ["method"], "metric": "latency"}})
    spec = default_spec(aggregate, ylabel="Latency (seconds)")
    assert spec.kind == "bar" and spec.hue is None and spec.scale == 1
    assert default_spec(aggregate, ylabel="Latency (seconds)", distribution=True).kind == "point"
    with pytest.raises(ValueError, match="ylabel"):
        default_spec(aggregate)


@pytest.mark.parametrize(("update", "message"), [
    ({"xlabel": ""}, "xlabel"), ({"ylabel": ""}, "ylabel"),
    ({"ylabel": "Latency"}, "unit"), ({"ylabel": "Latency ()"}, "unit"),
    ({"ylabel": "Latency (unknown)"}, "unit"),
    ({"scale": 0}, "scale"), ({"scale": -1}, "scale"), ({"scale": float("inf")}, "scale"),
    ({"scale": float("nan")}, "scale"),
    ({"error": "none"}, "uncertainty"), ({"x": "missing"}, "grouping"),
    ({"hue": "size"}, "distinct"), ({"metric": "absent"}, "no rows"),
    ({"aggregate_id": "wrong"}, "aggregate_id"), ({"fig_id": "../escape"}, "fig_id"),
    ({"caption": "95% CI"}, "actual n"), ({"caption": "n=4; 95% CI"}, "actual n"),
    ({"caption": "n=3"}, "define"), ({"error": "std", "caption": "n=3; 95% CI"}, "standard deviation"),
    ({"x": "method", "hue": "size"}, "numeric x"),
    ({"kind": "bar", "log_x": True}, "log_x"),
])
def test_spec_rejections(aggregate, plot_spec, update, message):
    with pytest.raises(ValueError, match=message):
        prepare_data(aggregate, plot_spec.model_copy(update=update))


@pytest.mark.parametrize(("update", "message"), [
    ({"ci95": None}, "CI is unavailable"), ({"ci95": [2, 1]}, "CI bounds"),
    ({"std": -1}, "std"), ({"mean": float("nan")}, "non-finite"),
    ({"values": [float("inf")] * 3}, "non-finite"),
    ({"n": 4}, "provenance"), ({"run_ids": ["same"] * 3}, "duplicate run"),
    ({"values": [0.01]}, "raw values"), ({"group": {"size": None, "method": "A"}}, "group values"),
])
def test_source_rejections(aggregate, plot_spec, update, message):
    aggregate.rows[0] = aggregate.rows[0].model_copy(update=update)
    with pytest.raises(ValueError, match=message):
        prepare_data(aggregate, plot_spec)


@pytest.mark.parametrize("kind", ["point", "box"])
def test_raw_values_required(aggregate, plot_spec, kind):
    aggregate.rows[0].values = []
    with pytest.raises(ValueError, match="actual per-run"):
        prepare_data(aggregate, plot_spec.model_copy(update={"kind": kind}))


def test_missing_std_exploration_and_hidden_dimensions(aggregate, plot_spec):
    aggregate.rows[0].std = None
    with pytest.raises(ValueError, match="std is unavailable"):
        prepare_data(aggregate, plot_spec.model_copy(update={"error": "std", "caption": "n=3, SD"}))
    aggregate.eval_condition = "explore"
    with pytest.raises(ValueError, match="exploratory"):
        prepare_data(aggregate, plot_spec)
    spec = default_spec(aggregate, ylabel="Latency (s)")
    assert spec.title.startswith("Exploratory")
    aggregate.eval_condition = "final"
    with pytest.raises(ValueError, match="duplicate plotted group|unplotted"):
        prepare_data(aggregate, plot_spec.model_copy(update={"kind": "bar", "x": "method", "hue": None}))


def test_single_observation_no_uncertainty(aggregate, plot_spec):
    row = aggregate.rows[0]
    row = row.model_copy(update={"values": [row.mean], "n": 1, "run_ids": [row.run_ids[0]],
                                "std": None, "ci95": None, "min": row.mean, "max": row.mean})
    aggregate = aggregate.model_copy(update={"rows": [row]})
    spec = plot_spec.model_copy(update={"error": "none", "caption": "One observation, n=1; no error bars."})
    assert prepare_data(aggregate, spec)


def test_scaled_overflow_is_rejected(aggregate, plot_spec):
    for row in aggregate.rows:
        for field in ("mean", "std", "min", "max"):
            setattr(row, field, getattr(row, field) * 1000)
        row.ci95 = [v * 1000 for v in row.ci95]
        row.values = [v * 1000 for v in row.values]
    with pytest.raises(ValueError, match="scaled"):
        prepare_data(aggregate, plot_spec.model_copy(update={"scale": 1e308}))


def test_log_and_multilingual_caption(aggregate, plot_spec):
    spec = plot_spec.model_copy(update={"log_x": True, "caption": "每组 n=3；误差条：95%置信区间。"})
    assert prepare_data(aggregate, spec)
    fig = load_renderer().draw(spec, load_renderer().select_rows(aggregate, spec))
    assert fig.axes[0].get_xscale() == "log"
    plt.close(fig)
    aggregate.rows[0].group["size"] = 0
    with pytest.raises(ValueError, match="log_x"):
        prepare_data(aggregate, spec)


@pytest.fixture
def generated(tmp_path, aggregate, plot_spec, plotting_skill):
    ref = archive_source(tmp_path, aggregate)
    generate_figure(tmp_path, aggregate.aggregate_id, plot_spec, skill=plotting_skill)
    return tmp_path / "artifacts/figures/fig_test", ref


@pytest.mark.parametrize("file", ["spec.json", "data.csv", "plot.py", "figure.json", "fig_test.pdf", "fig_test.png"])
def test_missing_artifact_files(generated, aggregate, file):
    directory, ref = generated
    (directory / file).unlink()
    assert validate_artifact(directory, aggregate, ref)


@pytest.mark.parametrize(("field", "value", "message"), [
    ("run_ids", ["invented"], "run provenance"), ("n_per_group", {"wrong": 99}, "n_per_group"),
    ("script_sha256", "0" * 64, "script_sha256"), ("skill_ref", "wrong@1", "skill_ref"),
    ("outputs", ["elsewhere.pdf"], "output paths"), ("fig_id", "different", "fig_id"),
])
def test_metadata_tampering(generated, aggregate, field, value, message):
    directory, ref = generated
    data = json.loads((directory / "figure.json").read_text(encoding="utf-8"))
    data[field] = value
    atomic_write_json(directory / "figure.json", data)
    assert any(message in p for p in validate_artifact(directory, aggregate, ref))


def test_data_tampering_fails_validator_and_cli(generated, aggregate):
    directory, ref = generated
    data = list(csv.DictReader(io.StringIO((directory / "data.csv").read_text(encoding="utf-8"))))
    data[0]["display_mean"] = "999"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(data[0]))
    writer.writeheader()
    writer.writerows(data)
    (directory / "data.csv").write_text(buf.getvalue(), encoding="utf-8")
    assert any("scale" in p for p in validate_artifact(directory, aggregate, ref))
    result = subprocess.run([sys.executable, str(directory / "plot.py"), "--spec", str(directory / "spec.json"),
                             "--data", str(directory / "data.csv"), "--out", str(directory / "bad")],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 2 and "inconsistent" in result.stderr
    assert not (directory / "bad").exists()


def test_script_tampering_cannot_forge_hash(generated, aggregate):
    directory, ref = generated
    script = directory / "plot.py"
    script.write_text("raise RuntimeError('do not execute validator input')\n", encoding="utf-8")
    metadata = json.loads((directory / "figure.json").read_text(encoding="utf-8"))
    metadata["script_sha256"] = sha256_file(script)
    atomic_write_json(directory / "figure.json", metadata)
    assert any("actual skill" in p for p in validate_artifact(directory, aggregate, ref))


def test_invalid_binaries_and_metadata(generated, aggregate):
    directory, ref = generated
    (directory / "fig_test.pdf").write_bytes(b"invalid PDF")
    (directory / "fig_test.png").write_bytes(b"invalid PNG")
    problems = validate_artifact(directory, aggregate, ref)
    assert any("PDF" in p for p in problems) and any("PNG" in p for p in problems)
    (directory / "figure.json").write_text('{}', encoding="utf-8")
    assert validate_artifact(directory, aggregate, ref)


def test_bad_samples_are_rejected():
    for path in (SKILL / "sample/bad").glob("*.json"):
        assert check_output(path.read_text(encoding="utf-8")), path


def test_embedded_style_matches_template():
    style = matplotlib.rc_params_from_file(SKILL / "templates/air.mplstyle", use_default_template=False)
    for key, value in load_renderer().STYLE.items():
        assert style[key] == value, key


def test_actual_sample_project_runtime_and_replay(sample_project, tmp_path, plotting_skill):
    root = sample_project.root
    aggregate = runs.load_aggregate(root, "C1")
    spec = default_spec(aggregate, fig_id="fig_real", ylabel="Score (dimensionless)")
    spec.title = "Sample-project results"
    generate_figure(root, "C1", spec, skill=plotting_skill)
    directory = root / "artifacts/figures/fig_real"
    assert check_output({"artifact_dir": directory, "project_root": root}) == []
    # Replay outside the project with just the three reproducibility inputs.
    replay = tmp_path / "独立 replay"
    replay.mkdir()
    for name in ("spec.json", "data.csv", "plot.py"):
        shutil.copyfile(directory / name, replay / name)
    subprocess.run([sys.executable, "plot.py", "--spec", "spec.json", "--data", "data.csv", "--out", "."],
                   cwd=replay, check=True, capture_output=True)
    for suffix in ("pdf", "png"):
        assert (replay / f"fig_real.{suffix}").read_bytes() == (directory / f"fig_real.{suffix}").read_bytes()
    archive = sample_project.archive
    previous = archive.latest("artifacts/figures/fig_real")
    generate_figure(root, "C1", spec, skill=plotting_skill)
    assert archive.latest("artifacts/figures/fig_real") == previous


def test_exact_historical_version(tmp_path, aggregate, plot_spec, plotting_skill):
    first = archive_source(tmp_path, aggregate)
    changed = aggregate.model_copy(deep=True)
    changed.comparison["question"] = "A new Aggregate version"
    second = archive_source(tmp_path, changed)
    assert second.version == first.version + 1
    figure = generate_figure(tmp_path, aggregate.aggregate_id, plot_spec, version=first.version, skill=plotting_skill)
    assert figure.aggregate_ref == first
    directory = tmp_path / "artifacts/figures/fig_test"
    assert check_output({"artifact_dir": directory, "project_root": tmp_path}) == []
    metadata = json.loads((directory / "figure.json").read_text(encoding="utf-8"))
    metadata["aggregate_ref"]["sha256"] = "f" * 64
    atomic_write_json(directory / "figure.json", metadata)
    assert check_output({"artifact_dir": directory, "project_root": tmp_path})

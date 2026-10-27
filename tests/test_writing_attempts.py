"""Bounded semantic failures use production Questions and survive engine restarts."""
import json

import pytest

from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.common import VersionRef
from airesearcher.engine.stage import Error
from airesearcher.stages.writing import review
from scripts.writing.demo import SampleWritingLLM, context_of, prepare_sample, run

from .test_writing_stage import restart, to_phase


def setup(tmp_path, monkeypatch, kind, failures):
    p, e = prepare_sample(tmp_path / 'project', compile_enabled=False, evidence_check=True)
    e.llm.backend.semantic_revision = kind == 'revision'
    prompt = review.REVIEW_PROMPT if kind == 'review' else review.REVISE_PROMPT
    original = SampleWritingLLM.call
    seen = []
    def call(self, request):
        response = original(self, request)
        if request.prompt_id == prompt:
            seen.append(context_of(request)['sentence'])
            if len(seen) <= failures:
                response.text = '{malformed'
        return response
    monkeypatch.setattr(SampleWritingLLM, 'call', call)
    to_phase(e, 'verify' if kind == 'review' else 'revise')
    return p, e, seen


def until_question(p, e):
    for _ in range(80):
        tick = e.tick()
        assert not isinstance(tick.result, Error), tick.result
        if e.state == S.Paused:
            return p.questions.pending()
    pytest.fail('no recovery Question')


@pytest.mark.parametrize('kind', ['review', 'revision'])
def test_bad_first_attempt_then_valid_restarts(tmp_path, monkeypatch, kind):
    p, e, seen = setup(tmp_path, monkeypatch, kind, 1)
    tick = e.tick()
    assert not isinstance(tick.result, Error), tick.result
    e = restart(p, e)
    run(e)
    journals = list((p.root / 'paper/.writing').glob('*-attempts.json'))
    attempts = [a for path in journals for a in json.loads(path.read_text(encoding='utf-8'))['attempts']]
    assert sum(a['status'] == 'rejected' for a in attempts) == 1
    assert any(a['status'] == 'accepted' for a in attempts)
    for path in journals:
        history = json.loads(path.read_text(encoding="utf-8"))["attempts"]
        assert [a["number"] for a in history] == list(range(1, len(history) + 1))
        assert all(a.get("call_id") and a.get("raw") for a in history)
    assert len(seen) >= 2


@pytest.mark.parametrize('kind', ['review', 'revision'])
@pytest.mark.parametrize('choice', ['retry', 'skip', 'end'])
def test_exhaustion_question_recovery(tmp_path, monkeypatch, kind, choice):
    p, e, seen = setup(tmp_path, monkeypatch, kind, 2)
    q = until_question(p, e)
    assert {o.id for o in q.options} == {'retry', 'skip', 'end'}
    assert len(seen) == 2
    e = restart(p, e)
    assert p.questions.pending().question_id == q.question_id
    p.questions.answer(q.question_id, choice, request_id=q.question_id)
    e = restart(p, e)
    if choice == 'end':
        e.tick()
        assert e.state == S.Failed
        assert len(seen) == 2
        return
    approval = run(e)
    assert e.state == S.ManuscriptPending
    if choice == 'skip':
        assert seen.count(seen[0]) == 2
        assert 'INCOMPLETE' in approval.summary
        reports = list((p.root / 'paper/review').glob('*.json'))
        assert any('SEMANTIC_INCOMPLETE' in path.read_text(encoding='utf-8') for path in reports)
    else:
        assert len(seen) >= 3
    n = len(seen)
    e = restart(p, e)
    e.tick()
    assert len(seen) == n


@pytest.mark.parametrize('relative', ['claims.jsonl', 'air_values.tex'])
def test_reconciliation_restores_exact_bytes_and_keeps_backup(tmp_path, relative):
    p, e = prepare_sample(tmp_path / 'project', compile_enabled=False, evidence_check=True)
    to_phase(e, 'compile')
    path = p.root / 'paper' / relative
    original = path.read_bytes()
    edited = original + b'% human edit\n'
    path.write_bytes(edited)
    q = until_question(p, e)
    assert path.read_bytes() == edited
    assert {o.id for o in q.options} == {'restore', 'end'}
    e = restart(p, e)
    p.questions.answer(q.question_id, 'restore', request_id=q.question_id)
    e = restart(p, e)
    e.tick()
    assert path.read_bytes() == original
    recovery = json.loads((p.root / 'paper/.writing/reconcile.json').read_text(encoding='utf-8'))
    assert bytes.fromhex(recovery['before']['paper/' + relative]) == edited
    assert p.archive.get(VersionRef.model_validate(recovery['backup_ref']))
    run(e)
    assert e.state == S.ManuscriptPending


def test_font_fallback_names_are_optional_and_ordered(monkeypatch):
    from types import SimpleNamespace

    from airesearcher.stages.writing.figures import load_renderer
    plot = load_renderer()
    monkeypatch.setattr(plot.font_manager.fontManager, 'ttflist', [])
    assert plot.presentation_style() == plot.STYLE
    monkeypatch.setattr(plot.font_manager.fontManager, 'ttflist', [
        SimpleNamespace(name='PingFang SC'), SimpleNamespace(name='Noto Sans CJK SC')])
    assert plot.presentation_style()['font.family'] == ['DejaVu Sans', 'Noto Sans CJK SC', 'PingFang SC']


@pytest.mark.parametrize('number', ['3 seeds', 'n = 8', 'mean 0.5', 'std 0.02', 'CI 0.1--0.2',
                                   'p = 0.04', 'effect size 0.8', 'failure rate 5 percent', 'score 42', '2026 samples'])
def test_generated_measurements_cannot_bypass_value_refs(tmp_path, monkeypatch, number):
    p, e = prepare_sample(tmp_path / 'project', compile_enabled=False, evidence_check=True)
    to_phase(e, 'write')
    original = SampleWritingLLM.call
    def literal(self, request):
        response = original(self, request)
        if request.prompt_id == 'writing/section':
            data = json.loads(response.text)
            data['latex'] += '\nObserved ' + number + '.'
            response.text = json.dumps(data)
        return response
    monkeypatch.setattr(SampleWritingLLM, 'call', literal)
    tick = e.tick()
    assert isinstance(tick.result, Error)
    assert 'ValueRef' in tick.result.message
    assert not (p.root / 'paper/sections/introduction.tex').exists()


def test_unreceipted_section_regenerates_only_after_explicit_answer(tmp_path):
    p, e = prepare_sample(tmp_path / 'project', compile_enabled=False, evidence_check=True)
    to_phase(e, 'write')
    path = p.root / 'paper/sections/introduction.tex'
    path.parent.mkdir(exist_ok=True, parents=True)
    original = b'Human file without a receipt.\n'
    path.write_bytes(original)
    q = until_question(p, e)
    assert path.read_bytes() == original
    e = restart(p, e)
    p.questions.answer(q.question_id, 'restore', request_id=q.question_id)
    e = restart(p, e)
    run(e)
    assert e.state == S.ManuscriptPending
    assert path.read_bytes() != original
    record = json.loads((p.root / 'paper/.writing/reconcile.json').read_text(encoding='utf-8'))
    assert bytes.fromhex(record['before']['paper/sections/introduction.tex']) == original


@pytest.mark.parametrize('kind', ['review', 'revision'])
@pytest.mark.parametrize('boundary', ['returned', 'accepted'])
def test_completed_attempt_crash_never_repeats_call(tmp_path, monkeypatch, kind, boundary):
    from airesearcher.stages.writing import stage
    p, e, seen = setup(tmp_path, monkeypatch, kind, 0)
    original = stage._save
    failed = []
    def crash(ctx, name, value):
        original(ctx, name, value)
        if name.endswith('-attempts') and value['attempts'][-1]['status'] == boundary and not failed:
            failed.append(name)
            raise OSError('completed response saved before checkpoint')
    monkeypatch.setattr(stage, '_save', crash)
    assert isinstance(e.tick().result, Error)
    assert len(seen) == 1
    monkeypatch.setattr(stage, '_save', original)
    e.request_action('resume')
    e = restart(p, e)
    run(e)
    assert e.state == S.ManuscriptPending
    assert seen.count(seen[0]) == 1
    journal = json.loads((p.root / f'paper/.writing/{failed[0]}.json').read_text(encoding='utf-8'))
    assert len(journal['attempts']) == 1
    assert journal['attempts'][0]['status'] == 'accepted'
    assert journal['attempts'][0]['call_id']
    assert journal['attempts'][0]['raw']


@pytest.mark.parametrize('kind', ['review', 'revision'])
def test_explicit_retry_is_one_call_and_budget_survives_restart(tmp_path, monkeypatch, kind):
    p, e, seen = setup(tmp_path, monkeypatch, kind, 99)
    q = until_question(p, e)
    assert len(seen) == 2
    p.questions.answer(q.question_id, 'retry', request_id=q.question_id)
    e = restart(p, e)
    q2 = until_question(p, e)
    assert q2.question_id != q.question_id and len(seen) == 3
    e = restart(p, e)
    e.tick()
    assert len(seen) == 3 and p.questions.pending().question_id == q2.question_id


def test_reconciliation_rejects_intervening_edit_after_question(tmp_path):
    p, e = prepare_sample(tmp_path / 'project', compile_enabled=False, evidence_check=True)
    to_phase(e, 'compile')
    path = p.root / 'paper/air_values.tex'
    path.write_bytes(path.read_bytes() + b'% first edit')
    q = until_question(p, e)
    second = path.read_bytes() + b'% second edit'
    path.write_bytes(second)
    p.questions.answer(q.question_id, 'restore', request_id=q.question_id)
    e = restart(p, e)
    tick = e.tick()
    assert isinstance(tick.result, Error)
    assert path.read_bytes() == second


def test_reconciliation_cannot_target_project_configuration(tmp_path):
    p, e = prepare_sample(tmp_path / 'project', compile_enabled=False, evidence_check=True)
    original = (p.root / 'project.yaml').read_bytes()
    target = p.root / 'paper/.writing/reconcile.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({'reason': 'forged', 'targets': {'project.yaml': '00'},
                                 'before': {'project.yaml': original.hex()}}), encoding='utf-8')
    result = e.tick().result
    assert isinstance(result, Error)
    assert 'Writing-owned' in result.message
    assert (p.root / 'project.yaml').read_bytes() == original

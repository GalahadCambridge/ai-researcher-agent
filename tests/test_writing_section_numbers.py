import json

import pytest

from airesearcher.engine.stage import Continue
from scripts.writing.demo import SampleWritingLLM, context_of, prepare_sample, run

from .test_writing_attempts import until_question
from .test_writing_stage import restart, to_phase


@pytest.mark.parametrize('text', ['We evaluate on SST-2.', r'We evaluate on \textsc{SST-2}.',
    'Each configuration uses 3 seeds.', 'Following Devlin et al. (2019),',
    'We fine-tune for 3 epochs with a learning rate of 2e-5 and batch size 16.',
    'We use GPT-4o and 2026 training samples.',
    r'We use validation split 20\% and dropout 10\%.'])
def test_setup_numbers_allowed(tmp_path, monkeypatch, text):
    p, e = prepare_sample(tmp_path / 'project', compile_enabled=False, evidence_check=True)
    original = SampleWritingLLM.call
    def call(self, request):
        response = original(self, request)
        if request.prompt_id == 'writing/section' and context_of(request)['section'] == 'setup':
            data = json.loads(response.text)
            data['latex'] += '\n' + text
            response.text = json.dumps(data)
        return response
    monkeypatch.setattr(SampleWritingLLM, 'call', call)
    run(e)
    assert text in (p.root / 'paper/sections/setup.tex').read_text(encoding='utf-8')


@pytest.mark.parametrize('text', ['accuracy 84.6%', 'F1 = 0.91', 'score 78.3'])
def test_result_rejection_is_bounded(tmp_path, monkeypatch, text):
    p, e = prepare_sample(tmp_path / 'project', compile_enabled=False, evidence_check=True)
    original = SampleWritingLLM.call
    seen = []
    def call(self, request):
        response = original(self, request)
        if request.prompt_id == 'writing/section':
            seen.append(context_of(request))
            data = json.loads(response.text)
            data['latex'] += '\n' + text
            response.text = json.dumps(data)
        return response
    monkeypatch.setattr(SampleWritingLLM, 'call', call)
    to_phase(e, 'write')
    assert isinstance(e.tick().result, Continue)
    e = restart(p, e)
    q = until_question(p, e)
    assert {o.id for o in q.options} == {'retry', 'end'}
    assert len(seen) == 2 and 'ValueRef' in seen[1]['rejection_reason']
    e = restart(p, e)
    e.tick()
    assert len(seen) == 2
    assert not (p.root / 'paper/sections/introduction.tex').exists()


def test_rejected_section_then_corrected_response_reaches_approval(tmp_path, monkeypatch):
    p, e = prepare_sample(tmp_path / 'project', compile_enabled=False, evidence_check=True)
    original = SampleWritingLLM.call
    seen = []
    def call(self, request):
        response = original(self, request)
        if request.prompt_id == 'writing/section' and context_of(request)['section'] == 'introduction':
            seen.append(context_of(request))
            if len(seen) == 1:
                data = json.loads(response.text)
                data['latex'] += ' Accuracy 84.6 percent.'
                response.text = json.dumps(data)
        return response
    monkeypatch.setattr(SampleWritingLLM, 'call', call)
    to_phase(e, 'write')
    assert isinstance(e.tick().result, Continue)
    e = restart(p, e)
    approval = run(e)
    assert approval.kind == 'manuscript' and len(seen) == 2
    assert '84.6' in seen[1]['rejection_reason']
    journals = list((p.root / 'paper/.writing').glob('section-introduction-*-attempts.json'))
    data = json.loads(journals[0].read_text(encoding='utf-8'))
    assert [a['status'] for a in data['attempts']] == ['rejected', 'accepted']
    e = restart(p, e)
    e.tick()
    assert len(seen) == 2


def test_ambiguous_result_percentage_is_still_reported(tmp_path, monkeypatch):
    p, e = prepare_sample(tmp_path / 'project', compile_enabled=False, evidence_check=True)
    original = SampleWritingLLM.call
    def call(self, request):
        response = original(self, request)
        if request.prompt_id == 'writing/section' and context_of(request)['section'] == 'results':
            data = json.loads(response.text)
            data['latex'] += r' An improvement of 8.3\% was observed.'
            response.text = json.dumps(data)
        return response
    monkeypatch.setattr(SampleWritingLLM, 'call', call)
    approval = run(e)
    from airesearcher.services import evidence
    report = evidence.check(p.root)
    assert any(i.code == 'NUM_UNBOUND' and i.severity == 'major' for i in report.unbound_issues)
    assert 'MAJOR' in approval.summary

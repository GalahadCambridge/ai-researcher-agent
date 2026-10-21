"""Check spending admission, durable reservations, model allowlists, and interrupted-call accounting."""

import json

import pytest

from airesearcher.llm.gateway import LLMRequest, RawCompletion
from scripts.literature.budgeted import BudgetedBackend, SpendingLimit


class Backend:
    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail

    def call(self, request):
        self.calls += 1
        if self.fail:
            raise TimeoutError("Simulated timeout; no network request.")
        return RawCompletion(text="{}", input_tokens=10, output_tokens=10, usd=0.0001)


def request(**kwargs):
    return LLMRequest(role="idea.parse", prompt_id="idea/parse_input", prompt_version=1,
                      model="openrouter/google/gemini-2.5-pro", messages=[{"role": "user", "content": "Test"}],
                      max_tokens=8192, api_base="https://openrouter.ai/api/v1", **kwargs)


def test_budget_survives_new_backend_instances_and_stops_before_call(tmp_path):
    ledger = tmp_path / "budget.json"
    backend = Backend()
    wrapper = BudgetedBackend(ledger, cap=0.2, backend=backend)
    wrapper.call(request())
    assert backend.calls == 1
    resumed = BudgetedBackend(ledger, cap=0.2, backend=backend)
    with pytest.raises(SpendingLimit):
        resumed.call(request())
    assert backend.calls == 1 and len(resumed.read()["calls"]) == 1
    assert resumed.committed(resumed.read()) <= 0.2


def test_unknown_failure_cost_keeps_its_reservation(tmp_path):
    wrapper = BudgetedBackend(tmp_path / "budget.json", cap=0.2, backend=Backend(fail=True))
    with pytest.raises(RuntimeError, match="reservation is retained"):
        wrapper.call(request())
    ledger = wrapper.read()
    assert ledger["calls"][0]["status"] == "failed_unknown_cost"
    assert wrapper.committed(ledger) > 0
    assert not (tmp_path / "budget.lock").exists()


def test_unknown_model_and_large_context_are_blocked_without_calls(tmp_path):
    backend = Backend()
    wrapper = BudgetedBackend(tmp_path / "budget.json", backend=backend)
    req = request()
    req.model = "unknown/model"
    with pytest.raises(SpendingLimit):
        wrapper.call(req)
    req = request()
    req.messages = [{"role": "user", "content": "A" * 100_001}]
    with pytest.raises(SpendingLimit):
        wrapper.call(req)
    assert backend.calls == 0


def test_existing_lock_requires_manual_intervention(tmp_path):
    backend = Backend()
    wrapper = BudgetedBackend(tmp_path / "budget.json", backend=backend)
    (tmp_path / "budget.lock").write_text("Interrupted process", encoding="utf-8")
    with pytest.raises(SpendingLimit, match="budget lock"):
        wrapper.call(request())
    assert backend.calls == 0 and (tmp_path / "budget.lock").exists()


def test_mismatched_cap_does_not_reset_existing_ledger(tmp_path):
    ledger = tmp_path / "budget.json"
    ledger.write_text(json.dumps({"cap_usd": 2, "calls": []}), encoding="utf-8")
    wrapper = BudgetedBackend(ledger, cap=1, backend=Backend())
    with pytest.raises(SpendingLimit):
        wrapper.call(request())
    assert json.loads(ledger.read_text())["cap_usd"] == 2


@pytest.mark.parametrize("cap", [0, -1, 2.01, float("inf"), float("nan")])
def test_validation_runner_cannot_raise_user_budget(tmp_path, cap):
    with pytest.raises(ValueError):
        BudgetedBackend(tmp_path / "budget.json", cap=cap)


def test_unknown_cost_prevents_another_remote_attempt_even_with_available_budget(tmp_path):
    backend = Backend(fail=True)
    wrapper = BudgetedBackend(tmp_path / "budget.json", cap=2, backend=backend)
    with pytest.raises(RuntimeError):
        wrapper.call(request())
    with pytest.raises(SpendingLimit, match="unknown cost"):
        wrapper.call(request())
    assert backend.calls == 1


def test_settlement_keeps_actual_cost_margin_and_original_reservation_history(tmp_path):
    wrapper = BudgetedBackend(tmp_path / "budget.json", cap=0.2, backend=Backend())
    wrapper.call(request())
    before = wrapper.committed(wrapper.read())
    ledger = wrapper.settle_completed(0.001)
    assert 0.001 < wrapper.committed(ledger) < before
    assert ledger["calls"][0]["reserved_usd"] == before
    assert ledger["calls"][0]["settled"]
    assert ledger["settlements"][0]["provider_key_usage_usd"] == 0.001


def test_unknown_cost_cannot_be_settled(tmp_path):
    wrapper = BudgetedBackend(tmp_path / "budget.json", backend=Backend(fail=True))
    with pytest.raises(RuntimeError):
        wrapper.call(request())
    with pytest.raises(SpendingLimit, match="Unknown request costs"):
        wrapper.settle_completed(0)

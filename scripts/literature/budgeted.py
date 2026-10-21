"""Conservative, persistent spending reservations for opt-in OpenRouter validation runs."""

from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path

from airesearcher.core.fsutil import atomic_write_json, now
from airesearcher.llm.gateway import LiteLLMBackend

RATES = {
    "openrouter/google/gemini-2.5-flash": (0.30, 2.50),
    "openrouter/google/gemini-2.5-pro": (1.25, 10.0),
}


class SpendingLimit(RuntimeError):
    """The next request cannot be safely admitted within the configured spending limit."""


class BudgetedBackend:
    """Wrap the real backend; every gateway repair or transport attempt shares one ledger."""

    def __init__(self, ledger: Path, cap=2.0, backend=None):
        if not math.isfinite(cap) or cap <= 0 or cap > 2:
            raise ValueError("Validation spending limits must be positive and no greater than USD 2.")
        self.ledger, self.cap = Path(ledger), cap
        self.backend = backend or LiteLLMBackend()
        self.lock = threading.Lock()

    def read(self):
        if self.ledger.exists():
            result = json.loads(self.ledger.read_text(encoding="utf-8"))
            if result.get("cap_usd") != self.cap or not isinstance(result.get("calls"), list):
                raise SpendingLimit("The existing ledger has a different cap or invalid structure; review it manually.")
            return result
        return {"cap_usd": self.cap, "calls": []}

    @staticmethod
    def committed(ledger):
        return ledger.get("settled_usd", 0) + sum(
            call["reserved_usd"] for call in ledger["calls"] if not call.get("settled"))

    def call(self, req):
        if req.model not in RATES or req.tools or req.api_base != "https://openrouter.ai/api/v1":
            raise SpendingLimit("This validation runner only supports the two configured text-only OpenRouter models.")
        if req.params.get("n", 1) != 1 or not 1 <= req.max_tokens <= 8192:
            raise SpendingLimit("Only one completion with at most 8192 output tokens is permitted.")
        # UTF-8 bytes conservatively bound text tokenization; additional margin covers message framing.
        size = len(json.dumps(req.messages, ensure_ascii=False).encode())
        if size > 100_000:
            raise SpendingLimit("The validation input exceeds the conservative short-context bound.")
        pin, pout = RATES[req.model]
        estimate = ((size + 4096) * pin + req.max_tokens * pout) / 1_000_000
        # Reserve twice the configured maximum-token estimate, including billed reasoning.
        reserved = round(2 * estimate, 6)
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.ledger.with_suffix(".lock")
        with self.lock:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                raise SpendingLimit("A budget lock exists; review the other or interrupted run manually.") from None
            os.close(fd)
            try:
                ledger = self.read()
                if any(c["status"] != "done" for c in ledger["calls"]):
                    raise SpendingLimit("A prior call has unknown cost; review it before another paid request.")
                if self.committed(ledger) + reserved > self.cap:
                    raise SpendingLimit("The next request and its reserve would exceed the cumulative USD limit.")
                call = {"started_at": now().isoformat(), "prompt_id": req.prompt_id, "model": req.model,
                        "status": "started", "reserved_usd": reserved}
                ledger["calls"].append(call)
                atomic_write_json(self.ledger, ledger)
                try:
                    raw = self.backend.call(req)
                except Exception as exc:
                    call.update(status="failed_unknown_cost", error_type=type(exc).__name__)
                    atomic_write_json(self.ledger, ledger)
                    # Keep the full reservation; never assume a failed remote request was free.
                    message = f"Model request failed ({type(exc).__name__}); its reservation is retained."
                    raise RuntimeError(message) from None
                estimated_actual = (raw.input_tokens * pin + raw.output_tokens * pout) / 1_000_000
                actual = max(raw.usd or 0, estimated_actual)
                if not math.isfinite(actual) or actual < 0:
                    raise SpendingLimit("The provider returned invalid usage; its full reservation is retained.")
                call.update(status="done", measured_usd=round(actual, 6),
                            reserved_usd=max(reserved, round(actual, 6)),
                            input_tokens=raw.input_tokens, output_tokens=raw.output_tokens)
                # Retain the conservative reservation even on success. This bounds future requests
                # without assuming provider usage reports or reasoning accounting are complete.
                atomic_write_json(self.ledger, ledger)
                return raw
            finally:
                lock_path.unlink()


    def settle_completed(self, provider_usage_usd):
        """Release completed-call reservations after comparing provider usage and recorded token costs."""
        if not math.isfinite(provider_usage_usd) or provider_usage_usd < 0:
            raise ValueError("Provider usage must be finite and nonnegative")
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.ledger.with_suffix(".lock")
        with self.lock:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                raise SpendingLimit("An active or interrupted request must be reviewed before settlement.") from None
            os.close(fd)
            try:
                ledger = self.read()
                if any(c["status"] != "done" for c in ledger["calls"]):
                    raise SpendingLimit("Unknown request costs cannot be settled automatically.")
                measured = sum(c["measured_usd"] for c in ledger["calls"])
                # Use the entire key's reported lifetime usage, without subtracting a baseline.
                # Retain an additional 50% plus one cent over the larger recorded amount.
                settled = round(max(provider_usage_usd, measured) * 1.5 + 0.01, 6)
                ledger["settled_usd"] = max(ledger.get("settled_usd", 0), settled)
                for call in ledger["calls"]:
                    call["settled"] = True
                ledger.setdefault("settlements", []).append({
                    "at": now().isoformat(), "provider_key_usage_usd": provider_usage_usd,
                    "token_cost_estimate_usd": round(measured, 6), "settled_with_margin_usd": ledger["settled_usd"],
                    "note": "All replies completed. Original reservations remain in call history.",
                })
                atomic_write_json(self.ledger, ledger)
                return ledger
            finally:
                lock_path.unlink()

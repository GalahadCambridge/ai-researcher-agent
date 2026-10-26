# Experiment module: requirement alignment

Authority: docs/AI_Researcher_Agent_需求分析.md; docs/详细设计/3_实验计划与执行_详细设计.md;
docs/开工指南/实验.md. The user subsequently required retaining the existing configuration
schema and stopping on unsupported configurations. No research novelty feature is added.

PR review follow-up (2026-10-23): byte-exact UTF-8 environment locks; best-effort,
permission-checked inspection; explicit uncertain-submission recovery; task-declared
method aliases, accelerator requirements and trial overrides; cancelled expired
coding operations; session-local gateway budget adaptation; process-identity checks
and detached data workers; cached planning hashes with fresh submission verification.
Production stages no longer import concrete testing backend classes.
Final Ubuntu/Python 3.12 offline regression: 481 passed, 6 opt-in paid tests skipped,
0 failed (384.96 s), with one upstream Pydantic warning. Ruff, all five import
contracts, skill verification, pip check and diff checks passed. No paid API calls
or classifier training were performed. Windows/macOS reruns remain CI work after
the revision is pushed; the protected configuration schema is unchanged.

Review follow-up (2026-10-25): when an expired coding dispatch prevents cleanup,
the worker exception carries its session-start source/configuration snapshot.
The engine restores that snapshot directly before offering retry, reports the
reverted files, and retains the recovery task if rollback itself fails. Rejected
edits are not accepted as a user-confirmed version or used by subsequent trials.
The regression reproduces real FakeLLM file edits followed by a paused trial;
it failed before the fix and now verifies byte-exact restoration and later run
code snapshots. Related offline regression: 48 passed, no paid model calls or
classifier training. Full-suite validation for this revision is recorded separately.

Final Ubuntu/Python 3.12 regression for the rollback fix: 483 passed, 6 opt-in paid
tests skipped, 0 failed (401.31 s), with one upstream Pydantic warning. The updated
CRLF/cleanup-retry regression also passed both cases. Ruff, five import contracts,
skill verification, pip check and diff checks passed. The reviewer reported all
seven CI checks green for 2115628; this new revision still needs a CI rerun after push.

| Requirements | Implementation | Validation boundary |
|---|---|---|
| FR-20–23 | Exact approved idea; planning skill and existing LLMGateway.complete; Cartesian tasks; prechecks; two bounded revisions; versioned plan approval and explicit risk acceptance | FakeLLM regression and a live OpenRouter plan; unknown licenses remain warnings |
| FR-24–25 | Evidence-bound follow-up proposals; existing tool_loop coding tools; generated configurations; semantic guard and rollback | No schema extension: unsupported configurations stop, as requested |
| FR-26–29 / FR-62 | Local executor/wrapper; trial gates; interpreter-specific static/dependency checks; code/config/data/environment snapshots; run kinds and eval conditions | FakeExecutor regression plus two trials and six formal CUDA runs; independent clean-machine reproduction is not claimed |
| FR-30 | Per-error retry limits; deterministic OOM, timeout and lost handling; data revalidation; coder repairs; explicit dependency/manual repair questions | Fake injected errors validate control flow, not arbitrary real-model repair quality |
| FR-31 | Existing status/log/cancel/reconcile services; background collection without new submissions or repair | Standard-library smoke subprocess tests and live local classifier scheduling |
| FR-32 / FR-37 | Canonical comparison/all JSON/CSV/Markdown; sample std, t CI, Welch, 3-MAD flags, exclusions, labels, resource costs and archived run parents | Small-sample statistics remain descriptive; raw evidence is preserved |
| FR-34–36 / FR-54 | AnalysisReport schema; numeric observation verification; evidence levels, hypothesis statuses, anomaly diagnosis, bounded replication, budget/round/no-progress stops; important-log approval | Ordinary continuation is automatic; critical decisions and writing handoff require human approval |
| Detailed design §15.2 | eval/metrics/experiment_capability.py | Human-rated usefulness and injected-anomaly labels require explicit annotations; absent labels produce null |
| Detailed design §15.1 | eval/metrics/experiment_pilot.py | Separates role costs, approval waits, pauses and run durations; the experiment-only pilot does not include live literature or paper-writing costs |
| Detailed design §15.3 | English experiment-loop report draft and independent-reproduction package under environments/alignment/ | The writing owner must independently execute and sign the reproduction; a package is not a reproduction result |

Canonical output paths are plan/, runs/, artifacts/aggregates/, logs/round_<n>.md and
environments/. The writing owner reads the existing services.runs API. Old custom
result manifests, reports and replication proposals are no longer production outputs.
Historical approved artifacts are retained for audit; they are not silently rewritten.

Offline acceptance evidence remains under local_results/current_validation/requirements_fake/.
See local_results/current_validation/validation.json for those test results and limitations.
Offline acceptance uses FakeLLM, costs USD 0, and submits no real classifier training.
Real API mode is implemented behind the explicit `experiment.llm_backend: api` selection;
the default remains `fake`. Production LLMGateway/LiteLLMBackend transport is replaced by
local FakeLLM responses in adapter tests, covering role routing, parsing/retries, tool calls,
budget accounting/stops and canonical handoff.

On 2026-10-23, separately authorized live validation used OpenRouter Gemini 2.5 Pro
and LocalExecutor on an NVIDIA GeForce RTX 4060 Laptop GPU. It reused the exact
approved integration-test idea, generated a real plan, received explicit human
approval, and completed two trials plus six formal full_ft/LoRA runs. Prediction
metrics and aggregates were independently recomputed and the writing-reader APIs
were checked. Live interpretation and its format retry are recorded separately.
Each plan and important round-log approval remains a human decision.

The human-approved round-log v2 reached Writing without starting a writing job.
The pre-alignment regression after the stop-rule correction passed 458 tests, with six
opt-in tests skipped; Ruff and all five import contracts passed. The cumulative
recorded token-cost estimate was USD 0.142262, including earlier attempts. See
environments/complete_validation.md for the completed acceptance report.

Live evidence is in `${AIR_HOME}/projects/p-20261023-58db/`: canonical plan/, runs/,
artifacts/aggregates/ and logs/, with validation state and costs in
environments/complete_validation.json and result checks in
environments/complete_result_verification.json. The user authorized a cumulative
USD 1 cap for this isolated validation, including earlier failed attempts. Original
reservations, responses and superseded logs are retained. This cap does not change
the repository defaults or the user's global model configuration.

The live test exposed and corrected missing task-schema context, unspecified
evidence-object prompting, and a decision path that could request more tuning
after a completed negative comparison without outstanding anomalies. Final
completion now enforces design section 9.5 while retaining anomaly-driven,
plan-authorized replication. New prompts retain their previous versions.

The upstream idea still carries simulated-literature warnings. Real execution
does not validate those literature claims. A subsequent, explicitly authorized
alignment check injected one runtime fault into an isolated LoRA source copy.
The real exp.coder used the existing tool loop to remove it and complete a CUDA
trial. The original methods, approved plan, approved log and Writing state were
preserved. This controlled repair does not establish arbitrary model repair quality.

The alignment check corrected the remaining model-input discrepancies in detailed
design sections 7.3 and 9.3: the coder receives the target method source, explicit
workspace-relative path instructions and approved trial keys; the analyst receives
the exact archived Markdown tables alongside structured statistics. Prompt history
is preserved in coder v1/v2/v3 and analyze v1/v2/v3. A shadow real-API analyst call
validated the Markdown input without replacing the approved research log.

The live tool test also exposed a missing SDK extra: LiteLLM 1.106.2 imports MCP
support for function tools. The existing optional llm dependency now requests
litellm[mcp]. The air validation environment was repaired explicitly; coding agents
still cannot install dependencies. A network-forbidden SDK initialization test
covers this dependency path without sending model requests.

The cumulative recorded token-cost estimate after alignment is USD 0.299121,
including earlier unsuccessful sessions; the USD 1 cap is unchanged. The local
dependency error occurred before transport and is reconciled in the retained
ledger. Final counts and evidence are in environments/alignment/requirements_alignment.json.
The final alignment regression passed 467 tests with six opt-in skips; Ruff, all
five import contracts and pip check passed. One upstream Pydantic warning occurred
in the network-forbidden mocked SDK test.
The original first-Writing pilot remains USD 0.142262; later validation is separate.

The user rated one historical follow-up rationale as reasonable. This is one
explicit human label, not approval of that superseded follow-up or a general quality
score. Three controlled positive anomaly cases were detected, with no false alarm
in two negative controls; one positive uses a temporary train/test contamination.
Capability metrics exclude dependency-installation failures from the repairable
failure denominator. Unassessed metrics remain null.

SSH and automatic dependency installation are explicitly out of scope; the non-NLP
example is optional in the design. Full-project trajectory costing, comparison-run
counts and a different team member's reproduction remain team evaluation work.
See EVALUATION_HANDOFF.md for the prepared evidence and the exact remaining actions.

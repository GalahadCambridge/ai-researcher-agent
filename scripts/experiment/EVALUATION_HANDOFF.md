# Experiment evaluation handoff

Authority: detailed design 3, sections 15.1–15.3, and requirements section 8.2.
The implementation and evidence concern the experiment-planning/execution owner.
No literature-owner code or paper-writing stage is implemented here.

The authorized validation workspace is `/home/plj/air/projects/p-20261023-58db`.
Its approved plan, eight original run attempts, aggregates and approved round log
remain canonical. Additional acceptance evidence is under `environments/alignment/`;
it is not a replacement research-output format.

## Evidence ready for review

| Item | Evidence | Scope |
|---|---|---|
| Input-format alignment | `requirements_alignment.json`, source tests and recorded requests | Preloaded method source, exact archived Markdown aggregate tables, explicit tool paths/trial keys |
| Real coding repair | `coder_result.json`, `coder_trials/`, `live_alignment_validation.json` | One injected runtime fault repaired through the existing LLMGateway; real small CUDA trial |
| Real Markdown analysis | `shadow_analysis.json`, `request_16.json`, `response_16.json` | Parsed and evidence-checked shadow analysis; does not replace the human-approved log |
| Cost pilot | `experiment_pilot.json/.md` | Experiment-only interval through the first Writing handoff; later validation costs excluded |
| Human follow-up rating | `annotations.json`, `followup_for_human_review.md` | User explicitly rated one historical distinguishes rationale reasonable; no new experiment approved |
| Controlled anomaly checks | `controlled_anomaly_evaluation.json` | Three positive injections detected, two negative controls; not a general detection benchmark |
| Independent reproduction | `independent_reproduction.zip`, `INDEPENDENT_REPRODUCTION.md` | Prepared six-run source/configuration/reference package; awaiting a different team member's execution |
| Course-report contribution | `EXPERIMENT_LOOP_REPORT.md` | English evidence-based draft of the experiment-loop section; remaining team evaluations marked pending |

## Recompute evaluation without model calls

From the repository root, in the configured Python environment:

```bash
python -m eval.metrics.experiment_pilot \
  --workspace /home/plj/air/projects/p-20261023-58db \
  --annotations /home/plj/air/projects/p-20261023-58db/environments/alignment/annotations.json \
  --out /home/plj/air/projects/p-20261023-58db/environments/alignment/experiment_pilot.json
```

For a new project, supply its own workspace and actual annotations. Empty annotation
lists produce null, never a fabricated success. The failure-recovery denominator
includes the documented repairable categories; manual dependency installation and
cancelled runs are excluded. The pilot excludes inherited LLM records and ends at
the first Writing transition. Approval waiting means elapsed pending-state time,
not measured human labour.

## Remaining team activities

The writing owner must choose and record acceptable accuracy/macro-F1 differences
before running, independently execute the packaged six runs, and sign the results.
This is explicitly a different-person check in section 15.3. Do not relabel the
original author's results as independent reproduction.

The team lead must combine the experiment pilot with actual literature and writing
costs and choose the main comparison's trajectory count (section 15.1). The available
pilot used simulated-literature input and developer interventions, so it cannot be
presented as an unbiased complete idea-to-paper cost estimate. The user-selected
USD 1 acceptance cap is unrelated to that future evaluation budget.

Keep unknown dataset licensing, simulated-literature provenance, small sample size,
and the narrow controlled-repair scope visible when assembling the final report.

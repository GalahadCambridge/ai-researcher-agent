# Literature development and evaluation tools

These commands run from a complete repository checkout after installing `.[dev]`.
They are not imported by the production idea stage or installed as part of the
`airesearcher` package. The runtime stage remains under `airesearcher/stages/idea/`.

- `demo.py`: fixture models for free workflow demonstrations, optionally with real retrieval.
- `evaluation.py`: offline metrics and pending human-annotation templates.
- `quality.py`: read-only auditing of frozen replies and evidence.
- `recovery.py`: explicitly invoked helpers for reusing prior validation artifacts.
- `live.py`: opt-in LoRA validation with fixed model profiles and query repair.
- `budgeted.py`: conservative reservations for that paid validation runner only;
  it does not replace or reset the project's core budget.

Free example from the repository root (Windows PowerShell):

```powershell
$demoRoot = Join-Path $env:USERPROFILE ("air\d-" + (Get-Date -Format "yyyyMMddHHmmss"))
python -m scripts.literature.demo --workspace "$demoRoot" --read-papers 2
```

macOS / Linux (bash):

```bash
demo_root="$HOME/air/d-$(date +%Y%m%d%H%M%S)"
python -m scripts.literature.demo --workspace "$demo_root" --read-papers 2
```

The resulting draft is simulated and waits for approval. It is not a real-model
quality result or evidence that LoRA training was performed.

Offline entry points (Windows PowerShell):

```powershell
python -m scripts.literature.evaluation --help
python -m scripts.literature.quality --help
python eval/metrics/literature_compare.py --help
```

macOS / Linux (bash):

```bash
python -m scripts.literature.evaluation --help
python -m scripts.literature.quality --help
python eval/metrics/literature_compare.py --help
```

`live.py` can incur API charges. Its persistent validation ledger retains the same
US$2 cap across invocations; do not replace it to reset spending. The core project
budget still applies. This documentation does not authorize a paid run.

The former `python -m airesearcher.stages.idea.<tool>` paths have moved to
`python -m scripts.literature.<tool>`. See the current handoff checklist under
`docs/开工指南/文献/` for setup, inputs, outputs, and honest evaluation limits.

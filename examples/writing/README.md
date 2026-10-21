# Writing functional examples

Each completed Writing feature phase should retain a viewable example in a separate
stage directory and update this index. Each example includes a **real compiled PDF**,
the necessary source inputs, the actual compile report, and reproduction instructions.
Later phases add directories rather than overwrite historical examples. Earlier
phases are not retroactively populated by this change.

| Phase | PDF | Stage notes and reproduction |
| --- | --- | --- |
| Phase 2A-4 Writer, after independent-review fixes | [main.pdf](phase-2a-4-writer/main.pdf) | [README](phase-2a-4-writer/README.md) |
| Phase 2A-5 real control-arm WritingStage | [main.pdf](phase-2a-5-stage/main.pdf) | [README](phase-2a-5-stage/README.md) |

Examples demonstrate functionality and reproducibility. They introduce no production
configuration or global mechanism. Phase 2A-5 exercises the actual Stage. Use synthetic data, public
samples, or explicitly authorized materials. Never commit secrets, private inputs,
account information, machine configuration, whole temporary projects, raw runs,
archives, LLM logs, caches, or intermediate TeX build files.

Keep sources identical to the compilation inputs, preserve the real report's timing,
identify the code state and data sources, and validate the PDF before updating this
index. Compile reports may refer to workspace logs that are not distributed; explain
that distinction in the stage notes. Reproduction must target a new caller-selected
directory and must not overwrite checked-in examples or canonical fixtures.

"""Seed a fresh validation run from verified retrieval artifacts and replay paid reading replies."""

from __future__ import annotations

import json

from airesearcher.core.project import Project
from airesearcher.stages.idea.storage import save_json

EXACT = {
    "idea/brief.json", "idea/queries.json", "idea/query_repair.json", "idea/literature.jsonl",
    "idea/screen_input.json", "idea/screening.json", "idea/reading_input.json",
}
PREFIXES = ("idea/search_batches/", "idea/search_snapshots/", "idea/screen_batches/",
            "idea/fulltext/", "idea/fulltext_records/")


def seed_cached_run(project, original_root):
    original = Project.open(original_root)
    if project.root == original.root or project.config.goal != original.config.goal:
        raise ValueError("Recovery requires a different empty workspace with the same validation goal")
    copied = []
    for path in original.archive.artifact_ids():
        if path not in EXACT and not path.startswith(PREFIXES):
            continue
        info = original.archive.latest_version(path)
        if info.is_dir:
            raise ValueError("Validation recovery expects individual archived files")
        # Read the immutable stored version and verify its hash instead of copying unchecked files.
        project.archive.put(path, info.kind, original.archive.get(info.ref),
                            producer="agent:idea/recovery", note="Verified artifact copied from prior validation")
        copied.append(info.ref.model_dump(mode="json"))
    if not {"idea/brief.json", "idea/queries.json", "idea/reading_input.json"} <= {r["artifact_id"] for r in copied}:
        raise ValueError("The original validation lacks the inputs needed to replay reading")
    cfg = project.config.model_copy(deep=True)
    cfg.llm_cache = "replay"
    versions = {}
    calls_path = original.root / ".llm/calls.jsonl"
    for line in calls_path.read_text(encoding="utf-8").splitlines():
        call = json.loads(line)
        if call["prompt_id"] in {"idea/methods_reader", "idea/feasibility_reader", "idea/single_reader"}:
            versions.setdefault(call["prompt_id"], set()).add(call["prompt_version"])
    if not versions or any(len(found) != 1 for found in versions.values()):
        raise ValueError("Missing or mixed reading prompt versions require manual cache review")
    cfg.prompt_pins.update({prompt: next(iter(found)) for prompt, found in versions.items()})
    project.save_config(cfg, summary="Replay original paid reading replies before fresh downstream analysis")
    save_json(project, "idea/recovery_manifest.json", {
        "source_project": str(original.root), "copied": copied, "mode": "paid_reply_cache_replay",
        "note": "Failed reading cards and downstream drafts were not copied; original history is preserved.",
    })


def finish_replay(project):
    cfg = project.config.model_copy(deep=True)
    cfg.llm_cache = "record"
    for prompt in ("idea/methods_reader", "idea/feasibility_reader", "idea/single_reader"):
        cfg.prompt_pins.pop(prompt, None)
    project.save_config(cfg, summary="Cached reading recovered; record new downstream model replies")


def recover_cached_score(ctx, state):
    """Revalidate the saved score reply locally; this helper never schedules a model request."""
    import json

    from airesearcher.core.fsutil import sha256_bytes
    from airesearcher.llm.cache import LLMCache
    from airesearcher.llm.gateway import UNTRUSTED_NOTICE, extract_json
    from airesearcher.stages.idea.evidence import load_json, material
    from airesearcher.stages.idea.models import ScoreOutput
    from airesearcher.stages.idea.scoring import decode_score
    from airesearcher.stages.idea.selection import normalize_scores, round_path, selection_context

    context = {**selection_context(ctx), "idea": load_json(ctx, "idea/selected.json")}
    attempts_path = round_path(state, "score_attempts")
    attempts = load_json(ctx, attempts_path)
    digest = sha256_bytes(json.dumps(context, ensure_ascii=False, sort_keys=True).encode())
    if attempts["input_sha256"] != digest or not attempts["attempts"]:
        raise ValueError("The failed score reply does not match the current immutable input")
    last = attempts["attempts"][-1]
    if last["status"] != "failed" or not last.get("trace"):
        raise ValueError("Only a completed, traced score reply can be revalidated from cache")
    if last.get("response_path") and ctx.archive.latest(last["response_path"]):
        cached = load_json(ctx, last["response_path"])
        if (cached["input_sha256"] != digest
                or cached["trace"]["call_id"] != last["trace"]["call_id"]):
            raise ValueError("Archived score response does not match its input or call trace")
    else:
        prompt = ctx.llm.prompts.load("idea/score", last["trace"]["prompt_version"])
        system, user = prompt.render({
            "schema_json": ScoreOutput.model_json_schema(), "goal": ctx.project.goal,
            "context_block": ctx.llm.wrap_untrusted(json.dumps(context, ensure_ascii=False),
                                                 source="selection_context"),
            "validation_errors": [a.get("error", "") for a in attempts["attempts"][:-1]],
        })
        candidates = ctx.llm.resolve("idea.score", "idea/score", "strong", prompt.tier)
        entry = next(m.entry for m in candidates if m.entry.litellm_model == last["trace"]["model"])
        key = LLMCache.key(entry.litellm_model,
                           [{"role": "system", "content": system + UNTRUSTED_NOTICE},
                            {"role": "user", "content": user}],
                           "ScoreOutput", entry.temperature)
        cached = LLMCache().get(key)
    output = (decode_score(cached, ScoreOutput) if last.get("response_path")
              else ScoreOutput.model_validate_json(extract_json(cached["text"])))
    original = output.model_dump(mode="json")
    save_json(ctx, round_path(state, "score_cached_review"), {
        "input_sha256": digest, "original_output": original, "trace": last["trace"],
        "note": "Local revalidation of an already paid reply; no new model request.",
    })
    _, papers, refs, weak = material(ctx)
    normalize_scores(ctx, output, papers, refs, weak)
    save_json(ctx, round_path(state, "score"), {
        "input_sha256": digest, "output": output.model_dump(mode="json"), "trace": last["trace"],
        "recovered_from_cache": True, "original_output": original,
        "note": "Only unambiguous registered paper/role/item aliases were canonicalized; no new model call.",
    })

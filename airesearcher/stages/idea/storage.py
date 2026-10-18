"""Save stage artifacts through the shared archive; literature JSONL remains append-only."""

import json

from airesearcher.services.literature import load_papers


def save_json(ctx, path: str, value, kind: str = "idea"):
    previous = ctx.archive.latest(path)
    return ctx.archive.put(path, kind, json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                           parents=[previous] if previous else [], producer="agent:idea")


def append_papers(ctx, papers):
    existing = load_papers(ctx.root)
    path = ctx.root / "idea/literature.jsonl"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    additions = [p for p in papers if p.paper_id not in existing or existing[p.paper_id] != p]
    if text and not text.endswith("\n"):
        text += "\n"
    text += "".join(p.model_dump_json() + "\n" for p in additions)
    previous = ctx.archive.latest("idea/literature.jsonl")
    return ctx.archive.put("idea/literature.jsonl", "literature", text,
                           parents=[previous] if previous else [], producer="agent:idea")

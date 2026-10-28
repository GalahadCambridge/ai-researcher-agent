"""Constrain score citations and recover archived responses without paid regeneration."""

import json

from airesearcher.llm.gateway import extract_json

from .storage import save_json


def score_variables(schema, context, previous):
    literature = context["literature"]
    allowed = sorted({item["ref"] for item in literature["items"]}
                     | {paper["paper_id"] for paper in literature["papers"]})
    shape = schema.model_json_schema()
    evidence = shape["$defs"]["StrictDimension"]["properties"]["evidence"]
    if allowed:
        evidence["items"]["enum"] = allowed
    else:
        evidence["maxItems"] = 0
    example = {name: {"score": 3, "rationale": "A tentative context-only assessment requiring verification.",
                       "evidence": [], "to_verify": True} for name in shape["properties"]}
    return {"schema_json": json.dumps(shape, ensure_ascii=False), "allowed_evidence_json": json.dumps(allowed),
            "example_json": json.dumps(example), "previous_output": previous}


def decode_score(response, schema):
    if response.get("finish_reason") == "length":
        raise ValueError("The score response reached the length limit; return shorter complete JSON")
    return schema.model_validate_json(extract_json(response["text"]))


def restore_score_attempt(ctx, path, attempts, schema, validate):
    record = attempts["attempts"][-1]
    raw_path = record.get("response_path")
    if record["status"] != "started" or not raw_path or not ctx.archive.latest(raw_path):
        return
    response = json.loads((ctx.root / raw_path).read_text(encoding="utf-8"))
    if response["input_sha256"] != attempts["input_sha256"]:
        raise ValueError("Archived score response belongs to different input")
    record["trace"] = response["trace"]
    try:
        output = decode_score(response, schema)
        record["raw_output"] = output.model_dump(mode="json")
        validate(output)
    except (ValueError, KeyError, TypeError) as exc:
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    else:
        record.update(status="done", raw_output=output.model_dump(mode="json"))
    save_json(ctx, path, attempts)

import json

from airesearcher.stages.writing.review import normalize


def run(skill, input_dir, out_dir):
    envelope = json.loads((input_dir / "review.json").read_text())
    issues = normalize(envelope["unit"], envelope["response"])
    (out_dir / "issues.json").write_text(json.dumps([i.model_dump() for i in issues], sort_keys=True, indent=2) + "\n")
    return envelope

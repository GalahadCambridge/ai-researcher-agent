"""Validate exact quotes and code-owned issue policy using production normalization."""

import json

from airesearcher.stages.writing.review import normalize


def check_output(output):
    try:
        if isinstance(output, str):
            output = json.loads(output)
        normalize(output["unit"], output["response"])
    except (ValueError, KeyError, TypeError) as exc:
        return [str(exc)]
    return []

"""Preserve explicit comparison targets from the user's goal without trusting literature instructions."""

from __future__ import annotations

import hashlib
import re
import unicodedata


def normalized(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[^\w]+", " ", text).strip()


def target_key(text):
    value = normalized(text)
    aliases = {"full fine tuning", "full finetuning", "full parameter fine tuning",
               "full parameter finetuning", "full model fine tuning"}
    if value in aliases:
        return "full fine tuning"
    if value in {"low rank adaptation", "low rank adapters", "lora"}:
        return "lora"
    return re.sub(r"^(?:the |standard )", "", value)


def comparison_targets(goal):
    """Extract two explicit English comparison targets; unsupported syntax remains advisory."""
    match = re.search(
        r"\bcompar(?:e|ing)\s+(.+?)\s+(?:and|with|against|versus|vs\.?)\s+(.+?)"
        r"(?=\s+(?:for|on|under|using|in)\b|[.;\n]|$)", goal, re.I,
    )
    if not match:
        match = re.search(r"^(.+?)\s+(?:versus|vs\.?)\s+(.+?)"
                          r"(?=\s+(?:for|on|under|using|in)\b|[.;\n]|$)", goal, re.I)
    if not match:
        return []
    targets = [value.strip(" \"'") for value in match.groups()]
    if any(not value or len(value.split()) > 12 for value in targets):
        return []
    return targets if target_key(targets[0]) != target_key(targets[1]) else []


def goal_contract(goal):
    targets = comparison_targets(goal)
    return {"goal": goal, "goal_sha256": hashlib.sha256(goal.encode()).hexdigest(),
            "comparison_targets": targets,
            "coverage": "explicit_english_comparison" if targets else "advisory_only",
            "limitations": "Deterministic checks do not prove full semantic alignment with every user requirement."}


def contains_target(text, target):
    key = target_key(target)
    value = normalized(text)
    if key == "full fine tuning":
        return bool(re.search(r"\bfull (?:parameter |model )?(?:fine tuning|finetuning)\b", value))
    if key == "lora":
        return bool(re.search(r"(?<![\w-])lora(?![\w-])|\blow[ -]rank adaptation\b", text, re.I))
    return f" {key} " in f" {value} "


def validate_goal_scope(goal, idea):
    contract = goal_contract(goal)
    targets = contract["comparison_targets"]
    named_methods = [item.name for item in [*idea.baselines, *idea.methods]]
    hypotheses = " ".join(h.statement for h in idea.hypotheses)
    missing = [target for target in targets if not contains_target(idea.research_question, target)
               or not any(contains_target(name, target) for name in named_methods)
               or not contains_target(hypotheses, target)]
    if len(targets) == 2:
        pairs = [(i, j) for i, left in enumerate(named_methods) for j, right in enumerate(named_methods)
                 if i != j and contains_target(left, targets[0]) and contains_target(right, targets[1])]
        if not pairs:
            missing = targets
    if missing:
        raise ValueError("GOAL_SCOPE_MISMATCH: preserve the user's explicit comparison targets in the research "
                         "question, distinct method entries, and hypotheses: " + ", ".join(missing)
                         + ". Missing literature must remain an open question; do not substitute another study.")
    return {**contract, "status": "passed" if targets else "not_automatically_established"}

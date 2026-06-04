"""Shared review helpers for every review-capable adapter.

Centralizes the verdict schema, the (adversarial) audit prompt, and tolerant JSON
parsing so cross-model audit — one model reviewing another model's diff — behaves
identically no matter which agent does the reviewing.
"""
from __future__ import annotations

import json

from .base import ReviewFinding, ReviewResult

VERDICTS = {"approve", "request_changes"}
SEVERITIES = {"blocker", "warning", "nit"}


def review_prompt(goal: str, diff: str) -> str:
    """An adversarial audit prompt: the reviewer is checking ANOTHER agent's work,
    so it's told to be skeptical and hunt for problems rather than rubber-stamp."""
    return (
        "You are auditing another AI agent's code change. Be skeptical and "
        "adversarial: assume there may be bugs, omissions, or shortcuts and try to "
        "find them — do NOT rubber-stamp. Review the unified DIFF against its GOAL. "
        "You may read the repository (read-only) for context. Judge correctness, "
        "completeness vs the goal, edge cases, obvious bugs, and security. Be "
        "concise and specific.\n\n"
        f"GOAL:\n{goal}\n\n"
        f"DIFF:\n{diff}\n\n"
        "Respond with ONLY a JSON object — no prose, no markdown fences:\n"
        '{"verdict": "approve" | "request_changes", '
        '"summary": "<1-3 sentence overall assessment>", '
        '"findings": [{"severity": "blocker" | "warning" | "nit", '
        '"file": "<path or empty>", "comment": "<specific issue>"}]}\n'
        'Use "request_changes" if any blocker exists; otherwise "approve". '
        "An empty findings array is fine when the change is genuinely clean."
    )


def extract_json_object(text: str) -> dict:
    """Best-effort: pull the first top-level JSON object out of model output."""
    text = text.strip()
    if text.startswith("```"):  # strip a markdown fence + optional language hint
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        v = None
                    if isinstance(v, dict):
                        return v
                    break  # this {...} wasn't a JSON object; try the next one
        start = text.find("{", start + 1)
    return {}


def parse_review(text: str) -> ReviewResult:
    """Map a reviewer's raw JSON output into a normalized ReviewResult, tolerating
    a missing/garbled verdict by inferring it from finding severities."""
    obj = extract_json_object(text)
    findings: list[ReviewFinding] = []
    for item in obj.get("findings") or []:
        if not isinstance(item, dict):
            continue
        comment = str(item.get("comment", "")).strip()
        if not comment:
            continue
        sev = str(item.get("severity", "")).strip().lower()
        findings.append(ReviewFinding(
            severity=sev if sev in SEVERITIES else "warning",
            comment=comment,
            file=str(item.get("file", "")).strip(),
        ))
    verdict = str(obj.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        verdict = ("request_changes"
                   if any(f.severity == "blocker" for f in findings)
                   else "approve")
    return ReviewResult(
        verdict=verdict,
        summary=str(obj.get("summary", "")).strip(),
        findings=findings,
    )

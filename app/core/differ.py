"""Structured diffs.

The output of this module is the product: a machine-readable description of
what changed, not a screenshot. It goes straight into the webhook payload.
"""

from __future__ import annotations

import difflib

MAX_LINES_REPORTED = 200
MAX_LINE_CHARS = 500
MAX_UNIFIED_LINES = 400


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "…"


def build_diff(old: str | None, new: str) -> dict:
    """Compare two normalised extractions and describe the difference."""
    old_lines = (old or "").split("\n") if old else []
    new_lines = new.split("\n") if new else []

    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    similarity = round(matcher.ratio(), 4)

    added: list[str] = []
    removed: list[str] = []
    modified: list[dict[str, str]] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            added.extend(_clip(l) for l in new_lines[j1:j2])
        elif tag == "delete":
            removed.extend(_clip(l) for l in old_lines[i1:i2])
        elif tag == "replace":
            old_block = old_lines[i1:i2]
            new_block = new_lines[j1:j2]
            for idx in range(max(len(old_block), len(new_block))):
                before = old_block[idx] if idx < len(old_block) else ""
                after = new_block[idx] if idx < len(new_block) else ""
                modified.append({"before": _clip(before), "after": _clip(after)})

    unified = list(
        difflib.unified_diff(
            old_lines, new_lines, fromfile="previous", tofile="current", lineterm="", n=2
        )
    )

    return {
        "similarity": similarity,
        "lines_added": len(added),
        "lines_removed": len(removed),
        "lines_modified": len(modified),
        "added": added[:MAX_LINES_REPORTED],
        "removed": removed[:MAX_LINES_REPORTED],
        "modified": modified[:MAX_LINES_REPORTED],
        "truncated": (
            len(added) > MAX_LINES_REPORTED
            or len(removed) > MAX_LINES_REPORTED
            or len(modified) > MAX_LINES_REPORTED
            or len(unified) > MAX_UNIFIED_LINES
        ),
        "unified_diff": "\n".join(unified[:MAX_UNIFIED_LINES]),
        "is_first_observation": old is None,
    }


def summarise(diff: dict, monitor_name: str) -> str:
    """One-line human summary, used in email subjects and log lines."""
    if diff.get("is_first_observation"):
        return f"{monitor_name}: first observation recorded"
    parts = []
    if diff.get("lines_modified"):
        parts.append(f"{diff['lines_modified']} modified")
    if diff.get("lines_added"):
        parts.append(f"{diff['lines_added']} added")
    if diff.get("lines_removed"):
        parts.append(f"{diff['lines_removed']} removed")
    detail = ", ".join(parts) if parts else "content changed"
    return f"{monitor_name}: {detail}"

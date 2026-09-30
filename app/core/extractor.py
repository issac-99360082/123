"""Turn a raw HTTP response into the small piece of text we actually compare.

Extraction is the difference between a useful monitor and a noisy one. Diffing
whole pages produces false positives on every ad slot, CSRF token and rendered
timestamp; extracting `.price` first means the monitor fires only when the
price moves.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

import regex as regex_engine
from bs4 import BeautifulSoup

from app.models import ExtractMode

MAX_EXTRACT_CHARS = 200_000
_NON_CONTENT_TAGS = ("script", "style", "noscript", "template", "svg")

# ReDoS containment. Users supply these patterns and we run them, so a
# catastrophic-backtracking pattern is a remote CPU denial of service. Three
# layers, since none alone suffices: the `regex` engine (defuses the classic
# pathological cases), a hard `timeout=` on every user pattern, and caps on
# pattern and input size.
REGEX_TIMEOUT_SECONDS = 2.0
MAX_REGEX_INPUT_CHARS = 100_000
MAX_PATTERN_CHARS = 1_000


class ExtractionError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_NON_CONTENT_TAGS):
        tag.decompose()
    return soup.get_text(separator="\n")


def normalise(text: str) -> str:
    """Collapse the differences that are never semantically meaningful.

    Unicode NFC, CRLF -> LF, runs of inline whitespace -> single space, trim
    each line, drop blank lines. Two pages that render identically should
    normalise identically.
    """
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", " ").replace("​", "")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _check_pattern(pattern: str) -> None:
    if len(pattern) > MAX_PATTERN_CHARS:
        raise ExtractionError(
            "pattern_too_long",
            f"Pattern exceeds {MAX_PATTERN_CHARS} characters.",
        )


def safe_findall(pattern: str, text: str) -> list:
    """Run a user-supplied pattern under the ReDoS containment described above."""
    _check_pattern(pattern)
    text = text[:MAX_REGEX_INPUT_CHARS]
    try:
        return regex_engine.findall(
            pattern,
            text,
            flags=regex_engine.MULTILINE | regex_engine.DOTALL,
            timeout=REGEX_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise ExtractionError(
            "regex_timeout",
            f"Pattern took longer than {REGEX_TIMEOUT_SECONDS}s to evaluate. "
            "Simplify it - nested quantifiers such as (a+)+ are the usual cause.",
        ) from exc
    except regex_engine.error as exc:
        raise ExtractionError("invalid_regex", f"Invalid regex: {exc}") from exc


def safe_sub(pattern: str, replacement: str, text: str) -> str:
    """Substitution counterpart of safe_findall, with the same containment."""
    _check_pattern(pattern)
    return regex_engine.sub(
        pattern,
        replacement,
        text[:MAX_REGEX_INPUT_CHARS],
        flags=regex_engine.MULTILINE,
        timeout=REGEX_TIMEOUT_SECONDS,
    )


def apply_ignore_patterns(text: str, patterns: list[str] | None) -> str:
    """Blank out volatile substrings (timestamps, nonces, view counters).

    An invalid, oversized or too-slow pattern is skipped rather than failing the
    whole check - the monitor keeps working and the user sees the pattern had no
    effect. A hostile pattern therefore costs at most REGEX_TIMEOUT_SECONDS.
    """
    if not patterns:
        return text
    for pattern in patterns[:20]:
        try:
            text = safe_sub(pattern, "", text)
        except (regex_engine.error, re.error, TimeoutError, ExtractionError):
            continue
    return normalise(text)


def _extract_json_path(body: str, path: str):
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ExtractionError("not_json", f"Response is not valid JSON: {exc}") from exc

    current = data
    for part in [p for p in path.replace("[", ".").replace("]", "").split(".") if p]:
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise ExtractionError("path_not_found", f"No index {part!r} in JSON array.") from exc
        elif isinstance(current, dict):
            if part not in current:
                raise ExtractionError("path_not_found", f"Key {part!r} not found in JSON object.")
            current = current[part]
        else:
            raise ExtractionError("path_not_found", f"Cannot descend into {part!r}: value is a scalar.")
    if isinstance(current, (dict, list)):
        return json.dumps(current, sort_keys=True, ensure_ascii=False, indent=2)
    return str(current)


def extract(
    body: str,
    mode: str = ExtractMode.text.value,
    selector: str | None = None,
    ignore_patterns: list[str] | None = None,
) -> str:
    """Extract, normalise and clean the comparable content of a response."""
    if mode == ExtractMode.raw.value:
        content = body

    elif mode == ExtractMode.text.value:
        content = html_to_text(body)

    elif mode == ExtractMode.css.value:
        if not selector:
            raise ExtractionError("missing_selector", "extract_mode 'css' requires extract_value.")
        soup = BeautifulSoup(body, "html.parser")
        try:
            nodes = soup.select(selector)
        except Exception as exc:  # soupsieve raises its own selector errors
            raise ExtractionError("invalid_selector", f"Invalid CSS selector: {exc}") from exc
        if not nodes:
            raise ExtractionError("selector_no_match", f"CSS selector {selector!r} matched nothing.")
        content = "\n".join(node.get_text(separator="\n") for node in nodes)

    elif mode == ExtractMode.regex.value:
        if not selector:
            raise ExtractionError("missing_selector", "extract_mode 'regex' requires extract_value.")
        matches = safe_findall(selector, body)
        if not matches:
            raise ExtractionError("selector_no_match", f"Regex {selector!r} matched nothing.")
        flat = ["".join(m) if isinstance(m, tuple) else str(m) for m in matches]
        content = "\n".join(flat)

    elif mode == ExtractMode.json_path.value:
        if not selector:
            raise ExtractionError("missing_selector", "extract_mode 'json' requires extract_value.")
        content = _extract_json_path(body, selector)

    else:
        raise ExtractionError("unknown_mode", f"Unknown extract_mode {mode!r}.")

    content = normalise(content)
    content = apply_ignore_patterns(content, ignore_patterns)
    return content[:MAX_EXTRACT_CHARS]


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()

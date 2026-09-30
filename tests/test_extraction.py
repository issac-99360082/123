"""Extraction and diffing - the logic that decides whether a change happened."""

from __future__ import annotations

import pytest

from app.core.differ import build_diff, summarise
from app.core.extractor import ExtractionError, content_hash, extract, normalise

PAGE = """
<html><head><style>.x{color:red}</style><script>var t=Date.now()</script></head>
<body>
  <h1>Widget Pro</h1>
  <div class="price">  $19.00  </div>
  <p class="stock">In stock</p>
  <span id="rendered-at">2026-09-02 11:04:33</span>
</body></html>
"""


def test_css_extraction_picks_only_the_selected_node():
    assert extract(PAGE, "css", ".price") == "$19.00"


def test_text_extraction_drops_script_and_style():
    text = extract(PAGE, "text")
    assert "Widget Pro" in text and "$19.00" in text
    assert "Date.now" not in text and "color:red" not in text


def test_ignore_patterns_remove_volatile_content():
    without = extract(PAGE, "text", ignore_patterns=[r"\d{4}-\d{2}-\d{2} [\d:]+"])
    assert "2026-09-02" not in without
    assert "Widget Pro" in without


def test_whitespace_only_change_is_not_a_change():
    a = extract('<div class="price">$19.00</div>', "css", ".price")
    b = extract('<div class="price">\n\n   $19.00\t </div>', "css", ".price")
    assert content_hash(a) == content_hash(b)


def test_real_value_change_moves_the_hash():
    a = extract('<div class="price">$19.00</div>', "css", ".price")
    b = extract('<div class="price">$17.50</div>', "css", ".price")
    assert content_hash(a) != content_hash(b)


def test_json_path_extraction():
    body = '{"data":{"items":[{"status":"ok"},{"status":"degraded"}]}}'
    assert extract(body, "json", "data.items[1].status") == "degraded"


def test_regex_extraction_returns_capture_group():
    assert extract(PAGE, "regex", r'class="price">\s*([^<]+?)\s*<') == "$19.00"


@pytest.mark.parametrize(
    "mode,selector,kind",
    [
        ("css", ".does-not-exist", "selector_no_match"),
        ("css", None, "missing_selector"),
        ("regex", r"zzz(\d+)", "selector_no_match"),
        ("json", "a.b", "not_json"),
        ("bogus", None, "unknown_mode"),
    ],
)
def test_extraction_failures_are_typed(mode, selector, kind):
    with pytest.raises(ExtractionError) as exc:
        extract(PAGE, mode, selector)
    assert exc.value.kind == kind


def test_invalid_ignore_pattern_is_skipped_not_fatal():
    assert extract(PAGE, "css", ".price", ignore_patterns=["(unclosed"]) == "$19.00"


def test_normalise_is_idempotent():
    once = normalise("  a \t b \r\n\n  c  ")
    assert normalise(once) == once == "a b\nc"


def test_diff_reports_modified_line():
    diff = build_diff("$19.00", "$17.50")
    assert diff["lines_modified"] == 1
    assert diff["modified"][0] == {"before": "$19.00", "after": "$17.50"}
    assert diff["is_first_observation"] is False


def test_diff_reports_additions_and_removals():
    diff = build_diff("a\nb\nc", "a\nc\nd")
    assert diff["lines_removed"] == 1 and diff["lines_added"] == 1
    assert "unified_diff" in diff and diff["unified_diff"]


def test_first_observation_is_flagged():
    diff = build_diff(None, "anything")
    assert diff["is_first_observation"] is True
    assert summarise(diff, "M") == "M: first observation recorded"


def test_large_diff_is_truncated_not_unbounded():
    old = "\n".join(f"line {i}" for i in range(5000))
    new = "\n".join(f"changed {i}" for i in range(5000))
    diff = build_diff(old, new)
    assert diff["truncated"] is True
    assert len(diff["modified"]) <= 200
    assert len(diff["unified_diff"].split("\n")) <= 400

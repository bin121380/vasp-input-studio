from __future__ import annotations

import re
from pathlib import Path


STYLES = (Path(__file__).resolve().parents[1] / "static" / "styles.css").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>[^}}]*)\}}", STYLES, flags=re.MULTILINE)
    assert match is not None, f"missing CSS rule for {selector}"
    return match.group("body")


def test_dos_series_are_rendered_as_unfilled_lines() -> None:
    rule = _rule(".dos-series")
    assert re.search(r"\bfill\s*:\s*none\s*;", rule)
    assert re.search(r"\bstroke-width\s*:", rule)


def test_dynamic_ui_layout_classes_keep_explicit_rules() -> None:
    selectors = (
        ".band-artifact-strip",
        ".band-plot-meta",
        ".incar-form-actions",
        ".jobs-scroll",
        ".mapping-grid",
        ".mapping-item",
        ".system-card-head",
        ".tier-card-head",
    )
    for selector in selectors:
        assert selector in STYLES, f"missing CSS selector {selector}"


def test_mobile_shell_can_shrink_below_content_width() -> None:
    assert "grid-template-columns: minmax(0, 1fr);" in STYLES
    sidebar = _rule(".sidebar")
    assert re.search(r"\bmin-width\s*:\s*0\s*;", sidebar)

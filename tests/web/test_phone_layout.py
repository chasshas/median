"""Phone-width layout (phase 9 regression): wide tables must not widen the page.

At 390 px the jobs, sources, tags and settings tables were wider than the screen, so the whole
page scrolled sideways. The suite has no browser, so these tests pin the CSS rules that fix it
and check that the pages still use the classes those rules target.
"""

import re
from pathlib import Path

import pytest

WEB = Path(__file__).parents[2] / "app" / "web"
CSS = (WEB / "static" / "app.css").read_text()
TEMPLATES = WEB / "templates"


def narrow_rules() -> str:
    """The bodies of every @media (max-width: ...) block of app.css."""
    return "\n".join(re.findall(r"@media \(max-width: \d+px\) \{(.*?)\n\}", CSS, re.DOTALL))


def test_wide_tables_scroll_inside_themselves_on_narrow_screens() -> None:
    rule = re.search(r"\.data-table, \.tags-table \{([^}]*)\}", narrow_rules())

    assert rule is not None
    body = rule.group(1)
    assert "display: block" in body
    assert "overflow-x: auto" in body
    # Absolutely positioned children (visually hidden labels) must stay inside the scroll box.
    assert "position: relative" in body


def test_settings_grid_column_can_shrink_below_its_content() -> None:
    rule = re.search(r"\.settings-grid \{([^}]*)\}", CSS)

    assert rule is not None
    assert "grid-template-columns: minmax(0, 1fr)" in rule.group(1)


@pytest.mark.parametrize(
    ("template", "marker"),
    [
        ("jobs.html", 'class="data-table card" id="jobs-table"'),
        ("sources.html", 'class="data-table card" id="sources-table"'),
        ("tags.html", 'class="tags-table card" id="tags-table"'),
        ("settings.html", 'class="settings-grid"'),
    ],
)
def test_pages_use_the_responsive_table_classes(template: str, marker: str) -> None:
    assert marker in (TEMPLATES / template).read_text()

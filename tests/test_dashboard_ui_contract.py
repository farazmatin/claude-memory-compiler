"""Static UI contracts that keep the local dashboard usable and accessible."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

STATIC_DIR = Path(__file__).parents[1] / "pipeline" / "static"


class _IndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.by_id: dict[str, dict[str, str | None]] = {}
        self.labels_for: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if element_id := attributes.get("id"):
            self.by_id[element_id] = attributes
        if tag == "label" and (control_id := attributes.get("for")):
            self.labels_for.add(control_id)


def _index() -> _IndexParser:
    parser = _IndexParser()
    parser.feed((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
    return parser


def test_primary_tabs_expose_relationships_and_roving_tabindex():
    page = _index()
    pairs = {
        "tab-button-ledger": "tab-ledger",
        "tab-button-knowledge": "tab-knowledge",
        "tab-button-ask": "tab-ask",
        "tab-button-control": "tab-control",
    }

    for button_id, panel_id in pairs.items():
        button = page.by_id[button_id]
        panel = page.by_id[panel_id]
        assert button["aria-controls"] == panel_id
        assert panel["aria-labelledby"] == button_id

    assert page.by_id["tab-button-ledger"]["tabindex"] == "0"
    for button_id in tuple(pairs)[1:]:
        assert page.by_id[button_id]["tabindex"] == "-1"


def test_editable_fields_have_durable_labels():
    page = _index()
    expected = {
        "meeting-search",
        "person-canonical",
        "person-role",
        "person-aliases",
        "merge-from",
        "merge-into",
        "question",
        "timeline-topic-input",
        "modal-speaker-name",
    }
    assert expected <= page.labels_for


def test_library_voice_queue_and_idle_status_are_explicit_contracts():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    markup = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert "const VOICE_LIBRARY_LIMIT = 3;" in script
    assert ".slice(0, VOICE_LIBRARY_LIMIT)" in script
    assert 'statusText.textContent = "Processing Idle"' in script
    assert 'id="archive-attention"' in markup


def test_narrow_layout_reflows_archive_filters_instead_of_clipping_them():
    stylesheet = (STATIC_DIR / "style.css").read_text(encoding="utf-8")

    assert "@media (max-width: 900px)" in stylesheet
    assert ".archive-heading" in stylesheet
    assert "flex-direction: column" in stylesheet
    assert ".archive-filters" in stylesheet
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in stylesheet
    assert ".search input" in stylesheet
    assert "min-width: 0" in stylesheet
    assert "scroll-snap-type: x mandatory" in stylesheet

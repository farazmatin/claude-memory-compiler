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
        "tab-button-speakers": "tab-speakers",
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
        "people-search",
        "person-merge-target",
        "person-rename-name",
        "question",
        "timeline-topic-input",
        "modal-speaker-name",
        "voice-existing-person",
        "voice-new-person-name",
    }
    assert expected <= page.labels_for


def test_library_voice_queue_and_idle_status_are_explicit_contracts():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    markup = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert 'id="speaker-resolution-clusters"' in markup
    assert 'id="speaker-resolution-oneoffs"' in markup
    assert 'id="speaker-resolution-summary"' in markup
    assert 'fetch("/api/speakers/queue")' in script
    assert "renderOneOffSpeakerCards" in script
    assert "confirmOneOffSpeaker" in script
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


def test_speaker_identity_review_exposes_playback_and_human_name_choices():
    page = _index()
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    markup = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert "voice-name-modal" in page.by_id
    assert "voice-name-existing-mode" in page.by_id
    assert "voice-name-new-mode" in page.by_id
    assert 'class="js-voice-audio" controls' in script
    assert "playClusterClips(listen.dataset.cluster, listen)" in script
    assert "openVoiceNameModal(custom.dataset.cluster)" in script
    assert 'prompt("Enter the real contact name' not in script
    assert "Choose someone you already know, or add a new person" in markup


def test_duplicate_speaker_merge_and_noise_actions_explain_their_effects():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    markup = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert 'id="people-list" class="people-list"' in markup
    assert 'id="person-merge-modal"' in markup
    assert 'id="person-merge-target" required' in markup
    assert 'id="people-merge-selected" disabled' in markup
    assert "Update Corrected Minutes" in markup
    assert "openSelectedPeopleMergeModal" in script
    assert 'class="js-person-select"' in script
    assert 'fetch("/api/people/merge-many"' in script
    assert "Select all spellings for one person" in markup
    assert "Fix spelling" in script
    assert 'id="people-suggestion-yes"' in markup
    assert 'id="people-suggestion-no"' in markup
    assert "Do these ${names.length} names describe the same person?" in script
    assert "No, different people" in markup
    assert "choose the spelling you want to see everywhere" in markup.lower()
    assert "No audio, transcript, or minutes will be deleted" in script
    assert "runStage('speaker-refresh')" in markup


def test_people_merge_controls_allow_typed_targets_and_preview_before_apply():
    page = _index()

    assert page.by_id["people-suggestion-rename"]["type"] == "button"
    assert "people-suggestion-target" in page.labels_for
    assert "disabled" in page.by_id["people-suggestion-confirm"]
    assert "person-merge-target-custom" in page.labels_for
    assert "disabled" in page.by_id["person-merge-save"]
    assert "person-rename-preview" in page.by_id

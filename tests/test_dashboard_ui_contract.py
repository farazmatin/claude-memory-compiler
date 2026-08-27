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


def test_slow_actions_expose_a_pending_state_and_announce_their_outcome():
    """A click must be visibly distinct from a dead button.

    Every action here is a round trip to a server that may be transcribing
    audio, calling an LLM, or rewriting minutes, so the pending state and the
    finished-run announcement are the only signals that separate "still
    working" from "nothing happened".
    """
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    stylesheet = (STATIC_DIR / "style.css").read_text(encoding="utf-8")

    # The shared pending-state plumbing and its double-submit guard.
    assert "async function withBusy(button, label, task)" in script
    assert 'button.setAttribute("aria-busy", "true")' in script
    assert 'if (button && button.dataset.busy === "1") return undefined;' in script
    assert '[aria-busy="true"]' in stylesheet
    assert "@keyframes pending-spin" in stylesheet

    # Actions that reach the server go through it.
    for handler in (
        "async function runStage(stage, trigger = triggerButton())",
        "async function retryAllFailed(trigger = triggerButton())",
        "async function recompileStale(trigger = triggerButton())",
        "async function retrySingleMeeting(meetingId, trigger = triggerButton())",
        "async function confirmOneOffSpeaker(meetingId, label, name, trigger = triggerButton())",
        "async function dismissVoiceCluster(clusterId, trigger = triggerButton())",
        "async function loadTimelineForTopic(topic, trigger = triggerButton())",
    ):
        assert handler in script

    # A finished background run announces itself instead of going quiet.
    assert "if (wasRunning && !status.running)" in script
    assert 'showToast("Processing finished.", "success")' in script
    assert "showToast(`Processing stopped: ${status.error}`" in script
    assert "function setStageControlsRunning(running)" in script

    # The archive query is the slowest action, so it counts the seconds.
    assert 'id="answer-elapsed"' in script
    assert ".answer-elapsed" in stylesheet

    # Refresh reported success while its requests were still in flight.
    assert 'withBusy(event.currentTarget, "\u21bb Refreshing\u2026"' in script
    assert 'showToast("Refreshed archive data", "success");' in script


def test_both_review_surfaces_expose_labelled_sort_and_filter_controls():
    """Sorting is a control, not a hardcoded ORDER BY.

    104 meetings and 166 pending speaker decisions are more than a fixed order
    can serve: the useful next row is "thinnest score margin" one minute and
    "longest speech I have a clip for" the next. Both lists get the same
    vocabulary so the bar means one thing in two places.
    """
    page = _index()
    controls = {
        "meeting-sort",
        "meeting-filter-range",
        "meeting-filter-audio",
        "speaker-sort",
        "speaker-filter-band",
        "speaker-filter-suggestion",
        "speaker-filter-clip",
    }

    assert controls <= set(page.by_id)
    # aria-label is not enough here - these sit in a row of similar selects and
    # a visible label is the only thing that says which list a control governs.
    assert controls <= page.labels_for


def test_speaker_controls_read_clusters_and_one_offs_through_one_shape():
    """A cluster and a one-off label are the same decision in two shapes.

    Clusters count `size`/`total_speech`; one-offs carry `speech_sec` and no
    size at all. Without one normalizer, every comparator and predicate has to
    remember both shapes, and "sort by speech time" silently sorts one list by
    undefined.
    """
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "function normalizeSpeakerRow(row)" in script
    assert "const SPEAKER_SORTS = {" in script
    assert "const MEETING_SORTS = {" in script
    assert "function speakerMatchesFilters(row, controls)" in script
    assert "function applySpeakerControls()" in script
    # Thinnest margin first: a 0.72/0.71 coin flip is the row a human is needed
    # for, and sorting it below a confident 0.95 buries the only real decision.
    assert '"margin-asc"' in script


def test_control_choices_survive_a_reload():
    """The queue is worked over days, not in one sitting.

    Re-picking "thinnest margin, has a clip" on every page load is friction on
    exactly the workflow this is meant to speed up.
    """
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "function saveControlState()" in script
    assert "function loadControlState()" in script
    assert "localStorage" in script

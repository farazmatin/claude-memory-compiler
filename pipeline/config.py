"""Paths, timezone, and tunables for the meeting minutes pipeline.

Everything configurable lives here or in the environment. Environment variables
win over the defaults below so a server deployment can be retuned without
editing code.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# ── Paths ─────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent

# Load .env before any os.environ.get below reads a default. python-dotenv was
# already a declared dependency but nothing called it, so .env configured only
# docker compose and never the pipeline: MMC_LIGHTRAG_API_KEY stayed empty and
# requests went out unauthenticated, and HF_TOKEN stayed empty so diarization
# silently produced no speakers. Real environment variables still win, which is
# what a server deployment needs.
load_dotenv(ROOT_DIR / ".env", override=False)

INBOX_DIR = Path(os.environ.get("MMC_INBOX", ROOT_DIR / "inbox"))
DRIVE_HANDOFF_DIR = INBOX_DIR / "drive"
AUDIO_DIR = Path(os.environ.get("MMC_AUDIO", ROOT_DIR / "audio"))
TRANSCRIPTS_DIR = Path(os.environ.get("MMC_TRANSCRIPTS", ROOT_DIR / "transcripts"))
MINUTES_DIR = Path(os.environ.get("MMC_MINUTES", ROOT_DIR / "minutes"))
DB_DIR = Path(os.environ.get("MMC_DB_DIR", ROOT_DIR / "db"))
TEMPLATES_DIR = ROOT_DIR / "templates"

DB_PATH = DB_DIR / "manifest.db"
GLOSSARY_FILE = ROOT_DIR / "glossary.md"
MINUTES_TEMPLATE_FILE = TEMPLATES_DIR / "minutes.md"
SPEAKER_OVERRIDES_FILE = ROOT_DIR / "speaker-overrides.yaml"

ALL_DIRS = [
    INBOX_DIR, DRIVE_HANDOFF_DIR, AUDIO_DIR, TRANSCRIPTS_DIR, MINUTES_DIR, DB_DIR, TEMPLATES_DIR,
]

# ── External Export Targets ───────────────────────────────────────────
# Automatically push sanitized professional minutes to the Product Manager repo
DEFAULT_PM_MINUTES_DIR = ROOT_DIR.parent.parent / "Product Manager" / "minutes"
EXPORT_PM_MINUTES_DIR = Path(
    os.environ.get("MMC_EXPORT_PM_MINUTES_DIR", DEFAULT_PM_MINUTES_DIR)
).resolve()
ENABLE_PM_EXPORT = os.environ.get("MMC_ENABLE_PM_EXPORT", "1").lower() not in ("0", "false", "no")

# Populated further down, once SNIPPETS_DIR is defined; kept out of the literal
# above so the voice settings stay in one block.

# Audio extensions we will pick up out of the inbox.
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus", ".mp4"}

# ── Timezone ──────────────────────────────────────────────────────────
# Meeting dates and the date-ordered compile depend on this. A wrong value
# mislabels every document in the corpus.
TIMEZONE = os.environ.get("MMC_TIMEZONE", "America/Toronto")
TZ = ZoneInfo(TIMEZONE)

# ── ASR ───────────────────────────────────────────────────────────────
# large-v3-turbo, not large-v3. On CPU, large-v3 costs ~60-120 min per audio
# hour; with diarization on top that is ~9 h/day for 5 meetings, which does not
# fit an overnight batch. Turbo is ~4-8x faster for a modest accuracy cost on
# English. Override to "large-v3" if this ever runs on a GPU.
ASR_MODEL = os.environ.get("MMC_ASR_MODEL", "large-v3-turbo")
ASR_DEVICE = os.environ.get("MMC_ASR_DEVICE", "cpu")
ASR_COMPUTE_TYPE = os.environ.get("MMC_ASR_COMPUTE_TYPE", "int8")
ASR_BATCH_SIZE = int(os.environ.get("MMC_ASR_BATCH_SIZE", "8"))
ASR_LANGUAGE = os.environ.get("MMC_ASR_LANGUAGE", "en")

# ASR backend selection: Replicate is the default. Local WhisperX is available
# only through the explicit "whisperx" opt-in; a missing cloud token must never
# silently start a local model.
ASR_BACKEND = os.environ.get("MMC_ASR_BACKEND", "replicate").lower().strip()

# Replicate serverless GPU configuration
REPLICATE_API_TOKEN = (
    os.environ.get("REPLICATE_API_TOKEN", "") or os.environ.get("REPLICATE_API_KEY", "")
).strip()
REPLICATE_MODEL = os.environ.get("MMC_REPLICATE_MODEL", "victor-upmeet/whisperx").strip()
REPLICATE_TIMEOUT_SEC = float(os.environ.get("MMC_REPLICATE_TIMEOUT", "600"))
REPLICATE_POLL_INTERVAL_SEC = float(os.environ.get("MMC_REPLICATE_POLL_INTERVAL", "2.0"))

# Every ASR model resamples to 16 kHz mono internally, so normalizing to it up
# front costs no accuracy and shrinks ~18 MB/hr of m4a to ~2-4 MB/hr.
TARGET_SAMPLE_RATE = 16_000

# Whisper's initial_prompt is capped at 224 tokens. The glossary is priority
# ordered and truncated to fit rather than silently dropped.
INITIAL_PROMPT_TOKEN_BUDGET = 224

ENABLE_DIARIZATION = os.environ.get("MMC_DIARIZATION", "1") not in ("0", "false", "False")

# ── Voice enrollment ──────────────────────────────────────────────────
# Diarization separates voices within one recording; these settings govern
# recognising the same voice ACROSS recordings, so a person named once is
# labelled automatically thereafter.
#
# Every threshold below is set for the deployment profile that actually exists:
# one phone on a table, in person. That is far-field single-channel audio, the
# hardest realistic case, and it puts same-speaker similarity well below the
# figures published for close-mic enrollment. Treat these as starting points -
# `calibrate` replaces them with values measured on real recordings.

# Auto-apply at or above this cosine similarity.
VOICE_AUTO = float(os.environ.get("MMC_VOICE_AUTO", "0.62"))
# Queue a card for confirmation at or above this.
VOICE_REVIEW = float(os.environ.get("MMC_VOICE_REVIEW", "0.38"))
# Required gap between the best and second-best match. Absolute similarity alone
# confuses two people at the same table on the same microphone; distance to the
# runner-up is what catches it.
VOICE_MARGIN = float(os.environ.get("MMC_VOICE_MARGIN", "0.12"))
# Below this much speech, never auto-apply. A four-second embedding is noise, and
# someone who says "yeah, agreed" once should not enroll anybody.
VOICE_MIN_SPEECH_SEC = float(os.environ.get("MMC_VOICE_MIN_SPEECH_SEC", "30"))
# Meetings a person must appear in before they are eligible for auto-matching.
# The key far-field rule: enrolled from one meeting means enrolled from one seat,
# and the same colleague across the table next week embeds differently enough to
# be mistaken for someone else.
VOICE_MIN_ENROLL_MEETINGS = int(os.environ.get("MMC_VOICE_MIN_ENROLL_MEETINGS", "2"))
# Grouping pending labels into "one person" is deliberately tighter than the
# auto-match threshold: a contaminated cluster enrolls a poisoned voiceprint from
# a single confirmation, which is the most damaging wrong answer available.
VOICE_CLUSTER_THRESHOLD = float(os.environ.get("MMC_VOICE_CLUSTER", "0.72"))
# Embeddings from different models are not comparable. Matching filters on this,
# and changing it means re-enrollment rather than nonsense similarities.
VOICE_MODEL = os.environ.get("MMC_VOICE_MODEL", "pyannote/wespeaker-voxceleb-resnet34-LM")

# Retained voice clips, so speakers stay labellable by ear after the source audio
# is deleted. ~30 KB per speaker against 2-4 MB per audio-hour.
SNIPPETS_DIR = Path(os.environ.get("MMC_SNIPPETS", ROOT_DIR / "snippets"))
# Four clips, not three. Reviewers asked to hear ~20 seconds before putting a
# name to a voice, and 3x6s tops out at 18. This cannot be applied backwards:
# clips are cut once, at enrollment, from audio that transcription deletes in
# the same loop iteration - so the existing corpus keeps whatever it retained
# (18s for 30 of 57 speakers, 6s for 11, none for 8) and only new meetings
# reach 24s. Cost stays trivial at ~40 KB per speaker.
SNIPPET_COUNT = int(os.environ.get("MMC_SNIPPET_COUNT", "4"))
SNIPPET_SEC = float(os.environ.get("MMC_SNIPPET_SEC", "6"))
# The opening of a meeting is join noise and overlapping greetings. Clips taken
# from it are the worst possible evidence to hand someone for a decision.
SNIPPET_SKIP_OPENING_SEC = float(os.environ.get("MMC_SNIPPET_SKIP_OPENING", "90"))
# Reject a candidate clip whose aligned words cover less than this of its span.
SNIPPET_MIN_WORD_COVERAGE = 0.5
# Minimum separation between chosen clips, so three clips are not three slices of
# one sentence.
SNIPPET_MIN_SEPARATION_SEC = 60.0

ALL_DIRS.append(SNIPPETS_DIR)


def _optional_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


# Speaker count bounds passed to pyannote. Unset means "let it decide", which is
# right for a mixed calendar. Setting them helps materially when you know the
# shape: pyannote over-segments a two-person 1:1 into three or four speakers when
# audio is noisy, and under-counts a large meeting with overlapping speech.
# Diarization accuracy is the main lever on whether action items get correct
# owners, so a known bound is worth supplying.
MIN_SPEAKERS = _optional_int("MMC_MIN_SPEAKERS")
MAX_SPEAKERS = _optional_int("MMC_MAX_SPEAKERS")

# Above this, warn: the count is more likely over-segmentation than a real crowd,
# and speaker names will be unreliable.
IMPLAUSIBLE_SPEAKER_COUNT = int(os.environ.get("MMC_IMPLAUSIBLE_SPEAKERS", "8"))
# pyannote models are gated: needs a HF read token AND manual acceptance of the
# terms for pyannote/speaker-diarization-3.1 and pyannote/segmentation-3.0.
# This fails at runtime, not at install time.
HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")


# ── LLM providers ─────────────────────────────────────────────────────
# Priority order, highest first. Tried in sequence, falling through on failure,
# so a quota limit on the preferred provider does not stall a nightly batch that
# has already paid for transcription.
#
# All providers are subscription-backed rather than metered APIs. They author
# minutes, entities, relations, and answers. LightRAG is graph storage and
# traversal only; its model-backed document and query routes are not used.
_configured_llm_providers = {
    name.strip()
    for name in os.environ.get(
        "MMC_LLM_PROVIDERS", "codex,claude,antigravity"
    ).split(",")
    if name.strip()
}
# The environment may select a subset, but cannot reorder providers or add an
# older/dead path from a stale .env. This is a policy, not a preference hint.
LLM_PROVIDER_ORDER = [
    name
    for name in ("codex", "claude", "antigravity")
    if name in {configured.lower() for configured in _configured_llm_providers}
]

# Antigravity is the final configured fallback after Codex and Claude.
ANTIGRAVITY_BIN = os.environ.get("MMC_ANTIGRAVITY_BIN", "agy")
# `agy models` lists gemini-3.7-flash-{high,medium,low}. The suffix is reasoning
# effort, not a version: medium is the sensible default for summarisation, and
# the pipeline's prompts are strict output-format asks rather than open problems.
ANTIGRAVITY_MODEL = os.environ.get("MMC_ANTIGRAVITY_MODEL", "gemini-3.7-flash-medium")

# The standalone `gemini` CLI's own registry tops out at gemini-3.5-flash and it
# does not validate ids locally, so a bad id is forwarded and fails server-side
# rather than failing fast. Note this is a DIFFERENT namespace from Antigravity's:
# "gemini-3.7-flash" is real there and unknown here.
GEMINI_MODEL = os.environ.get("MMC_GEMINI_MODEL", "gemini-3.7-flash")

# Per-call ceiling. Generous: a full transcript is a large prompt, and a CLI that
# wants a TTY would otherwise hang the batch indefinitely.
LLM_TIMEOUT_SEC = float(os.environ.get("MMC_LLM_TIMEOUT", "900"))

OWNER_NAME = os.environ.get("MMC_OWNER_NAME", "")

# ── Alerting ──────────────────────────────────────────────────────────
# Command invoked when the nightly batch fails, with the summary on stdin and
# {subject} substituted. A command rather than built-in email/webhook support:
# whatever the server already has beats a second notification stack.
#   MMC_ALERT_COMMAND=curl -s -d @- https://ntfy.sh/my-topic
#   MMC_ALERT_COMMAND=mail -s "{subject}" me@example.com
ALERT_COMMAND = os.environ.get("MMC_ALERT_COMMAND", "")
ALERT_TIMEOUT_SEC = float(os.environ.get("MMC_ALERT_TIMEOUT", "30"))


# ── Minutes compilation ───────────────────────────────────────────────
# Bump when templates/minutes.md changes semantically. Stamped into every
# minutes file's frontmatter so `pipeline minutes --recompile` can find stale
# documents and rebuild them from retained transcripts without re-running ASR.
TEMPLATE_VERSION = "2"

# Below either floor, `pipeline minutes` parks the meeting instead of spending a
# full LLM compile on it. Accidental phone-in-pocket recordings and short test
# clips still produce a document and a card in the meeting library same as a
# real meeting - at roughly five a week that is pure recurring waste. A genuine
# sub-two-minute decision is rare but real, so this parks rather than discards:
# `pipeline minutes --force` compiles a short meeting deliberately.
MIN_MEETING_SEC = float(os.environ.get("MMC_MIN_MEETING_SEC", "120"))
# A meeting can clear the duration floor and still be near-silent; word count
# catches that independently, so either floor alone is enough to park.
MIN_TRANSCRIPT_WORDS = int(os.environ.get("MMC_MIN_TRANSCRIPT_WORDS", "150"))

# Structured and comprehensive, not a 5-bullet executive summary. Summaries drop
# rationale, and rationale is what answers "why did we deprioritize X".
#
# A floor with no ceiling, deliberately. The old fixed 600-1200 band was applied
# identically to a five-minute standup and a ninety-minute planning session:
# measured across the corpus that kept 35% of the transcript's words on average
# but only 9.5% of the longest meeting (15,291 words compressed into 1,454). The
# meetings worth the most were compressed the hardest. Length should follow the
# meeting, so MAX defaults to 0, meaning unbounded; set MMC_MINUTES_WORDS_MAX to
# a positive number to reimpose a ceiling.
MINUTES_TARGET_WORDS_MIN = int(os.environ.get("MMC_MINUTES_WORDS_MIN", "1200"))
MINUTES_TARGET_WORDS_MAX = int(os.environ.get("MMC_MINUTES_WORDS_MAX", "0"))

# How many prior related minutes to feed the compiler so it can flag decisions
# that reverse earlier positions.
PRIOR_CONTEXT_DOCS = 3

# Ceiling on the dialogue portion of the minutes prompt. A one-hour meeting is
# ~13k tokens and fits comfortably; a three-hour one does not. Over budget, the
# compiler switches to map-reduce rather than failing or silently truncating.
MINUTES_PROMPT_TOKEN_BUDGET = int(os.environ.get("MMC_MINUTES_TOKEN_BUDGET", "60000"))

# Window size for the map pass, in tokens of dialogue.
MINUTES_MAP_WINDOW_TOKENS = int(os.environ.get("MMC_MINUTES_MAP_WINDOW", "20000"))

# ── LightRAG ──────────────────────────────────────────────────────────
LIGHTRAG_URL = os.environ.get("MMC_LIGHTRAG_URL", "http://localhost:9621")
LIGHTRAG_API_KEY = os.environ.get("MMC_LIGHTRAG_API_KEY", "")
LIGHTRAG_TIMEOUT = float(os.environ.get("MMC_LIGHTRAG_TIMEOUT", "600"))
# hybrid = graph + vector. Use "global" for aggregative questions whose answer
# spans many meetings ("summarize all budget discussion this year").
LIGHTRAG_DEFAULT_MODE = os.environ.get("MMC_LIGHTRAG_MODE", "hybrid")

# ── Ask AI conversation history ─────────────────────────────────────────
# Hard cap on how many prior turns of a chat session join a synthesis prompt,
# regardless of how many the dashboard has stored - a long-running session
# must not grow the prompt without bound. answer.py trims further by an
# approximate token budget on top of this turn count.
CHAT_HISTORY_TURNS = int(os.environ.get("MMC_CHAT_HISTORY_TURNS", "6"))

# ── Local meeting-memory dashboard ────────────────────────────────────
# It renders private minutes and Drive links, so LAN exposure requires an explicit
# environment override. The default remains available only on this machine.
DASHBOARD_HOST = os.environ.get("MMC_DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.environ.get("MMC_DASHBOARD_PORT", "8765"))

# Shared secret for the dashboard. Empty is fine while the dashboard is bound to
# loopback - that is the existing single-user setup and the bind address is the
# boundary. Binding anywhere else without this set makes the dashboard refuse to
# start, because it serves meeting minutes and DELETE /api/meetings/{id}.
# Generate one with: python -c "import secrets;print(secrets.token_urlsafe(32))"
DASHBOARD_TOKEN = os.environ.get("MMC_DASHBOARD_TOKEN", "").strip()

# ── Google Drive capture ─────────────────────────────────────────────
# Audio reaches this pipeline through a private Drive folder populated by Easy
# Voice Recorder Pro. Drive is the durable raw-audio archive; this machine only
# keeps an audio file long enough to make the retained transcript.
APP_DATA_DIR = Path(
    os.environ.get(
        "MMC_APP_DATA",
        Path(os.environ.get("LOCALAPPDATA", ROOT_DIR / ".local")) / "MeetingMinutesCompiler",
    )
)
DRIVE_CREDENTIALS_FILE = Path(
    os.environ.get("MMC_DRIVE_CREDENTIALS", APP_DATA_DIR / "drive-client.json")
)
DRIVE_TOKEN_FILE = Path(os.environ.get("MMC_DRIVE_TOKEN", APP_DATA_DIR / "drive-token.json"))
DRIVE_FUTURE_FOLDER_ID = os.environ.get("MMC_DRIVE_FUTURE_FOLDER_ID", "")
DRIVE_BACKFILL_FOLDER_ID = os.environ.get("MMC_DRIVE_BACKFILL_FOLDER_ID", "")
DRIVE_BACKFILL_CUTOFF = os.environ.get("MMC_DRIVE_BACKFILL_CUTOFF", "2026-06-09")
DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive.readonly",)


# ── Time helpers ──────────────────────────────────────────────────────

def now() -> datetime:
    """Current time in the configured timezone."""
    return datetime.now(TZ)


def now_iso() -> str:
    """Current local time, ISO 8601, second precision."""
    return now().isoformat(timespec="seconds")


def today_iso() -> str:
    """Current local date as YYYY-MM-DD."""
    return now().strftime("%Y-%m-%d")


def ensure_dirs() -> None:
    """Create every working directory. Safe to call repeatedly."""
    for directory in ALL_DIRS:
        directory.mkdir(parents=True, exist_ok=True)

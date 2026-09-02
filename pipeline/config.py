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
# requests went out unauthenticated. Real environment variables still win, which is
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

# ── Remote transcription ──────────────────────────────────────────────
# Replicate is the only supported ASR backend. There is intentionally no local
# device, model, compute-type, or fallback configuration.
ASR_BATCH_SIZE = int(os.environ.get("MMC_ASR_BATCH_SIZE", "8"))
ASR_LANGUAGE = os.environ.get("MMC_ASR_LANGUAGE", "en")

# Replicate serverless GPU configuration
REPLICATE_API_TOKEN = (
    os.environ.get("REPLICATE_API_TOKEN", "") or os.environ.get("REPLICATE_API_KEY", "")
).strip()
REPLICATE_MODEL = os.environ.get("MMC_REPLICATE_MODEL", "victor-upmeet/whisperx").strip()
REPLICATE_TIMEOUT_SEC = float(os.environ.get("MMC_REPLICATE_TIMEOUT", "600"))
REPLICATE_POLL_INTERVAL_SEC = float(os.environ.get("MMC_REPLICATE_POLL_INTERVAL", "2.0"))

# Large upload bodies get reset mid-transfer on this network path: measured
# success per attempt is 8/8 at <=5 MB, ~50% at 10 MB, and ~33% at 20-35 MB, with
# the reset arriving 1-2s in. Three attempts inside a ~7-second window therefore
# lost every recording over ~30 minutes long and parked six of them at `failed`,
# so no minutes were ever compiled. Eight attempts on a capped exponential
# backoff take a 33% per-attempt success rate to ~96% overall.
REPLICATE_UPLOAD_ATTEMPTS = int(os.environ.get("MMC_REPLICATE_UPLOAD_ATTEMPTS", "8"))
REPLICATE_UPLOAD_BACKOFF_SEC = float(
    os.environ.get("MMC_REPLICATE_UPLOAD_BACKOFF", "5.0")
)
REPLICATE_UPLOAD_BACKOFF_MAX_SEC = float(
    os.environ.get("MMC_REPLICATE_UPLOAD_BACKOFF_MAX", "60.0")
)

# Even eight attempts lose a recording if the rough patch outlasts them, and a
# row parked at FAILED is only recovered by a human reading the manifest - six
# of them sat there for a day with no minutes. Each later run spends one of
# these on a network-faulted meeting; the budget keeps a real multi-day outage
# from requeueing the same recording forever.
AUTO_REQUEUE_LIMIT = int(os.environ.get("MMC_AUTO_REQUEUE_LIMIT", "3"))

# Upload encoding. Reset probability climbs with payload size, so the cheapest
# reliability win is sending less: 32 kbps Opus is ~2x smaller than the 64 kbps
# MP3 it replaces (a 72-minute meeting: ~35 MB -> ~16 MB) and upload time drops
# with it. Verified against 64k MP3 on a 7-minute six-speaker sample - word
# counts within 1% and a diarization talk-split of 47/18/15/14 against 46/19/16/14,
# closer than two runs of the same file differ from each other. Opus below 32k
# starts merging the quietest speakers, so lower it only deliberately.
UPLOAD_AUDIO_CODEC = os.environ.get("MMC_UPLOAD_CODEC", "libopus").strip()
UPLOAD_AUDIO_BITRATE = os.environ.get("MMC_UPLOAD_BITRATE", "32k").strip()

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
# The encoder that produced every stored vector. Vectors from different encoders
# are not comparable, so this string namespaces voice_samples and speaker_matches.
#
# This is only the fallback. The live value is the manifest setting
# voice.active_namespace, read through voices.active_namespace() - so swapping
# encoders is one row rather than a deploy. The default was "historical" while
# local enrollment was retired; because nothing was ever written under that name,
# every namespaced read came back empty and cluster_pending() deleted the review
# queue on each dashboard load. A default that no stored row uses is not a
# quarantine, it is an outage.
VOICE_VECTOR_NAMESPACE = os.environ.get(
    "MMC_VOICE_VECTOR_NAMESPACE", "pyannote/wespeaker-voxceleb-resnet34-LM"
)

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

# Retained clip encoding. Opus at 24k keeps a 6-second clip near 18 KB, so a
# whole corpus of review snippets stays smaller than one hour of source audio.
SNIPPET_EXT = ".opus"
SNIPPET_BITRATE = "24k"

# Cap on the audio sent to the embedding provider for one label. A chronological
# truncation, not a quality selection: VOICE_MIN_SPEECH_SEC is already the floor
# for a robust sample, and this only bounds cost on someone who talks for twenty
# minutes.
VOICE_MAX_EMBED_SEC = float(os.environ.get("MMC_VOICE_MAX_EMBED_SEC", "90"))

# The remote speaker-embedding model. Unset by default and the voice stage
# no-ops without it: this is the only paid call in the stage, and a model that
# silently starts running because a default existed is a bill nobody approved.
REMOTE_VOICE_MODEL = os.environ.get("MMC_REMOTE_VOICE_MODEL", "").strip()
# Pinned version hash of that model, set after the encoder benchmark. Separate
# from the model name because the namespace every vector is keyed by is
# `encoder@version`: the name alone does not say which weights answered.
REMOTE_VOICE_VERSION = os.environ.get("MMC_REMOTE_VOICE_VERSION", "").strip()
# Which encoder the cog should serve. It hosts several behind one interface, and
# `wespeaker-resnet34-lm` is the same representation family as the vectors
# already in the corpus - the choice that lets the existing enrollments carry
# over instead of restarting.
REMOTE_VOICE_ENCODER = os.environ.get(
    "MMC_REMOTE_VOICE_ENCODER", "wespeaker-resnet34-lm"
).strip()

ALL_DIRS.append(SNIPPETS_DIR)


def _optional_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


# Speaker count bounds passed to the hosted diarizer. Unset means "let it decide",
# which is right for a mixed calendar. Setting them helps materially when you know
# the shape: a hosted diarizer can over-segment a two-person 1:1 into three or four speakers when
# audio is noisy, and under-counts a large meeting with overlapping speech.
# Diarization accuracy is the main lever on whether action items get correct
# owners, so a known bound is worth supplying.
MIN_SPEAKERS = _optional_int("MMC_MIN_SPEAKERS")
MAX_SPEAKERS = _optional_int("MMC_MAX_SPEAKERS")

# Above this, warn: the count is more likely over-segmentation than a real crowd,
# and speaker names will be unreliable.
IMPLAUSIBLE_SPEAKER_COUNT = int(os.environ.get("MMC_IMPLAUSIBLE_SPEAKERS", "8"))
# Passed only to the remote ASR provider when its selected hosted model requires
# it. It is never used to load a model in this process.
REMOTE_ASR_HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")


# ── LLM providers ─────────────────────────────────────────────────────
# Priority order, highest first. Tried in sequence on an operator-started run.
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
# Command invoked when an operator-started pipeline run fails, with the summary
# on stdin and {subject} substituted. A command rather than built-in
# email/webhook support: whatever the server already has beats a second
# notification stack.
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

# Dialogue budget for the speaker-resolution prompt. The resolver used to see a
# ~100-line sample of the meeting; measured against 37 human-confirmed labels,
# the whole transcript took it from 18% to 73% correct on meetings with a
# plausible speaker count. Over this budget it falls back to the sample rather
# than truncating - a truncated transcript loses the END of the meeting
# silently, and that is where a late joiner introduces themselves.
RESOLUTION_PROMPT_TOKEN_BUDGET = int(
    os.environ.get("MMC_RESOLUTION_TOKEN_BUDGET", "40000")
)

# Above this many diarized labels the resolver goes back to the sample.
# Deliberately stricter than IMPLAUSIBLE_SPEAKER_COUNT (8): that one asks whether
# the diarizer over-segmented, this asks a different question - how many
# label-to-person assignments the model can hold at once before it starts
# inventing them. Measured over 66 human-confirmed labels across 20 meetings:
#
#   labels   sample (right/wrong)   full transcript (right/wrong)
#    2-5          6 / 1                    19 / 0
#    6-7          4 / 0                     6 / 1
#      8          5 / 2                    10 / 4
#     9+          3 / 2                     4 / 6
#
# The full transcript is an unambiguous win up to five speakers and buys every
# further correct answer with a wrong one after eight. A confident wrong name is
# the failure this pipeline cannot fix, so the gate sits below the crossover
# rather than at it.
RESOLUTION_FULL_DIALOGUE_MAX_SPEAKERS = int(
    os.environ.get("MMC_RESOLUTION_MAX_SPEAKERS", "6")
)

# ── LightRAG ──────────────────────────────────────────────────────────
LIGHTRAG_URL = os.environ.get("MMC_LIGHTRAG_URL", "http://localhost:9621")
LIGHTRAG_API_KEY = os.environ.get("MMC_LIGHTRAG_API_KEY", "")
LIGHTRAG_TIMEOUT = float(os.environ.get("MMC_LIGHTRAG_TIMEOUT", "600"))
# hybrid = graph + vector. Use "global" for aggregative questions whose answer
# spans many meetings ("summarize all budget discussion this year").
LIGHTRAG_DEFAULT_MODE = os.environ.get("MMC_LIGHTRAG_MODE", "hybrid")

# ── Dense retrieval ───────────────────────────────────────────────────
# The local embedding model behind `pipeline dense-index`. 384 dimensions,
# English, CPU-only through the onnxruntime fastembed already rides on - no API
# key and no metered call, which is why this has a working default while the
# remote voice encoder above deliberately does not.
#
# Changing it invalidates every stored vector. Vectors are namespaced by this id
# and a search only ever reads its own model's rows, so a swap silently empties
# the dense half until `pipeline dense-index` has run again.
EMBED_MODEL = os.environ.get("MMC_EMBED_MODEL", "BAAI/bge-small-en-v1.5").strip()

# The cross-encoder that reorders a dense shortlist. Fetched only when
# something actually reranks - retrieval works without it, at lower ranking
# quality, so nothing here forces a download.
#
# The small model is the default because disk is the binding constraint on the
# machine this runs on: 4.4 GB free of 456 GB. At 0.12 GB this and the embedder
# together come to about 0.19 GB. `BAAI/bge-reranker-base` ranks better and is
# the right value for `MMC_RERANK_MODEL` on a machine with room, but it is
# 1.04 GB and cannot be the default here.
RERANK_MODEL = os.environ.get("MMC_RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-12-v2").strip()

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

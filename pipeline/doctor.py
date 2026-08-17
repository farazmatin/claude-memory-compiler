"""Preflight checks.

Most of what can go wrong in this pipeline fails at *runtime*, not install time, and
several failures are silent-ish: a missing HF token skips diarization with a printed
warning nobody reads, and a wrong embedding dimension surfaces as an opaque error
deep inside an insert.

This collects those checks into one command so the environment can be verified
before a real batch, on the real machine.

**What this does not do.** It verifies the environment is ready, not that the
pipeline produces good output. Transcription accuracy, diarization quality and
graph usefulness can only be judged by running one real meeting through and reading
the result. `doctor` tells you nothing is obviously broken; it cannot tell you the
minutes are any good.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

from pipeline import db
from pipeline.config import (
    ASR_DEVICE,
    ASR_MODEL,
    AUDIO_DIR,
    DB_PATH,
    DRIVE_CREDENTIALS_FILE,
    DRIVE_TOKEN_FILE,
    ENABLE_DIARIZATION,
    GLOSSARY_FILE,
    HF_TOKEN,
    INBOX_DIR,
    LIGHTRAG_API_KEY,
    LIGHTRAG_URL,
    LLM_PROVIDER_ORDER,
    MINUTES_DIR,
    TRANSCRIPTS_DIR,
)

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""

    @property
    def symbol(self) -> str:
        return {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}[self.status]


def check_ffmpeg() -> list[Check]:
    """ffmpeg normalizes audio; ffprobe reads duration."""
    checks = []
    for binary, consequence in (
        ("ffmpeg", "transcription cannot run at all"),
        ("ffprobe", "durations will be unknown (non-fatal)"),
    ):
        if shutil.which(binary):
            checks.append(Check(binary, OK, "found"))
        else:
            checks.append(
                Check(
                    binary,
                    FAIL if binary == "ffmpeg" else WARN,
                    f"not on PATH - {consequence}",
                    "install ffmpeg",
                )
            )
    return checks


def check_asr() -> list[Check]:
    """whisperx and the model choice."""
    checks = []

    try:
        import whisperx  # noqa: F401

        checks.append(Check("whisperx", OK, "importable"))
    except ImportError as exc:
        return [
            Check("whisperx", FAIL, f"not importable: {exc}", "uv sync --extra asr")
        ]

    if ASR_DEVICE == "cpu" and ASR_MODEL == "large-v3":
        checks.append(
            Check(
                "asr model",
                WARN,
                "large-v3 on CPU costs ~1.5-2.5 h per meeting with diarization; "
                "five a day will not fit a night",
                "unset MMC_ASR_MODEL to use large-v3-turbo",
            )
        )
    else:
        checks.append(Check("asr model", OK, f"{ASR_MODEL} on {ASR_DEVICE}"))
    return checks


def check_diarization() -> list[Check]:
    """pyannote is gated: a token AND manual acceptance of two model licences.

    This is the single most common silent degradation - without it you get
    transcripts with no speaker attribution, which means action items with no
    owners, and the only signal is a printed warning mid-batch.
    """
    if not ENABLE_DIARIZATION:
        return [Check("diarization", WARN, "disabled - action items will have no owners")]
    if not HF_TOKEN:
        return [
            Check(
                "diarization",
                FAIL,
                "HF_TOKEN unset - diarization will be skipped silently",
                "set HF_TOKEN and accept the terms for "
                "pyannote/speaker-diarization-3.1 and pyannote/segmentation-3.0",
            )
        ]

    # A token proves nothing about licence acceptance, which is the part people
    # miss. Try to actually load the gated config.
    try:
        from huggingface_hub import HfApi

        HfApi().model_info("pyannote/speaker-diarization-3.1", token=HF_TOKEN)
        return [Check("diarization", OK, "token valid, gated model reachable")]
    except ImportError:
        return [
            Check("diarization", WARN, "HF_TOKEN set; cannot verify gated access here")
        ]
    except Exception as exc:
        return [
            Check(
                "diarization",
                FAIL,
                f"gated model not reachable: {type(exc).__name__}",
                "accept the licence at hf.co/pyannote/speaker-diarization-3.1",
            )
        ]


def check_providers() -> list[Check]:
    """At least one LLM provider must be reachable, or stages 3 and 4 cannot run."""
    from pipeline import llm

    checks = []
    available = []
    for provider in llm.build_chain():
        if provider.available():
            available.append(provider.name)
            checks.append(Check(f"provider {provider.name}", OK, "available"))
        else:
            checks.append(
                Check(
                    f"provider {provider.name}",
                    WARN,
                    "not available - chain will fall through past it",
                )
            )

    if not available:
        checks.append(
            Check(
                "llm chain",
                FAIL,
                f"none of {LLM_PROVIDER_ORDER} available - minutes cannot be compiled",
                "install a CLI or the claude-agent-sdk",
            )
        )
    else:
        checks.append(Check("llm chain", OK, f"will use {available[0]}"))
    return checks


def check_lightrag() -> list[Check]:
    """Reachability plus the two configuration mistakes that matter."""
    from pipeline import index

    checks = []
    if not LIGHTRAG_API_KEY:
        checks.append(
            Check(
                "lightrag auth",
                WARN,
                "MMC_LIGHTRAG_API_KEY unset - requests will be unauthenticated",
                "set it in .env (compose requires it too)",
            )
        )
    else:
        checks.append(Check("lightrag auth", OK, "api key set"))

    try:
        info = index.health()
        checks.append(Check("lightrag", OK, f"reachable at {LIGHTRAG_URL}"))

        # Storage backend: the file-based defaults do not hold this corpus.
        backend = str(info.get("configuration", {}).get("kv_storage", "")) or ""
        if backend and not backend.startswith("PG"):
            checks.append(
                Check(
                    "lightrag storage",
                    WARN,
                    f"{backend} is file-based and will not hold thousands of documents",
                    "use the provided docker-compose.yml (Postgres storage)",
                )
            )
    except index.IndexError_ as exc:
        checks.append(
            Check("lightrag", FAIL, str(exc)[:160], "docker compose up -d")
        )
    return checks


def check_storage() -> list[Check]:
    """Directories exist and there is room to work."""
    checks = []
    for label, path in (
        ("inbox", INBOX_DIR),
        ("audio", AUDIO_DIR),
        ("transcripts", TRANSCRIPTS_DIR),
        ("minutes", MINUTES_DIR),
    ):
        if path.exists():
            checks.append(Check(f"dir {label}", OK, str(path)))
        else:
            checks.append(
                Check(f"dir {label}", WARN, f"missing: {path}", "pipeline init")
            )

    try:
        usage = shutil.disk_usage(MINUTES_DIR if MINUTES_DIR.exists() else ".")
        free_gb = usage.free / (1024**3)
        # A year of audio at ~18 MB/hr is ~33 GB, plus transcripts and Postgres.
        if free_gb < 20:
            checks.append(
                Check(
                    "disk",
                    WARN,
                    f"{free_gb:.0f} GB free - a year of audio is roughly 33 GB",
                    "point MMC_AUDIO at a larger disk",
                )
            )
        else:
            checks.append(Check("disk", OK, f"{free_gb:.0f} GB free"))
    except OSError as exc:
        checks.append(Check("disk", WARN, f"could not check: {exc}"))
    return checks


def check_manifest() -> list[Check]:
    checks = []
    if not DB_PATH.exists():
        return [Check("manifest", WARN, "not created yet", "pipeline init")]
    try:
        db.init_db()
        with db.connect() as conn:
            counts = db.status_counts(conn)
        total = sum(counts.values())
        checks.append(Check("manifest", OK, f"{total} meeting(s) tracked"))
        if counts.get(db.FAILED):
            checks.append(
                Check(
                    "failed meetings",
                    WARN,
                    f"{counts[db.FAILED]} parked - see `pipeline status`",
                    "pipeline retry",
                )
            )
    except Exception as exc:
        checks.append(Check("manifest", FAIL, f"{type(exc).__name__}: {exc}"))
    return checks


def check_glossary() -> list[Check]:
    """Not required, but the cheapest accuracy win available."""
    if not GLOSSARY_FILE.exists():
        return [Check("glossary", WARN, "missing", "create glossary.md")]
    from pipeline.asr import load_glossary_terms

    terms = load_glossary_terms()
    if len(terms) < 3:
        return [
            Check(
                "glossary",
                WARN,
                f"only {len(terms)} term(s) - add product and people names",
                "a mangled product name fragments the knowledge graph",
            )
        ]
    return [Check("glossary", OK, f"{len(terms)} terms")]


def check_ollama() -> list[Check]:
    """Ollama serves LightRAG's extraction and embeddings.

    Checked via the CLI when present; the HTTP check is LightRAG's own health.
    """
    if not shutil.which("docker"):
        return [Check("ollama", WARN, "docker not on PATH; cannot verify models")]
    try:
        result = subprocess.run(
            ["docker", "exec", "mmc-ollama", "ollama", "list"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [Check("ollama", WARN, f"could not query: {type(exc).__name__}")]

    if result.returncode != 0:
        return [
            Check("ollama", WARN, "container not running", "docker compose up -d")
        ]

    listed = result.stdout
    missing = [m for m in ("qwen3", "mxbai-embed-large") if m not in listed]
    if missing:
        return [
            Check(
                "ollama models",
                FAIL,
                f"missing: {', '.join(missing)} - indexing will fail",
                "docker compose exec ollama ollama pull " + " && ".join(missing),
            )
        ]
    return [Check("ollama models", OK, "extraction and embedding models present")]


def check_postgres() -> list[Check]:
    """Postgres holds LightRAG's KV, vector, doc-status and graph data.

    Without it, LightRAG silently falls back to file-based storage which does
    not hold thousands of documents. This is the most common undetected failure
    after HF_TOKEN: everything appears to work, but the index lives in
    throwaway JSON files instead of the durable pgvector tables.
    """
    if not shutil.which("docker"):
        return [Check("postgres", WARN, "docker not on PATH; cannot verify")]
    try:
        result = subprocess.run(
            ["docker", "exec", "mmc-postgres", "pg_isready", "-U", "lightrag", "-d", "rag"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [Check("postgres", WARN, f"could not query: {type(exc).__name__}")]

    if result.returncode != 0:
        return [
            Check(
                "postgres", FAIL,
                "mmc-postgres not running - LightRAG falls back to file-based storage",
                "docker compose up -d",
            )
        ]
    return [Check("postgres", OK, "accepting connections")]


def check_drive() -> list[Check]:
    """Google Drive OAuth credentials for nightly audio capture.

    Drive capture is optional - the pipeline works with local inbox drops - but
    the nightly unattended mode depends on it.
    """
    checks = []
    if DRIVE_CREDENTIALS_FILE.exists():
        checks.append(Check("drive client", OK, "configured"))
    else:
        checks.append(
            Check(
                "drive client", WARN,
                f"missing: {DRIVE_CREDENTIALS_FILE}",
                "download OAuth client JSON from Google Cloud Console",
            )
        )

    if DRIVE_TOKEN_FILE.exists():
        checks.append(Check("drive token", OK, "configured"))
    else:
        checks.append(
            Check(
                "drive token", WARN,
                f"missing: {DRIVE_TOKEN_FILE}",
                "run: pipeline auth-drive",
            )
        )
    return checks


def check_nightly_task() -> list[Check]:
    """Windows Scheduled Task for unattended nightly runs.

    Only checked on Windows; other platforms use cron, which is out of scope.
    """
    if sys.platform != "win32":
        return []  # Not applicable on non-Windows

    task_name = "Meeting Minutes Compiler - Nightly"
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f'Get-ScheduledTask -TaskName "{task_name}" -ErrorAction Stop '
             f'| Select-Object -ExpandProperty State'],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [Check("nightly task", WARN, f"could not check: {type(exc).__name__}")]

    if result.returncode != 0:
        return [
            Check(
                "nightly task", WARN,
                "not installed - pipeline will not run automatically",
                'run: .\\scripts\\install-nightly-task.ps1 -Owner "Your Name"',
            )
        ]

    state = result.stdout.strip()
    if state == "Ready":
        return [Check("nightly task", OK, "scheduled and ready")]
    return [
        Check(
            "nightly task", WARN,
            f"exists but state is '{state}'",
            "check Task Scheduler for errors",
        )
    ]


ALL_CHECKS = (
    check_ffmpeg,
    check_asr,
    check_diarization,
    check_providers,
    check_postgres,
    check_lightrag,
    check_ollama,
    check_drive,
    check_storage,
    check_manifest,
    check_glossary,
    check_nightly_task,
)


def run() -> tuple[list[Check], bool]:
    """Run every check. Returns (checks, ok) where ok means nothing FAILed."""
    checks: list[Check] = []
    for check in ALL_CHECKS:
        try:
            checks.extend(check())
        except Exception as exc:
            checks.append(Check(check.__name__, WARN, f"check errored: {exc}"))
    return checks, not any(c.status == FAIL for c in checks)

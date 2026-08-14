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

**No secret ever reaches this output.** Every check reports `configured` or
`missing` and where to fix it, never a value. A `doctor` report is meant to be
safe to paste into a bug report or read out over a call.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import asdict, dataclass

from pipeline import db, env
from pipeline.config import (
    ASR_DEVICE,
    ASR_MODEL,
    AUDIO_DIR,
    DASHBOARD_HOST,
    DASHBOARD_PORT,
    DB_PATH,
    DRIVE_BACKFILL_FOLDER_ID,
    DRIVE_CREDENTIALS_FILE,
    DRIVE_FUTURE_FOLDER_ID,
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

# The scheduled job that makes the whole thing unattended. Named here rather than
# only in the PowerShell installer so `doctor` can tell you it is missing.
NIGHTLY_TASK_NAME = "Meeting Minutes Compiler - Nightly"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""

    @property
    def symbol(self) -> str:
        return {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}[self.status]

    def as_dict(self) -> dict[str, str]:
        """Serializable form, for `pipeline doctor --json`.

        The setup scripts branch on individual checks - "is Drive authorized
        yet" - and parsing the human table for that is how a check silently stops
        being enforced when its wording changes.
        """
        return {**asdict(self), "symbol": self.symbol.strip()}


def check_env() -> list[Check]:
    """The `.env` file and the secrets it is supposed to carry.

    Separate from the checks that exercise those secrets - `diarization` proves a
    HuggingFace token *works*, this proves one was *supplied*. Both matter: a
    missing value and a rejected value need different fixes, and collapsing them
    into one line sends people to the wrong one.
    """
    checks: list[Check] = []
    env_file = env.ENV_FILE
    if env_file.exists():
        checks.append(Check("env file", OK, f"loaded {env_file}"))
    else:
        checks.append(
            Check(
                "env file",
                WARN,
                f"not found at {env_file}",
                "run scripts/setup.ps1 (Windows) or ./setup.sh, or: uv run pipeline config init",
            )
        )

    for key in env.REQUIRED_SECRETS:
        if env.is_configured(key):
            checks.append(Check(f"env {key}", OK, env.CONFIGURED))
        else:
            checks.append(
                Check(
                    f"env {key}",
                    FAIL,
                    f"{env.MISSING} - docker compose refuses to start without it",
                    "uv run pipeline config init (generates it; existing values "
                    "are left alone)",
                )
            )

    for key in env.MANUAL_SECRETS:
        if env.is_configured(key):
            checks.append(Check(f"env {key}", OK, env.CONFIGURED))
        else:
            checks.append(
                Check(
                    f"env {key}",
                    WARN,
                    f"{env.MISSING} - see the diarization check below",
                    "uv run pipeline config set HF_TOKEN, after creating a read "
                    "token at https://huggingface.co/settings/tokens",
                )
            )
    return checks


def check_drive() -> list[Check]:
    """Google Drive capture: configured, authorized, and actually able to list.

    Drive is where audio comes from on an unattended machine. A refresh token that
    has been revoked - or a folder that was renamed out from under the id - looks
    exactly like a quiet week of no meetings, so this reaches the API rather than
    trusting that the files on disk are still good.
    """
    if not (DRIVE_FUTURE_FOLDER_ID or DRIVE_BACKFILL_FOLDER_ID):
        return [
            Check(
                "drive capture",
                WARN,
                "not configured - only files dropped into inbox/ will be processed",
                "set MMC_DRIVE_FUTURE_FOLDER_ID in .env to the private recordings folder",
            )
        ]

    checks: list[Check] = []
    if not DRIVE_CREDENTIALS_FILE.exists():
        return [
            Check(
                "drive client",
                FAIL,
                f"OAuth desktop client missing: {DRIVE_CREDENTIALS_FILE}",
                "create one at console.cloud.google.com and save it to that path",
            )
        ]
    checks.append(Check("drive client", OK, "OAuth desktop client present"))

    if not DRIVE_TOKEN_FILE.exists():
        checks.append(
            Check(
                "drive auth",
                FAIL,
                "not authorized - nightly capture will download nothing",
                "run `pipeline auth-drive` once and approve read-only access",
            )
        )
        return checks

    from pipeline import capture

    folder_id = DRIVE_FUTURE_FOLDER_ID or DRIVE_BACKFILL_FOLDER_ID
    try:
        listed = capture.google_drive_client().list_files(folder_id)
    except Exception as exc:
        checks.append(
            Check(
                "drive auth",
                FAIL,
                f"cannot list the recordings folder: {type(exc).__name__}",
                "re-run `pipeline auth-drive`; the refresh token may have been revoked",
            )
        )
        return checks

    checks.append(Check("drive auth", OK, f"authorized, {len(listed)} file(s) visible"))
    return checks


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

        # Storage backends: the file-based defaults do not hold this corpus.
        file_based = _file_based_backends(info)
        if file_based:
            checks.append(
                Check(
                    "lightrag storage",
                    WARN,
                    f"{', '.join(file_based)} file-based - will not hold thousands "
                    f"of documents",
                    "use the provided docker-compose.yml (Postgres storage), then "
                    "scripts/migrate-to-postgres.ps1 to re-index what exists",
                )
            )
    except index.IndexError_ as exc:
        checks.append(
            Check("lightrag", FAIL, str(exc)[:160], "docker compose up -d")
        )
    return checks


# The four stores LightRAG keeps. All of them have file-based defaults, and all
# four have to move together: a Postgres vector store paired with a JSON graph is
# a corpus that half survives a restart.
STORAGE_KEYS = ("kv_storage", "vector_storage", "doc_status_storage", "graph_storage")


def _file_based_backends(info: dict) -> list[str]:
    """Configured stores that are not Postgres-backed, as `key=Backend` strings.

    Only reports what the server actually names. An older LightRAG that omits a
    key from `/health` is not evidence that the key is misconfigured.
    """
    configuration = info.get("configuration") or {}
    reported = {key: str(configuration.get(key, "") or "") for key in STORAGE_KEYS}
    return [
        f"{key}={backend}"
        for key, backend in reported.items()
        if backend and not backend.startswith("PG")
    ]


def check_postgres() -> list[Check]:
    """Postgres is the storage the corpus is sized for; confirm it is really in use.

    Two independent things can be wrong. The database can be down, which is loud.
    Or the database can be up while LightRAG quietly runs on its file-based
    defaults - which is silent until the day the JSON store stops scaling, by
    which point there are years of meetings in it.
    """
    from pipeline import index

    checks: list[Check] = []
    try:
        info = index.health()
    except index.IndexError_:
        # check_lightrag already reports the outage; do not fail twice for it.
        checks.append(
            Check("postgres storage", WARN, "cannot verify - LightRAG is unreachable")
        )
    else:
        file_based = _file_based_backends(info)
        if not file_based:
            checks.append(
                Check("postgres storage", OK, "KV, vectors, doc-status and graph on Postgres")
            )

    if not shutil.which("docker"):
        checks.append(Check("postgres", WARN, "docker not on PATH; cannot verify the database"))
        return checks

    user = os.environ.get("POSTGRES_USER", "lightrag")
    database = os.environ.get("POSTGRES_DATABASE", "rag")
    try:
        result = subprocess.run(
            ["docker", "exec", "mmc-postgres", "pg_isready", "-U", user, "-d", database],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        checks.append(Check("postgres", WARN, f"could not query: {type(exc).__name__}"))
        return checks

    if result.returncode != 0:
        checks.append(
            Check(
                "postgres",
                FAIL,
                "database not accepting connections - indexing and queries will fail",
                "docker compose up -d postgres",
            )
        )
    else:
        checks.append(Check("postgres", OK, f"accepting connections as {user}/{database}"))
    return checks


def check_dashboard() -> list[Check]:
    """The local read-only viewer. Optional, but it is how anyone reads the corpus."""
    import httpx

    host = "127.0.0.1" if DASHBOARD_HOST in ("0.0.0.0", "::") else DASHBOARD_HOST
    address = f"http://{host}:{DASHBOARD_PORT}"
    try:
        response = httpx.get(f"{address}/api/overview", timeout=3.0)
        response.raise_for_status()
    except Exception:
        return [
            Check(
                "dashboard",
                WARN,
                f"not serving at {address} - the pipeline still runs without it",
                "pipeline dashboard --open, or scripts/install-dashboard-task.ps1 "
                "to start it at sign-in",
            )
        ]
    return [Check("dashboard", OK, f"serving at {address}")]


def check_nightly_task() -> list[Check]:
    """Is anything scheduled to run the batch?

    Everything else here can be perfect and the corpus still stops growing,
    because `pipeline run` is only ever invoked by hand. That failure is invisible
    precisely because nothing errors.
    """
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["schtasks", "/query", "/tn", NIGHTLY_TASK_NAME],
                capture_output=True, text=True, timeout=20, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return [Check("nightly task", WARN, f"could not query: {type(exc).__name__}")]
        if result.returncode != 0:
            return [
                Check(
                    "nightly task",
                    WARN,
                    f"'{NIGHTLY_TASK_NAME}' is not registered - nothing runs unattended",
                    "scripts/install-nightly-task.ps1",
                )
            ]
        return [Check("nightly task", OK, f"'{NIGHTLY_TASK_NAME}' registered")]

    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return [Check("nightly task", WARN, "cannot inspect the scheduler on this platform")]

    if result.returncode == 0 and "pipeline run" in result.stdout:
        return [Check("nightly task", OK, "a crontab entry runs `pipeline run`")]
    return [
        Check(
            "nightly task",
            WARN,
            "no crontab entry runs `pipeline run` - nothing runs unattended",
            f"crontab -e, then: 0 1 * * * cd {MINUTES_DIR.parent} && uv run pipeline run",
        )
    ]


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


ALL_CHECKS = (
    check_env,
    check_ffmpeg,
    check_asr,
    check_diarization,
    check_providers,
    check_lightrag,
    check_postgres,
    check_ollama,
    check_drive,
    check_dashboard,
    check_nightly_task,
    check_storage,
    check_manifest,
    check_glossary,
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

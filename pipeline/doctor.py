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
from dataclasses import dataclass

from pipeline import db
from pipeline.config import (
    AUDIO_DIR,
    DASHBOARD_HOST,
    DB_PATH,
    DRIVE_CREDENTIALS_FILE,
    DRIVE_SCOPES,
    DRIVE_TOKEN_FILE,
    GLOSSARY_FILE,
    INBOX_DIR,
    LIGHTRAG_API_KEY,
    LIGHTRAG_URL,
    LLM_PROVIDER_ORDER,
    MINUTES_DIR,
    REPLICATE_API_TOKEN,
    REPLICATE_MODEL,
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
    """Required remote Replicate ASR backend."""
    checks = []
    if not REPLICATE_API_TOKEN:
        return [
            Check(
                "asr backend",
                FAIL,
                "REPLICATE_API_TOKEN is unset; no transcription backend is available",
                "add REPLICATE_API_TOKEN=r8_... to .env",
            )
        ]
    try:
        import httpx

        resp = httpx.get(
            "https://api.replicate.com/v1/account",
            headers={"Authorization": f"Bearer {REPLICATE_API_TOKEN}"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            username = resp.json().get("username", "authenticated")
            checks.append(
                Check(
                    "asr backend",
                    OK,
                    f"Replicate ({REPLICATE_MODEL}) — account: @{username}",
                )
            )
        else:
            checks.append(
                Check(
                    "asr backend",
                    FAIL,
                    f"Replicate auth error ({resp.status_code}): {resp.text[:60]}",
                    "check REPLICATE_API_TOKEN at replicate.com/account/api-tokens",
                )
            )
    except Exception as exc:
        checks.append(
            Check(
                "asr backend",
                WARN,
                f"Replicate token set; connectivity check failed ({exc})",
                "verify internet access to api.replicate.com",
            )
        )
    return checks


def check_diarization() -> list[Check]:
    """Diarization is supplied by the configured remote ASR service."""
    if not REPLICATE_API_TOKEN:
        return [Check("diarization", WARN, "unavailable until Replicate is configured")]
    return [Check("diarization", OK, "supplied remotely with transcription")]


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
        # Reachable is not the same as working. LightRAG answers /health while
        # its extraction queue fails every job, and it returns 200 on insert
        # because that only enqueues - so the index stage marked all 43 documents
        # INDEXED, `status` printed "healthy", and the dashboard advertised "43
        # searchable in AI" while the graph held nothing and every answer came
        # from a keyword scan of minutes/. Two cheap reads catch that state.
        checks.extend(_check_graph_populated())
    except index.IndexError_ as exc:
        checks.append(
            Check("lightrag", FAIL, str(exc)[:160], "docker compose up -d")
        )
    return checks


def _check_graph_populated() -> list[Check]:
    """The subscription-authored graph is populated and traversable."""
    from pipeline import graph_sync

    checks: list[Check] = []
    labels = graph_sync.graph_labels()
    if labels:
        checks.append(Check("lightrag graph", OK, f"{len(labels)} entities"))
    else:
        checks.append(
            Check(
                "lightrag graph",
                FAIL,
                "graph is empty - retrieval silently falls back to a keyword scan",
                "pipeline graph-sync",
            )
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


def check_postgres() -> list[Check]:
    """Postgres holds LightRAG's KV, vector, doc-status and graph data.

    Without it, LightRAG silently falls back to file-based storage which does
    not hold thousands of documents. This is the most common undetected failure
    after a configuration error: everything appears to work, but the graph store lives in
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
    """Google Drive OAuth credentials for on-demand audio capture.

    Drive capture is optional; the pipeline also accepts local inbox drops.
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

    if not DRIVE_TOKEN_FILE.exists():
        checks.append(
            Check(
                "drive token", WARN,
                f"missing: {DRIVE_TOKEN_FILE}",
                "run: uv run pipeline auth-drive",
            )
        )
    else:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials

            creds = Credentials.from_authorized_user_file(str(DRIVE_TOKEN_FILE), DRIVE_SCOPES)
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as exc:
                    checks.append(
                        Check(
                            "drive token", FAIL,
                            f"token expired/revoked ({exc})",
                            "run: uv run pipeline auth-drive",
                        )
                    )
                    return checks
            if creds.valid:
                checks.append(Check("drive token", OK, "authorized and valid"))
            else:
                checks.append(
                    Check(
                        "drive token", FAIL,
                        "token expired or invalid",
                        "run: uv run pipeline auth-drive",
                    )
                )
        except Exception as exc:
            checks.append(
                Check(
                    "drive token", FAIL,
                    f"token error: {exc}",
                    "run: uv run pipeline auth-drive",
                )
            )
    return checks


def check_dashboard_auth() -> list[Check]:
    """The dashboard's exposure matches its credentials."""
    from pipeline import dashboard_auth

    host = DASHBOARD_HOST
    if dashboard_auth.token_configured():
        return [Check("dashboard auth", OK, dashboard_auth.describe())]
    if dashboard_auth.is_loopback(host):
        return [
            Check(
                "dashboard auth",
                OK,
                f"no token; bound to {host} only, which is the single-user default",
            )
        ]
    return [
        Check(
            "dashboard auth",
            FAIL,
            f"MMC_DASHBOARD_HOST={host} with no token - the dashboard will refuse to start",
            "set MMC_DASHBOARD_TOKEN in .env, or bind to 127.0.0.1",
        )
    ]


def check_alerting() -> list[Check]:
    """Optional notification for failures in an operator-started run."""
    from pipeline import alert

    command = getattr(alert, "ALERT_COMMAND", "") or ""
    if not command.strip():
        return [
            Check(
                "alerting",
                WARN,
                "MMC_ALERT_COMMAND unset - failures are reported only to the initiating operator",
                'set MMC_ALERT_COMMAND in .env, e.g. curl -s -d @- https://ntfy.sh/your-topic',
            )
        ]
    # A malformed command should surface here, before it can hide a failure.
    try:
        parts = alert.split_command(command)
    except Exception as exc:
        return [
            Check(
                "alerting",
                FAIL,
                f"MMC_ALERT_COMMAND does not parse ({type(exc).__name__}: {exc})",
                "check quoting in .env",
            )
        ]
    if not parts:
        return [Check("alerting", FAIL, "MMC_ALERT_COMMAND parses to nothing", "check .env")]
    return [Check("alerting", OK, f"via {parts[0]}")]


ALL_CHECKS = (
    check_ffmpeg,
    check_asr,
    check_diarization,
    check_providers,
    check_dashboard_auth,
    check_alerting,
    check_postgres,
    check_lightrag,
    check_drive,
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

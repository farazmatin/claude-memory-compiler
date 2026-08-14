#!/usr/bin/env bash
#
# One-command setup. Safe to re-run: it never overwrites an existing .env and
# skips anything already done.
#
#   ./setup.sh
#
# Two things need a browser and your accounts, so they cannot be automated:
#   1. A HuggingFace read token, plus accepting two model licences
#   2. Google Drive consent, if you want unattended capture
# This prompts for the first (input hidden, never echoed) and points at the
# second. The Windows equivalent is scripts\setup.ps1, which also installs the
# prerequisites and registers the nightly Scheduled Task.

set -euo pipefail

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
YELLOW=$'\033[33m'; RESET=$'\033[0m'

step()  { printf '\n%s==> %s%s\n' "$BOLD" "$1" "$RESET"; }
ok()    { printf '    %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
warn()  { printf '    %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
fail()  { printf '    %s✗%s %s\n' "$RED" "$RESET" "$1"; }
have()  { command -v "$1" >/dev/null 2>&1; }

cd "$(dirname "$0")"
MISSING=0

# ── 1. Prerequisites ──────────────────────────────────────────────────

step "Checking prerequisites"

if have ffmpeg; then
    ok "ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f3)"
else
    fail "ffmpeg not found - transcription cannot run without it"
    if have apt-get;  then warn "install: sudo apt-get install ffmpeg"
    elif have brew;   then warn "install: brew install ffmpeg"
    elif have dnf;    then warn "install: sudo dnf install ffmpeg"
    else                   warn "install ffmpeg for your platform"
    fi
    MISSING=1
fi

if have docker; then
    if docker info >/dev/null 2>&1; then
        ok "docker running"
    else
        fail "docker installed but not running (or needs sudo)"
        MISSING=1
    fi
else
    fail "docker not found - needed for the search index"
    warn "install: https://docs.docker.com/engine/install/"
    MISSING=1
fi

if have uv; then
    ok "uv $(uv --version 2>/dev/null | cut -d' ' -f2)"
else
    warn "uv not found - installing it"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # shellcheck disable=SC1090
    [ -f "$HOME/.local/bin/env" ] && . "$HOME/.local/bin/env"
    export PATH="$HOME/.local/bin:$PATH"
    have uv && ok "uv installed" || { fail "uv install failed"; MISSING=1; }
fi

# At least one of these must exist or minutes cannot be written.
PROVIDERS=""
for cli in gemini codex claude; do
    have "$cli" && PROVIDERS="$PROVIDERS $cli"
done
if [ -n "$PROVIDERS" ]; then
    ok "LLM providers found:$PROVIDERS"
else
    warn "none of gemini/codex/claude on PATH"
    warn "minutes cannot be written until at least one is installed and logged in"
fi

if [ "$MISSING" -eq 1 ]; then
    printf '\n%sInstall the missing prerequisites above, then re-run ./setup.sh%s\n\n' "$RED" "$RESET"
    exit 1
fi

# ── 2. Python dependencies ────────────────────────────────────────────

# Before the .env step, which is driven by `pipeline config` so that one
# implementation writes the file on every platform. A second one here would drift
# from pipeline/env.py, and the drift would surface as setup insisting that a
# configured value is missing.

step "Installing Python dependencies"
warn "the ASR extra pulls torch - this can take several minutes"
uv sync --extra asr --extra dev
ok "dependencies installed"

# ── 3. Secrets ────────────────────────────────────────────────────────

step "Configuring .env"

# Creates .env from the example and generates MMC_LIGHTRAG_API_KEY and
# POSTGRES_PASSWORD. Only fills blanks: re-running never rotates a password that
# the running database still expects.
uv run pipeline config init

# Owner name makes 1:1 speaker identification much better.
if ! uv run pipeline config show --key MMC_OWNER_NAME >/dev/null 2>&1; then
    printf '\n    Your name (used to tell who is who in 1:1 recordings): '
    read -r OWNER || OWNER=""
    if [ -n "$OWNER" ]; then
        printf '%s' "$OWNER" | uv run pipeline config set MMC_OWNER_NAME >/dev/null
        ok "MMC_OWNER_NAME configured"
    else
        warn "skipped - set MMC_OWNER_NAME in .env later"
    fi
fi

# The one secret nobody can generate for you. Read with echo off and piped
# straight in: a token passed as an argument lands in shell history and in `ps`.
if ! uv run pipeline config show --key HF_TOKEN >/dev/null 2>&1; then
    printf '\n    A HuggingFace read token is needed for speaker diarization.\n'
    printf '    Without it, action items come out with nobody assigned.\n\n'
    printf '      1. Create a %sread%s token:  https://huggingface.co/settings/tokens\n' "$BOLD" "$RESET"
    printf '      2. Accept BOTH licences (the token alone is not enough):\n'
    printf '           https://hf.co/pyannote/speaker-diarization-3.1\n'
    printf '           https://hf.co/pyannote/segmentation-3.0\n\n'
    printf '    Paste the token (input hidden), or press Enter to skip: '
    read -rs HF || HF=""
    printf '\n'
    if [ -n "$HF" ]; then
        printf '%s' "$HF" | uv run pipeline config set HF_TOKEN >/dev/null
        ok "HF_TOKEN configured"
    else
        warn "skipped - re-run ./setup.sh when you have one"
    fi
    unset HF
fi

# ── 4. Services ───────────────────────────────────────────────────────

step "Starting services (LightRAG, Postgres, Ollama)"
docker compose up -d
ok "containers started"

printf '    waiting for Ollama'
for _ in $(seq 1 30); do
    if docker compose exec -T ollama ollama list >/dev/null 2>&1; then
        printf '\n'; ok "Ollama ready"; break
    fi
    printf '.'; sleep 2
done

step "Pulling local models (a few GB, one time)"
for model in qwen3:4b mxbai-embed-large; do
    if docker compose exec -T ollama ollama list 2>/dev/null | grep -q "${model%%:*}"; then
        ok "$model already present"
    else
        printf '    pulling %s...\n' "$model"
        docker compose exec -T ollama ollama pull "$model" && ok "$model pulled"
    fi
done

# ── 5. Initialise ─────────────────────────────────────────────────────

step "Creating folders and database"
uv run pipeline init
ok "ready"

# ── 6. Verify ─────────────────────────────────────────────────────────

step "Running preflight checks"
set +e
uv run pipeline doctor
DOCTOR=$?
set -e

# ── Done ──────────────────────────────────────────────────────────────

printf '\n%s────────────────────────────────────────────────────────%s\n' "$BOLD" "$RESET"

if [ "$DOCTOR" -eq 0 ]; then
    printf '%sSetup complete.%s\n\n' "$GREEN$BOLD" "$RESET"
else
    printf '%sSetup done, but preflight found problems.%s\n\n' "$YELLOW$BOLD" "$RESET"
    printf 'Each failure above prints its own fix. The two that need a browser:\n\n'
    printf '  %sdiarization%s - a read token, AND both licences accepted:\n' "$BOLD" "$RESET"
    printf '     %shttps://huggingface.co/settings/tokens%s\n' "$DIM" "$RESET"
    printf '     %shttps://hf.co/pyannote/speaker-diarization-3.1%s\n' "$DIM" "$RESET"
    printf '     %shttps://hf.co/pyannote/segmentation-3.0%s\n' "$DIM" "$RESET"
    printf '     Without it, transcripts have no speaker names, which means action\n'
    printf '     items with nobody assigned. Re-run this script to enter the token.\n\n'
    printf '  %sdrive auth%s - one-time consent for unattended capture:\n' "$BOLD" "$RESET"
    printf '     %suv run pipeline auth-drive%s\n\n' "$BOLD" "$RESET"
    printf 'Then re-check with: %suv run pipeline doctor%s\n\n' "$BOLD" "$RESET"
fi

printf '%sYour first meeting:%s\n\n' "$BOLD" "$RESET"
printf '  cp ~/some-recording.m4a inbox/\n'
printf '  uv run pipeline run\n\n'
printf 'Expect ~30-50 min for a one-hour recording on CPU. Then:\n\n'
printf '  less minutes/*.md                       # read what it wrote\n'
printf '  uv run pipeline query "what did we decide?"\n\n'
printf 'Full guide: %sdocs/USER_GUIDE.md%s\n\n' "$BOLD" "$RESET"

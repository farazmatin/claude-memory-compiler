#!/usr/bin/env bash
#
# One-command setup. Safe to re-run: it never overwrites an existing .env and
# skips anything already done.
#
#   ./setup.sh
#
# Afterwards you still need to do two things by hand, because nobody can do them
# for you:
#   1. Put a HuggingFace token in .env
#   2. Accept two model licences on huggingface.co
# The script tells you exactly where at the end.

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

# ── 2. Secrets ────────────────────────────────────────────────────────

step "Configuring .env"

gen_secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }

if [ -f .env ]; then
    ok ".env already exists - leaving it alone"
else
    cp .env.example .env
    # Fill in the secrets that can be generated.
    python3 - <<'PY'
import pathlib, secrets
env = pathlib.Path(".env")
text = env.read_text()
for key in ("MMC_LIGHTRAG_API_KEY", "POSTGRES_PASSWORD"):
    text = text.replace(f"{key}=\n", f"{key}={secrets.token_urlsafe(32)}\n", 1)
env.write_text(text)
PY
    ok "created .env with generated secrets"
fi

# Owner name makes 1:1 speaker identification much better.
if ! grep -q '^MMC_OWNER_NAME=' .env 2>/dev/null; then
    printf '\n    Your name (used to tell who is who in 1:1 recordings): '
    read -r OWNER || OWNER=""
    if [ -n "$OWNER" ]; then
        printf '\nMMC_OWNER_NAME=%s\n' "$OWNER" >> .env
        ok "set MMC_OWNER_NAME=$OWNER"
    else
        warn "skipped - set MMC_OWNER_NAME in .env later"
    fi
fi

# ── 3. Python dependencies ────────────────────────────────────────────

step "Installing Python dependencies"
uv sync --extra dev
ok "dependencies installed"

# ── 4. Services ───────────────────────────────────────────────────────

step "Starting services (LightRAG graph storage and Postgres)"
docker compose up -d
ok "containers started"

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
    printf 'Configure REPLICATE_API_TOKEN in .env for remote transcription.\n'
    printf 'Then re-run: %suv run pipeline doctor%s\n\n' "$BOLD" "$RESET"
fi

printf '%sYour first meeting:%s\n\n' "$BOLD" "$RESET"
printf '  cp ~/some-recording.m4a inbox/\n'
printf '  uv run pipeline run\n\n'
printf 'Remote transcription time depends on the configured Replicate model. Then:\n\n'
printf '  less minutes/*.md                       # read what it wrote\n'
printf '  uv run pipeline query "what did we decide?"\n\n'
printf 'Full guide: %sdocs/USER_GUIDE.md%s\n\n' "$BOLD" "$RESET"

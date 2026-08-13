# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A self-evolving personal knowledge base that captures Claude Code conversations, extracts knowledge via the Claude Agent SDK, and compiles it into a searchable markdown wiki. Follows Andrej Karpathy's "LLM Knowledge Base" compiler analogy: `daily/` logs are source code, the LLM is the compiler, `knowledge/` articles are the executable.

## Commands

```bash
# Install/sync dependencies
uv sync

# Compile daily logs → knowledge articles (incremental)
uv run python scripts/compile.py

# Force recompile all logs
uv run python scripts/compile.py --all

# Compile a specific log
uv run python scripts/compile.py --file daily/2026-04-08.md

# Preview what would compile
uv run python scripts/compile.py --dry-run

# Query the knowledge base
uv run python scripts/query.py "question here"

# Query and save answer back to KB
uv run python scripts/query.py "question here" --file-back

# Run all 7 health checks (includes LLM contradiction check)
uv run python scripts/lint.py

# Structural checks only (free, no LLM)
uv run python scripts/lint.py --structural-only
```

Linting uses ruff with 100-char line length (configured in pyproject.toml).

## Architecture

### Data Flow

```
Claude Code session
  → SessionEnd/PreCompact hook fires
  → hooks extract transcript context (local I/O only, no API calls)
  → spawn flush.py as detached background process
  → flush.py calls Agent SDK (allowed_tools=[]) to decide what's worth saving
  → appends to daily/YYYY-MM-DD.md
  → if past 6 PM local time, spawns compile.py
  → compile.py calls Agent SDK (with Read/Write/Edit/Glob/Grep tools)
  → writes knowledge/concepts/*.md, connections/*.md, updates index.md + log.md
  → next SessionStart hook injects index.md into new session context
```

### Three Layers

- **`hooks/`** — Claude Code lifecycle hooks (SessionStart, PreCompact, SessionEnd). Pure local I/O, fast (<1s). Spawn background processes but never call LLM directly.
- **`scripts/`** — CLI tools and background agents. These call the Claude Agent SDK. `flush.py` is spawned by hooks; `compile.py`, `query.py`, `lint.py` are run manually or triggered by flush.
- **`knowledge/`** — LLM-owned output. `index.md` is the master catalog and primary retrieval mechanism (no RAG/embeddings). Articles use YAML frontmatter + Obsidian-style `[[wikilinks]]`.

### Key Patterns

**Recursion prevention:** Hooks check `os.environ.get("CLAUDE_INVOKED_BY")` and exit early if set. `flush.py` sets `os.environ["CLAUDE_INVOKED_BY"] = "memory_flush"` at the top of the file *before any other imports* to prevent Agent SDK from re-triggering hooks.

**Import convention:** Scripts in `scripts/` use sibling imports (`from config import ...`, `from utils import ...`). They are invoked via `uv run python scripts/<name>.py` from project root — the scripts directory is the import context, not the project root.

**Agent SDK usage:** All LLM calls import `claude_agent_sdk` inside async functions (lazy import). Pattern:
```python
async def run():
    from claude_agent_sdk import query, ClaudeAgentOptions, ...
    async for message in query(prompt=..., options=ClaudeAgentOptions(...)):
        ...
```

**Background process spawning:** Hooks use `subprocess.Popen()` with `start_new_session=True` (Linux/Mac) or `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS` (Windows). Stdout/stderr go to DEVNULL; observability is via `scripts/flush.log`.

**Incremental compilation:** `scripts/state.json` tracks SHA-256 hashes of daily logs. compile.py skips unchanged files unless `--all` is passed.

**Deduplication:** `scripts/last-flush.json` tracks session_id + timestamp. flush.py skips if same session was flushed within 60 seconds.

## Configuration

- **Timezone:** `scripts/config.py` line 23 — `TIMEZONE = "America/Toronto"`. Used for daily log dates and the 6 PM auto-compile trigger.
- **Auto-compile hour:** `scripts/flush.py` line 142 — `COMPILE_AFTER_HOUR = 18`.
- **Context limits:** session-start.py caps injected context at 20,000 chars. session-end.py extracts last 30 turns (max 15KB).
- **Hooks:** `.claude/settings.json` — empty matcher catches all events.

## Generated Directories (gitignored)

`daily/`, `knowledge/`, `reports/` — created at runtime. `scripts/state.json`, `scripts/last-flush.json`, `scripts/flush.log` — runtime state files also gitignored.

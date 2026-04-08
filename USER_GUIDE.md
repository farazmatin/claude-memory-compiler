# User Guide: Claude Memory Compiler

## What Is This?

A **self-evolving memory system** for Claude Code. Every conversation you have with Claude Code is automatically captured, summarized, and compiled into a personal knowledge base — a searchable wiki of your decisions, lessons learned, patterns, and insights. The next time you start a session, Claude already "remembers" what you've learned.

Inspired by [Andrej Karpathy's LLM Knowledge Base](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) architecture.

---

## Architecture Overview

The system uses a **compiler analogy** — your conversations are "source code" that gets compiled into "executable" knowledge:

```
┌─────────────────────────────────────────────────────────────────────┐
│                        THE MEMORY LOOP                              │
│                                                                     │
│  ┌──────────┐    hooks fire     ┌───────────┐    LLM extracts      │
│  │  Claude   │ ───────────────► │ flush.py  │ ──────────────►      │
│  │  Code     │  (session end /  │ (background│   key insights       │
│  │  Session  │   pre-compact)   │  process)  │                      │
│  └──────────┘                   └───────────┘                      │
│       ▲                                │                            │
│       │                                ▼                            │
│  ┌──────────┐                   ┌───────────┐                      │
│  │ index.md │   session start   │  daily/   │  raw conversation    │
│  │ injected │ ◄──── hook ────── │ YYYY-MM-  │  logs (append-only)  │
│  │ into     │                   │  DD.md    │                      │
│  │ context  │                   └───────────┘                      │
│  └──────────┘                        │                              │
│       ▲                              │ compile.py                   │
│       │                              ▼ (auto after 6PM or manual)   │
│  ┌──────────────────────────────────────────┐                      │
│  │              knowledge/                   │                      │
│  │  index.md  ← master catalog (retrieval)   │                      │
│  │  log.md    ← build audit trail            │                      │
│  │  concepts/ ← atomic knowledge articles    │  ◄── Obsidian       │
│  │  connections/ ← cross-cutting insights    │      reads this     │
│  │  qa/       ← saved query answers          │      as a vault     │
│  └──────────────────────────────────────────┘                      │
└─────────────────────────────────────────────────────────────────────┘
```

### The Compiler Analogy

| Analogy | Component | What It Does |
|---------|-----------|--------------|
| **Source Code** | `daily/` | Raw conversation logs — append-only, never edited |
| **Compiler** | `compile.py` | LLM reads daily logs, extracts and organizes knowledge |
| **Executable** | `knowledge/` | Structured, cross-referenced markdown wiki |
| **Test Suite** | `lint.py` | 7 health checks for consistency and completeness |
| **Runtime** | `query.py` | Ask questions against the compiled knowledge base |

---

## Where Obsidian Fits In

**Obsidian is the visualization layer** — it is optional but highly recommended.

The knowledge base is pure markdown with `[[wikilinks]]` — the same format Obsidian uses natively. You don't need Obsidian for the system to work, but it gives you:

- **Graph View** — see how concepts connect visually (which articles link to which)
- **Backlinks** — click any article and see everything that references it
- **Search** — full-text search across your entire knowledge base
- **Reading experience** — rendered markdown with clickable wikilinks

### How to Connect Obsidian

1. Open Obsidian
2. Click "Open folder as vault"
3. Select the `knowledge/` folder inside this project
4. That's it — all your compiled articles, index, and connections appear immediately

You can also open the entire project root as a vault if you want to see daily logs too.

**Obsidian does NOT run anything.** It is purely a viewer/editor. All the automation (capturing, flushing, compiling) happens through Claude Code hooks and Python scripts.

---

## Dependencies

### Required

| Dependency | Version | Purpose |
|------------|---------|---------|
| **Claude Code CLI** | Latest (with hooks support) | The agent interface — where you have conversations |
| **Python** | 3.12+ | Runs all scripts (hooks, flush, compile, query, lint) |
| **uv** | Latest | Python package manager (installs dependencies, runs scripts) |
| **claude-agent-sdk** | >=0.1.29 | Background LLM calls for flush and compile (installed by `uv sync`) |
| **python-dotenv** | >=1.0.0 | Environment variable management (installed by `uv sync`) |
| **tzdata** | >=2024.1 | Timezone data (installed by `uv sync`) |

### Optional

| Dependency | Purpose |
|------------|---------|
| **Obsidian** | Visualize the knowledge graph, browse articles with backlinks and search |

### What You Do NOT Need

- **No API key** — uses Claude Code's built-in credentials (`~/.claude/.credentials.json`). Covered by your Claude subscription (Max, Team, or Enterprise).
- **No vector database** — no embeddings, no RAG. The LLM reads a structured `index.md` directly. At personal scale (50-500 articles), this outperforms cosine similarity.
- **No external services** — everything runs locally on your machine.

---

## Assumptions

1. **You use Claude Code regularly.** The system captures knowledge from Claude Code conversations. No conversations = no knowledge base.
2. **Claude Code supports hooks.** Your Claude Code version must support `SessionStart`, `SessionEnd`, and `PreCompact` hooks via `.claude/settings.json`.
3. **This repo is your working directory (or hooks are merged).** The hooks in `.claude/settings.json` fire when you open Claude Code in this project's directory. If you work in a different project, you need to copy/merge the hook configuration into that project's `.claude/settings.json`.
4. **Python 3.12+ and uv are installed.** All scripts are run via `uv run python ...`.
5. **You have a Claude subscription.** The Claude Agent SDK uses your existing Claude subscription — no separate API billing.
6. **Timezone is configured.** Currently set to `America/Toronto` (EST/EDT) in `scripts/config.py`. The 6 PM auto-compile trigger and all timestamps use this timezone.
7. **Background processes can run.** `flush.py` and `compile.py` spawn as detached background processes. They need to survive after the Claude Code hook process exits.

---

## Setup (Already Done)

For reference, here's what was set up:

```bash
# 1. Dependencies installed
uv sync

# 2. Timezone configured (scripts/config.py)
TIMEZONE = "America/Toronto"

# 3. Directories created
daily/                    # will store conversation logs
knowledge/                # will store compiled articles
knowledge/concepts/       # atomic knowledge
knowledge/connections/    # cross-cutting insights
knowledge/qa/             # saved query answers
reports/                  # lint reports

# 4. Hooks configured (.claude/settings.json)
SessionStart  → hooks/session-start.py   # injects KB into context
PreCompact    → hooks/pre-compact.py     # captures before compaction
SessionEnd    → hooks/session-end.py     # captures at session end
```

---

## Daily Usage

### Automatic (just use Claude Code normally)

You don't need to do anything special. The hooks handle everything:

| Event | What Happens Automatically |
|-------|---------------------------|
| **Start a session** | `session-start.py` injects your knowledge base index so Claude "remembers" past context |
| **Context compacts** | `pre-compact.py` captures the conversation before summarization discards detail |
| **End a session** | `session-end.py` extracts key insights → spawns `flush.py` → writes to `daily/YYYY-MM-DD.md` |
| **After 6 PM ET** | `flush.py` auto-triggers `compile.py` → daily logs get compiled into knowledge articles |

### Manual Commands

```bash
# Compile daily logs into knowledge articles (new/changed only)
uv run python scripts/compile.py

# Force recompile everything
uv run python scripts/compile.py --all

# Compile a specific daily log
uv run python scripts/compile.py --file daily/2026-04-08.md

# Preview what would be compiled
uv run python scripts/compile.py --dry-run

# Ask your knowledge base a question
uv run python scripts/query.py "What auth patterns do I use?"

# Ask and save the answer back into the KB (compounding loop)
uv run python scripts/query.py "What's my error handling strategy?" --file-back

# Run all 7 health checks
uv run python scripts/lint.py

# Run only free structural checks (no LLM cost)
uv run python scripts/lint.py --structural-only
```

---

## Cost Estimates

All costs are covered by your Claude subscription (no separate API billing).

| Operation | Estimated Cost |
|-----------|---------------|
| Memory flush (per session end) | ~$0.02-0.05 |
| Compile one daily log | ~$0.45-0.65 |
| Query (no file-back) | ~$0.15-0.25 |
| Query (with file-back) | ~$0.25-0.40 |
| Full lint (with contradiction check) | ~$0.15-0.25 |
| Structural lint only | $0.00 |

---

## File Structure

```
claude-memory-compiler/
├── .claude/
│   └── settings.json           # Hook configuration (auto-activates)
├── AGENTS.md                   # Technical schema — the "compiler specification"
├── README.md                   # Quick start
├── USER_GUIDE.md               # This file
├── pyproject.toml              # Python dependencies
├── uv.lock                     # Locked dependency versions
│
├── hooks/                      # Claude Code lifecycle hooks
│   ├── session-start.py        #   Injects KB context on session start
│   ├── session-end.py          #   Captures conversation on session end
│   └── pre-compact.py          #   Captures before context compaction
│
├── scripts/                    # CLI tools
│   ├── config.py               #   Paths and timezone configuration
│   ├── utils.py                #   Shared utilities
│   ├── flush.py                #   Background memory extraction
│   ├── compile.py              #   Daily logs → knowledge articles
│   ├── query.py                #   Ask questions against the KB
│   └── lint.py                 #   7 health checks
│
├── daily/                      # [Generated] Raw conversation logs
│   └── YYYY-MM-DD.md           #   One file per day, append-only
│
├── knowledge/                  # [Generated] Compiled knowledge base
│   ├── index.md                #   Master catalog (the retrieval mechanism)
│   ├── log.md                  #   Build log (audit trail)
│   ├── concepts/               #   Atomic knowledge articles
│   ├── connections/            #   Cross-cutting insights
│   └── qa/                     #   Saved query answers
│
└── reports/                    # [Generated] Lint reports
    └── lint-YYYY-MM-DD.md
```

---

## Troubleshooting

### "Knowledge Base Index is empty"
**Expected** when you first set up. The knowledge base builds over time as you use Claude Code. After a few sessions and a compile cycle, articles will start appearing.

### Hooks aren't firing
- Make sure you're running Claude Code from the project directory (or a directory that has `.claude/settings.json` with the hooks)
- Check that `uv` is installed and accessible from your PATH

### flush.py isn't running
- Check `scripts/flush.log` for error messages
- The flush process runs in the background — it won't show output in your terminal

### Compilation didn't happen automatically
- Auto-compilation only triggers after 6 PM local time (configured in `scripts/flush.py` as `COMPILE_AFTER_HOUR = 18`)
- You can always run `uv run python scripts/compile.py` manually

### Want to change the timezone?
- Edit `scripts/config.py` line 23: change `TIMEZONE = "America/Toronto"` to your timezone
- Use any valid IANA timezone name (e.g., `America/New_York`, `Europe/London`, `Asia/Tokyo`)

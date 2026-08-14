# Meeting Memory Desktop — Design

Status: approved approach, spec pending user review
Date: 2026-08-14

## What we are building

An installable Windows application that opens the private meeting archive in its
own window: log in, see pipeline status, read compiled minutes, and ask questions
answered from the archive with the source meetings named.

## Decisions already made

| Decision | Choice | Why |
|---|---|---|
| Shell | pywebview + PyInstaller | The project is Python end to end with no build step. Tauri adds a Rust toolchain and Electron adds a Node runtime, both for what is fundamentally a document reader. |
| Users | Single user now, schema ready for more | Others come later. Building the tables for multiple users costs nothing; building the UI for them now costs a lot. |
| Permissions | Everyone sees everything | Accounts establish identity, not restriction. No per-meeting ACL. |
| First milestone | Search and ask, with sources | Status and content are already served by existing endpoints. This is the part that does not exist yet. |

## Architecture

```
meeting-memory.exe  (PyInstaller, one file)
  └─ pywebview window  (WebView2, present on Win11 by default)
       └─ http://127.0.0.1:<ephemeral port>   loopback only
            └─ DashboardHandler + session gate
                 ├─ db/manifest.db   meetings, speakers, drive_sources   read-only
                 ├─ db/auth.db       users, sessions                     read-write
                 └─ httpx → LightRAG :9621  (X-API-Key)
```

The desktop app is a **reader and asker**. Transcription stays in the CLI. The
packaged executable therefore excludes torch, faster-whisper and pyannote, which
is what keeps it near 40-60 MB instead of several gigabytes.

## Components

**`pipeline/auth.py`** — new. `users` and `sessions` tables, password hashing,
session issue and verify. No new dependencies: `hashlib.scrypt` and `secrets` are
standard library.

**`pipeline/dashboard.py`** — extend. Session gate on every route, Host header
validation, `/api/login` and `/api/logout`, serve the login page when there is no
valid session.

**`pipeline/index.py`** — extend. Retain the document references LightRAG returns
so answers can cite sources. Raise instead of sending unauthenticated requests
when `LIGHTRAG_API_KEY` is empty.

**`pipeline/answer.py`** — extend. Return `sources: list[MeetingRef]` alongside
the answer text. Today `ask()` keeps only `len(context)` and discards the records.

**`pipeline/desktop.py`** — new. Entry point: bind an ephemeral port, start the
server on a background thread, open the pywebview window against it.

**`pipeline/static/login.html`**, **`app.js`** — login screen, and the ask panel
extended to render source meetings as links into the reader.

**`build/meeting-memory.spec`** — PyInstaller spec, read-path dependencies only.

## Authentication

- **First launch** shows "create your account" rather than a login form. No
  default password ever exists.
- **Hashing**: `hashlib.scrypt`, per-user random 16-byte salt, n=2^14, r=8, p=1.
- **Sessions**: 32-byte urlsafe token, HttpOnly and SameSite=Strict cookie,
  12-hour expiry, stored in `auth.db` so a restart does not silently extend them.
- **Every route** except `/api/login` and the login assets requires a valid
  session.
- **Host header** must be `127.0.0.1:<port>` or `localhost:<port>`, otherwise 421.

That last rule closes finding 3 of the 2026-08-13 security review: without it, a
website you visit can rebind DNS to loopback and read the whole archive. Putting
it in `dashboard.py` rather than the desktop shell means the CLI dashboard is
protected too.

## Sourced answers — the substantive work

`index.query_context()` returns an opaque string, and `answer.py` throws it away
after synthesis. Prose citations exist only because the synthesis prompt asks for
them (`answer.py:62`), so there is nothing structured to render.

Minutes are inserted with a `file_source` (`index.insert_text(text, file_source)`),
and `meetings.minutes_path` maps a minutes filename back to a meeting. The plan is
to parse the returned context for those markers and resolve them to
`{meeting_id, title, date, drive_url}`.

**Open risk:** the exact shape of LightRAG's context response has not been
verified. The implementation plan must begin with a short spike against the live
endpoint before the parsing code is written. If references cannot be recovered
reliably, sources degrade to empty and the existing prose citations still work —
the feature is not blocked, only reduced.

## Testing

- **auth**: hash and verify roundtrip, wrong password rejected, expired session
  rejected, first-run account creation path.
- **dashboard**: no session → 401, bad Host header → 421, valid session → 200.
- **sources**: given a known context blob, the correct meeting refs come back.
- The existing 57 tests must continue to pass.

## Explicitly out of scope

Per-meeting permissions. Password reset and email. Auto-update. Cross-platform
builds. Any multi-user interface — the schema supports more users, but no screen
manages them until that is actually needed.

## Prerequisites carried over

These are not part of this build but block a clean run:

1. LightRAG and Ollama are published on `0.0.0.0` with no API key. Restarting
   from the committed `docker-compose.yml` fixes both.
2. `docker-compose.yml` mounts `./rag_storage → /app/rag_storage` and
   `./minutes → /app/inputs`, but the image uses `WORKING_DIR=/app/data/rag_storage`
   and `INPUT_DIR=/app/data/inputs`. Both mounts are dead.
3. `My recording 10.mp4` (28.3 MB) is in Drive, not yet ingested.

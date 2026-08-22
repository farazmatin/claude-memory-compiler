"""Harness for end-to-end tests.

The unit suite tests functions. This exercises the app: the real CLI, the real
stage machine, the real SQLite manifest, real ffmpeg, real file I/O, and real HTTP
against a LightRAG-shaped server.

Only three things are faked, and only because they are the parts that need a GPU,
a subscription, or a docker stack:

  ASR         a Backend implementation returning a fixed transcript
  LLM         a real executable, driven through the real subprocess/stdin path
  LightRAG    a real HTTP server speaking the endpoints index.py calls

Faking the *model* while keeping the *plumbing* real is the point. The LLM fake in
particular is an actual script on PATH, so the CLIProvider subprocess handling -
stdin piping, exit codes, timeouts - is genuinely under test. That path had never
been run before these tests existed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# ── Audio ─────────────────────────────────────────────────────────────

def make_audio(path: Path, seconds: float = 2.0, freq: int = 440) -> Path:
    """Generate a real audio file with ffmpeg.

    Real bytes matter: ingest hashes the file and ffprobe reads its duration, so a
    stub would bypass exactly the code under test.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-f", "lavfi",
            "-i", f"sine=frequency={freq}:duration={seconds}",
            "-c:a", "aac", str(path),
        ],
        check=True, capture_output=True,
    )
    return path


# ── Fake LightRAG ─────────────────────────────────────────────────────

class FakeLightRAG:
    """An HTTP server speaking the endpoints `pipeline/index.py` calls.

    Real HTTP rather than a mocked client, so httpx usage, headers, status
    handling and the delete-endpoint fallback are all exercised.
    """

    def __init__(self, kv_storage: str = "PGKVStorage"):
        self.documents: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.queries: list[dict] = []
        self.kv_storage = kv_storage
        # Set to a status code to make the next insert fail.
        self.fail_insert_with: int | None = None
        # When True, both delete routes 404 - simulating a version whose delete
        # endpoint we cannot reach.
        self.refuse_delete = False
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_port}"

    def start(self) -> FakeLightRAG:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence per-request logging
                pass

            def _send(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_json(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                try:
                    return json.loads(self.rfile.read(length))
                except json.JSONDecodeError:
                    return {}

            def do_GET(self):
                if self.path == "/health":
                    self._send(200, {
                        "status": "healthy",
                        "configuration": {"kv_storage": outer.kv_storage},
                    })
                else:
                    self._send(404, {"detail": "not found"})

            def do_POST(self):
                payload = self._read_json()
                if self.path == "/documents/text":
                    if outer.fail_insert_with:
                        code, outer.fail_insert_with = outer.fail_insert_with, None
                        self._send(code, {"detail": "insert rejected"})
                        return
                    import hashlib

                    text = payload.get("text", "")
                    doc_id = "doc-" + hashlib.md5(text.strip().encode()).hexdigest()
                    outer.documents[doc_id] = {
                        "text": text,
                        "file_source": payload.get("file_source"),
                    }
                    self._send(200, {"status": "success", "id": doc_id})
                elif self.path == "/query":
                    outer.queries.append(payload)
                    if payload.get("only_need_context"):
                        # Return the stored corpus as "retrieved context".
                        joined = "\n\n".join(d["text"] for d in outer.documents.values())
                        self._send(200, {"response": joined[:4000]})
                    else:
                        self._send(200, {"response": "local-model answer"})
                else:
                    self._send(404, {"detail": "not found"})

            def do_DELETE(self):
                if outer.refuse_delete:
                    self._send(404, {"detail": "no such route"})
                    return
                payload = self._read_json()
                if self.path == "/documents/delete_document":
                    for doc_id in payload.get("doc_ids", []):
                        outer.documents.pop(doc_id, None)
                        outer.deleted.append(doc_id)
                    self._send(200, {"status": "success"})
                elif self.path.startswith("/documents/"):
                    doc_id = self.path.rsplit("/", 1)[-1]
                    outer.documents.pop(doc_id, None)
                    outer.deleted.append(doc_id)
                    self._send(200, {"status": "success"})
                else:
                    self._send(404, {"detail": "not found"})

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def __enter__(self) -> FakeLightRAG:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


# ── Fake LLM provider ─────────────────────────────────────────────────

MINUTES_DOC = """---
date: 2026-08-10
time: "11:00"
title: Atlas Roadmap Review
type: stakeholder
attendees: [Faraz, Ali]
entities: [Atlas, Northwind]
template_version: "__TEMPLATE_VERSION__"
source_audio: audio/x.m4a
source_transcript: transcripts/x.json
---

# Atlas Roadmap Review

## Context
Reviewing the Atlas rewrite ahead of the 2026.4 release.

## Decisions
- **Deferred Atlas to Q1** - decided by Faraz. Rationale: the rate-limiter is not
  ready and Northwind's SSO request is higher value this quarter. [0:00:01]

## Action Items
- [ ] **Ali** - scope the SSO work. Due: 2026-08-17. [0:00:02]

## Risks, Blockers & Dependencies
- **Rate-limiter** - Atlas depends on it and it is unstaffed.

## Entities
- Atlas (feature): the platform rewrite
- Faraz (person): product manager
- Ali (person): engineering lead
- Northwind (customer): asked for SSO
- 2026.4 (release)

## Relations
- Faraz -> deprioritized -> Atlas
- Northwind -> requested -> SSO
- Atlas -> part of -> 2026.4
"""

SPEAKER_JSON = '{"SPEAKER_00": "Faraz", "SPEAKER_01": "Ali"}'

FAKE_LLM = r'''#!/usr/bin/env python3
"""Stands in for gemini/codex/claude. Reads the prompt on stdin like the real CLIs."""
import sys, os, pathlib

prompt = sys.stdin.read()
log = os.environ.get("FAKE_LLM_LOG")
if log:
    pathlib.Path(log).open("a").write(f"=== CALL ({len(prompt)} chars) ===\n")

mode_file = os.environ.get("FAKE_LLM_FAIL")
if mode_file and pathlib.Path(mode_file).exists():
    sys.stderr.write("simulated provider failure\n")
    sys.exit(3)

if "identifying speakers" in prompt:
    sys.stdout.write(os.environ["FAKE_LLM_SPEAKERS"])
elif "extracting notes from part" in prompt:
    sys.stdout.write("- Deferred Atlas to Q1 because the rate-limiter is not ready [0:00:01]")
else:
    sys.stdout.write(pathlib.Path(os.environ["FAKE_LLM_MINUTES"]).read_text())
'''


def install_fake_llm(tmp_path: Path, minutes: str = MINUTES_DOC) -> dict[str, str]:
    """Write the fake provider to disk and return the env that selects it.

    A real executable, invoked through the real CLIProvider - so subprocess
    handling, stdin piping and exit-code mapping are under test, not stubbed.
    """
    script = tmp_path / "fake_llm.py"
    script.write_text(FAKE_LLM, encoding="utf-8")
    script.chmod(0o755)

    bin_path = script
    if os.name == "nt":
        cmd_script = tmp_path / "fake_llm.cmd"
        cmd_script.write_text(f'@echo off\n"{sys.executable}" "{script}" %*\n', encoding="utf-8")
        bin_path = cmd_script

    # Stamp whatever version actually ships. A hard-coded "1" here meant the
    # fixture meeting was born stale the moment TEMPLATE_VERSION moved past it.
    from pipeline.config import TEMPLATE_VERSION

    minutes_file = tmp_path / "minutes_response.md"
    minutes_file.write_text(
        minutes.replace("__TEMPLATE_VERSION__", TEMPLATE_VERSION), encoding="utf-8"
    )

    return {
        "MMC_LLM_PROVIDERS": "gemini",
        "MMC_GEMINI_BIN": str(bin_path),
        "MMC_GEMINI_ARGS": " ",  # no args; prompt arrives on stdin
        "FAKE_LLM_MINUTES": str(minutes_file),
        "FAKE_LLM_SPEAKERS": SPEAKER_JSON,
    }


# ── Fake ASR ──────────────────────────────────────────────────────────

class FakeASRBackend:
    """Deterministic two-speaker transcript.

    Implements the same `Backend` protocol as WhisperXBackend, which is the escape
    hatch the design promised - this test is also proof that the seam works.
    """

    name = "fake:test-asr"

    def __init__(self, speakers: int = 2, fail: bool = False):
        self.speakers = speakers
        self.fail = fail
        self.calls: list[str] = []

    def transcribe(self, audio_path: Path, meeting_id: str, initial_prompt: str):
        from pipeline.asr import Segment, Transcript

        self.calls.append(str(audio_path))
        if self.fail:
            raise RuntimeError("simulated ASR failure")

        lines = [
            ("SPEAKER_00", "Let's review Atlas before the 2026.4 release."),
            ("SPEAKER_01", "The rate limiter is not ready yet."),
            ("SPEAKER_00", "Then we defer Atlas to Q1 and take Northwind's SSO request."),
            ("SPEAKER_01", "I'll scope the SSO work by next Monday."),
        ]
        segments = [
            Segment(
                start=float(i), end=float(i + 1), text=text,
                speaker=f"SPEAKER_{i % self.speakers:02d}" if self.speakers else None,
            )
            for i, (_, text) in enumerate(lines)
        ]
        return Transcript(
            meeting_id=meeting_id,
            model=self.name,
            language="en",
            duration_sec=float(len(lines)),
            segments=segments,
        )


# ── Environment ───────────────────────────────────────────────────────

def pipeline_env(root: Path, lightrag_url: str, **extra: str) -> dict[str, str]:
    """MMC_* environment pointing the pipeline at a throwaway tree."""
    env = {
        "MMC_INBOX": str(root / "inbox"),
        "MMC_AUDIO": str(root / "audio"),
        "MMC_TRANSCRIPTS": str(root / "transcripts"),
        "MMC_MINUTES": str(root / "minutes"),
        "MMC_DB_DIR": str(root / "db"),
        "MMC_LIGHTRAG_URL": lightrag_url,
        "MMC_LIGHTRAG_API_KEY": "test-key",
        "MMC_TIMEZONE": "America/Toronto",
        "MMC_OWNER_NAME": "Faraz",
    }
    env.update(extra)
    return env


def apply_env(monkeypatch, env: dict[str, str], modules: list | None = None) -> None:
    """Apply env vars AND re-point already-imported module constants.

    `pipeline.config` reads the environment at import time, and every module does
    `from pipeline.config import AUDIO_DIR` — binding its own copy. Setting the
    environment alone therefore changes nothing for a module already imported, so
    every module holding a copy must be patched.

    The module list is enumerated rather than passed in: forgetting one silently
    points a single stage at the real repo directories, which is a confusing
    failure to debug and exactly what happened the first time these tests ran.
    """
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    from pipeline import (
        answer,
        asr,
        backup,
        cli,
        compile_minutes,
        config,
        doctor,
        index,
        ingest,
        speakers,
    )
    from pipeline import (
        db as db_module,
    )

    targets = [
        config, ingest, asr, speakers, compile_minutes,
        index, answer, backup, doctor, cli, db_module,
    ]
    if modules:
        targets.extend(m for m in modules if m not in targets)

    path_attrs = {
        "MMC_INBOX": ("INBOX_DIR", Path),
        "MMC_AUDIO": ("AUDIO_DIR", Path),
        "MMC_TRANSCRIPTS": ("TRANSCRIPTS_DIR", Path),
        "MMC_MINUTES": ("MINUTES_DIR", Path),
        "MMC_LIGHTRAG_URL": ("LIGHTRAG_URL", str),
        "MMC_LIGHTRAG_API_KEY": ("LIGHTRAG_API_KEY", str),
    }
    for env_key, (attr, caster) in path_attrs.items():
        if env_key in env:
            for module in targets:
                if hasattr(module, attr):
                    monkeypatch.setattr(module, attr, caster(env[env_key]))

    if "MMC_DB_DIR" in env:
        db_path = Path(env["MMC_DB_DIR"]) / "manifest.db"
        for module in targets:
            if hasattr(module, "DB_PATH"):
                monkeypatch.setattr(module, "DB_PATH", db_path)

    if "MMC_LLM_PROVIDERS" in env:
        providers = [p.strip() for p in env["MMC_LLM_PROVIDERS"].split(",") if p.strip()]
        for module in targets:
            if hasattr(module, "LLM_PROVIDER_ORDER"):
                monkeypatch.setattr(module, "LLM_PROVIDER_ORDER", providers)
            if hasattr(module, "LLM_PROVIDERS"):
                monkeypatch.setattr(module, "LLM_PROVIDERS", providers)

    # Keep the pipeline away from the repo's own glossary and overrides.
    root = Path(env["MMC_INBOX"]).parent
    for attr, filename in (
        ("GLOSSARY_FILE", "glossary.md"),
        ("SPEAKER_OVERRIDES_FILE", "speaker-overrides.yaml"),
    ):
        for module in targets:
            if hasattr(module, attr):
                monkeypatch.setattr(module, attr, root / filename)

    for key in ("MMC_INBOX", "MMC_AUDIO", "MMC_TRANSCRIPTS", "MMC_MINUTES", "MMC_DB_DIR"):
        if key in env:
            os.makedirs(env[key], exist_ok=True)

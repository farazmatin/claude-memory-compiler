"""Pipeline orchestrator.

Each stage claims meetings at one status and advances them to the next. Stages
run independently and are resumable, which is the central design property: ASR
costs 30-50 CPU-minutes per meeting and must never be repeated, so a crash during
minutes compilation loses minutes of work rather than hours.

    pipeline init                  create dirs and the manifest
    pipeline auth-drive            authorize the private Drive collector
    pipeline capture               download new Drive audio into the inbox
    pipeline ingest                discover + dedup new audio
    pipeline transcribe            ASR + align + diarize      (the expensive one)
    pipeline speakers              resolve SPEAKER_xx -> names
    pipeline minutes               compile structured minutes
    pipeline index                 push minutes into LightRAG
    pipeline run                   every pending stage, in order
    pipeline dashboard             browse and search the local meeting record
    pipeline query "question"      ask the knowledge base
    pipeline status                where everything is, plus stage timings
    pipeline retry                 requeue failed meetings

Wire `pipeline run` to a nightly timer, not a filesystem watcher: the combined
ASR + indexing window is measured in hours, so synchronous-on-arrival runs would
collide with each other.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from pipeline import capture, compile_minutes, db, entities, index, ingest, speakers
from pipeline.config import (
    DASHBOARD_HOST,
    DASHBOARD_PORT,
    OWNER_NAME,
    TEMPLATE_VERSION,
    ensure_dirs,
)

# Stage names as recorded in stage_runs, for timing analysis.
STAGE_TRANSCRIBE = "transcribe"
STAGE_SPEAKERS = "speakers"
STAGE_MINUTES = "minutes"
STAGE_INDEX = "index"


# ── init / status ─────────────────────────────────────────────────────

def cmd_init(_args: argparse.Namespace) -> int:
    ensure_dirs()
    db.init_db()
    print("Initialized working directories and manifest.")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    ensure_dirs()
    db.init_db()
    with db.connect() as conn:
        counts = db.status_counts(conn)
        total = sum(counts.values())
        print(f"Meetings: {total}")
        for status in [*db.STATUS_ORDER, db.FAILED]:
            if counts.get(status):
                print(f"  {status:<18} {counts[status]}")

        stale = db.stale_template(conn, TEMPLATE_VERSION)
        if stale:
            print(
                f"\n{len(stale)} meeting(s) built with an older template "
                f"(current: v{TEMPLATE_VERSION}). Rebuild with: "
                f"pipeline minutes --recompile"
            )

        timings = db.stage_timings(conn)
        if timings:
            print("\nStage timings (wall clock):")
            print(f"  {'stage':<12} {'runs':>5} {'ok':>4} {'avg':>9} {'max':>9}")
            for row in timings:
                avg = float(row["avg_sec"] or 0) / 60
                mx = float(row["max_sec"] or 0) / 60
                print(
                    f"  {row['stage']!s:<12} {row['runs']:>5} "
                    f"{row['ok_runs'] or 0:>4} {avg:>8.1f}m {mx:>8.1f}m"
                )

        failed = db.pending(conn, db.FAILED)
        if failed:
            print("\nFailed:")
            for meeting in failed:
                print(f"  {meeting.label}: {(meeting.error or '').splitlines()[:1]}")

    try:
        info = index.health()
        print(f"\nLightRAG: reachable ({info.get('status', 'ok')})")
    except index.IndexError_ as exc:
        print(f"\nLightRAG: {exc}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    ensure_dirs()
    db.init_db()
    print("Scanning inbox...")
    counts = ingest.run()
    linked = capture.reconcile_ingested()
    print(
        f"\nscanned {counts['scanned']}, ingested {counts['ingested']}, "
        f"duplicate {counts['duplicate']}, skipped {counts['skipped']}, "
        f"failed {counts['failed']}"
    )
    if linked:
        print(f"released {linked} local Drive handoff file(s)")
    if args.then_run:
        # Skip ingest inside the chain: it just ran, and re-running it would
        # re-hash the whole inbox for nothing.
        return _run_all(
            argparse.Namespace(limit=None, no_llm=False, owner=None),
            include_ingest=False,
        )
    return 1 if counts["failed"] else 0


def cmd_auth_drive(_args: argparse.Namespace) -> int:
    try:
        capture.authorize()
    except capture.CaptureError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    if args.complete_backfill:
        try:
            capture.complete_backfill()
        except capture.CaptureError as exc:
            print(exc, file=sys.stderr)
            return 1
        print("Backfill folder disabled. Future recordings remain enabled.")
        return 0

    try:
        counts = capture.run(dry_run=args.dry_run)
    except capture.CaptureError as exc:
        print(exc, file=sys.stderr)
        return 1
    if not any(counts.values()):
        print("Drive capture is not configured; skipping.")
        return 0
    verb = "eligible" if args.dry_run else "downloaded"
    print(
        f"Drive capture: scanned {counts['scanned']}, {verb} {counts[verb]}, "
        f"known {counts['already_known']}, excluded {counts['excluded']}, "
        f"ambiguous {counts['ambiguous']}, failed {counts['failed']}"
    )
    return 1 if counts["failed"] else 0


# ── Stage runners ─────────────────────────────────────────────────────

def cmd_transcribe(args: argparse.Namespace) -> int:
    """ASR + alignment + diarization. The expensive stage."""
    from pipeline import asr

    db.init_db()
    with db.connect() as conn:
        queue = db.pending(conn, db.DISCOVERED, args.limit)

    if not queue:
        print("Nothing to transcribe.")
        return 0

    backend = asr.default_backend()
    prompt = asr.build_initial_prompt()
    if prompt:
        print(f"Vocabulary bias: {prompt[:120]}{'...' if len(prompt) > 120 else ''}")

    failures = 0
    for position, meeting in enumerate(queue, 1):
        print(f"[{position}/{len(queue)}] {meeting.label}")
        audio_path = meeting.audio_path
        if not audio_path or not Path(audio_path).is_file():
            try:
                audio_path = str(capture.rehydrate_audio(meeting))
                print("    rehydrated archived source from Drive")
            except capture.CaptureError as exc:
                with db.connect() as conn:
                    db.mark_failed(conn, meeting.id, str(exc))
                failures += 1
                print(f"    FAILED {exc}")
                continue
        if not audio_path or not Path(audio_path).is_file():
            with db.connect() as conn:
                db.mark_failed(conn, meeting.id, "audio file missing and could not be restored")
            failures += 1
            continue

        with db.connect() as conn:
            run_id = db.start_stage(conn, meeting.id, STAGE_TRANSCRIBE)
        try:
            transcript = backend.transcribe(
                Path(audio_path), meeting.id, prompt
            )
            json_path = asr.save_transcript(transcript)
            with db.connect() as conn:
                db.finish_stage(
                    conn, run_id, True,
                    f"{len(transcript.segments)} segments, "
                    f"{len(transcript.speaker_labels)} speakers",
                )
                db.advance(
                    conn, meeting.id, db.TRANSCRIBED,
                    transcript_path=str(json_path),
                    asr_model=transcript.model,
                    duration_sec=transcript.duration_sec or meeting.duration_sec,
                )
            try:
                if capture.cleanup_transcribed_audio(meeting.id, audio_path):
                    print("    released local Drive audio")
            except capture.CaptureError as exc:
                print(f"    WARNING {exc}")
            print(
                f"    {len(transcript.segments)} segments, "
                f"{len(transcript.speaker_labels)} speaker(s)"
            )
            if transcript.diarization_warning:
                print(f"    WARNING {transcript.diarization_warning}")
        except Exception as exc:
            failures += 1
            detail = f"{type(exc).__name__}: {exc}"
            print(f"    FAILED {detail}")
            if args.traceback:
                traceback.print_exc()
            with db.connect() as conn:
                db.finish_stage(conn, run_id, False, detail)
                db.mark_failed(conn, meeting.id, detail)

    print(f"\nTranscribed {len(queue) - failures}/{len(queue)}.")
    return 1 if failures and not args.keep_going else 0


def cmd_speakers(args: argparse.Namespace) -> int:
    from pipeline import asr

    db.init_db()
    with db.connect() as conn:
        if getattr(args, "all", False):
            rows = conn.execute("""
                SELECT DISTINCT m.id
                FROM meetings m
                JOIN speakers s ON s.meeting_id = m.id
                WHERE s.name IS NULL OR s.name = ''
            """).fetchall()
            queue = [db.get_meeting(conn, r["id"]) for r in rows if db.get_meeting(conn, r["id"])]
            if args.limit:
                queue = queue[: args.limit]
        else:
            queue = db.pending(conn, db.TRANSCRIBED, args.limit)

    if not queue:
        print("Nothing to resolve.")
        return 0

    failures = 0
    for position, meeting in enumerate(queue, 1):
        print(f"[{position}/{len(queue)}] {meeting.label}")
        with db.connect() as conn:
            run_id = db.start_stage(conn, meeting.id, STAGE_SPEAKERS)
        try:
            transcript = asr.load_transcript(meeting.id)
            with db.connect() as conn:
                resolved = speakers.resolve(
                    conn, meeting, transcript,
                    owner_name=args.owner,
                    use_llm=not args.no_llm,
                )
            # Rewrite the readable transcript with real names now that we
            # have them; the JSON keeps the raw labels as ground truth.
            asr.save_transcript(transcript, resolved)
            unresolved = [
                label for label in transcript.speaker_labels if label not in resolved
            ]
            with db.connect() as conn:
                db.finish_stage(
                    conn, run_id, True,
                    f"{len(resolved)} resolved, {len(unresolved)} unresolved",
                )
                current = db.get_meeting(conn, meeting.id)
                if current and current.status == db.TRANSCRIBED:
                    db.advance(conn, meeting.id, db.SPEAKERS_RESOLVED)
            summary = ", ".join(f"{k}={v}" for k, v in resolved.items()) or "none"
            print(f"    {summary}")
            if unresolved:
                print(f"    unresolved: {', '.join(unresolved)}")
        except Exception as exc:
            failures += 1
            detail = f"{type(exc).__name__}: {exc}"
            print(f"    FAILED {detail}")
            if args.traceback:
                traceback.print_exc()
            with db.connect() as conn:
                db.finish_stage(conn, run_id, False, detail)
                db.mark_failed(conn, meeting.id, detail)

    return 1 if failures else 0


def cmd_minutes(args: argparse.Namespace) -> int:
    from pipeline import asr

    db.init_db()
    with db.connect() as conn:
        if args.recompile:
            # The recompilation path: rebuild from retained transcripts after a
            # template change, with no ASR cost.
            queue = db.stale_template(conn, TEMPLATE_VERSION)
            if args.limit:
                queue = queue[: args.limit]
        else:
            queue = db.pending(conn, db.SPEAKERS_RESOLVED, args.limit)

    if not queue:
        print("Nothing to compile.")
        return 0

    failures = 0
    for position, meeting in enumerate(queue, 1):
        print(f"[{position}/{len(queue)}] {meeting.label}")
        with db.connect() as conn:
            run_id = db.start_stage(conn, meeting.id, STAGE_MINUTES)
            resolved = db.get_speakers(conn, meeting.id)
        try:
            transcript = asr.load_transcript(meeting.id)
            with db.connect() as conn:
                path, document = compile_minutes.compile_meeting(
                    conn, meeting, transcript, resolved
                )
            words = len(document.split())
            with db.connect() as conn:
                db.finish_stage(conn, run_id, True, f"{words} words -> {path.name}")
                # A recompile rewinds to minutes_compiled so the index stage
                # picks the new version up.
                db.advance(
                    conn, meeting.id, db.MINUTES_COMPILED,
                    minutes_path=str(path),
                    template_version=TEMPLATE_VERSION,
                )
            print(f"    {words} words -> {path.name}")
        except Exception as exc:
            failures += 1
            detail = f"{type(exc).__name__}: {exc}"
            print(f"    FAILED {detail}")
            if args.traceback:
                traceback.print_exc()
            with db.connect() as conn:
                db.finish_stage(conn, run_id, False, detail)
            # Deliberately not marked failed: the transcript is intact and
            # the model call is retryable, so the meeting stays in the queue.

    print(f"\nCompiled {len(queue) - failures}/{len(queue)}.")
    return 1 if failures else 0


def cmd_index(args: argparse.Namespace) -> int:
    db.init_db()

    try:
        index.health()
    except index.IndexError_ as exc:
        print(f"{exc}\nStart it with: docker compose up -d")
        return 1

    with db.connect() as conn:
        queue = db.pending(conn, db.MINUTES_COMPILED, args.limit)

    if not queue:
        print("Nothing to index.")
        return 0

    failures = 0
    stale = 0
    for position, meeting in enumerate(queue, 1):
        print(f"[{position}/{len(queue)}] {meeting.label}")
        if not meeting.minutes_path:
            continue
        path = Path(meeting.minutes_path)
        if not path.exists():
            print(f"    FAILED minutes file missing: {path}")
            failures += 1
            continue

        with db.connect() as conn:
            run_id = db.start_stage(conn, meeting.id, STAGE_INDEX)
            try:
                # Append the canonicalized graph block from the manifest. The
                # local extraction model reads an explicit list reliably and
                # discovers the same facts from prose unreliably - this is the
                # mitigation for that ceiling.
                augment = entities.render_for_index(
                    db.get_entities(conn, meeting.id),
                    db.get_relations(conn, meeting.id),
                )
                doc_id, replaced = index.replace_minutes(
                    path, meeting.lightrag_doc_id, augment=augment
                )
                if not replaced:
                    # The old copy is still in the graph. Refusing to insert keeps
                    # the corpus consistent instead of leaving two contradictory
                    # versions of the same meeting for retrieval to trip over.
                    stale += 1
                    detail = f"stale copy {meeting.lightrag_doc_id} could not be deleted"
                    print(f"    SKIPPED {detail}")
                    db.finish_stage(conn, run_id, False, detail)
                    continue
                db.finish_stage(conn, run_id, True, f"{path.name} -> {doc_id}")
                db.advance(conn, meeting.id, db.INDEXED, lightrag_doc_id=doc_id)
                print("    indexed")
            except index.IndexError_ as exc:
                failures += 1
                print(f"    FAILED {exc}")
                db.finish_stage(conn, run_id, False, str(exc))

    print(f"\nIndexed {len(queue) - failures - stale}/{len(queue)}.")
    if stale:
        print(
            f"{stale} meeting(s) skipped because a previously indexed version "
            f"could not be removed. Delete them in the LightRAG UI, then re-run."
        )
    return 1 if (failures or stale) else 0


def _run_all(args: argparse.Namespace, include_ingest: bool = True) -> int:
    """Walk every stage in order, oldest meeting first.

    Returns non-zero if ANY stage failed. This runs from a nightly timer, and a
    batch that exits 0 after every stage failed is indistinguishable from success
    - which means a break in month 4 goes unnoticed until a query comes back
    empty in month 9.
    """
    stages: list[tuple[str, object, argparse.Namespace]] = []
    if include_ingest:
        stages.append(("capture", cmd_capture, argparse.Namespace(dry_run=False, complete_backfill=False)))
        stages.append(("ingest", cmd_ingest, argparse.Namespace(then_run=False)))
    stages += [
        # keep_going stays False here. It means "exit 0 despite failures", which is
        # only ever an interactive convenience — inside the nightly batch it would
        # hide a total transcription failure behind a success exit code, which is
        # the very thing this function exists to prevent. Continuing past one bad
        # file is separate and unconditional: the per-meeting try/except does it.
        ("transcribe", cmd_transcribe, argparse.Namespace(
            limit=args.limit, keep_going=False, traceback=False)),
        ("speakers", cmd_speakers, argparse.Namespace(
            limit=args.limit, owner=getattr(args, "owner", None),
            no_llm=args.no_llm, traceback=False)),
        ("minutes", cmd_minutes, argparse.Namespace(
            limit=args.limit, recompile=False, traceback=False)),
        ("index", cmd_index, argparse.Namespace(limit=args.limit)),
    ]
    failed: list[str] = []
    crashes: list[str] = []
    for name, handler, stage_args in stages:
        print(f"\n=== {name} ===")
        try:
            if handler(stage_args):  # type: ignore[operator]
                failed.append(name)
        except Exception as exc:
            failed.append(name)
            crashes.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"  stage crashed: {type(exc).__name__}: {exc}")
            traceback.print_exc()

    if failed:
        print(f"\nFAILED stages: {', '.join(failed)}")
        print("Run `pipeline status` for detail.")
        # A non-zero exit is not enough on a headless server: cron mails the local
        # user and nobody reads local mail. Push the failure somewhere visible.
        from pipeline import alert

        alert.send(failed, detail="\n".join(crashes))
        return 1
    print("\nAll stages completed.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    ensure_dirs()
    db.init_db()
    return _run_all(args)


def cmd_query(args: argparse.Namespace) -> int:
    from pipeline import answer

    try:
        result = answer.ask(
            args.question,
            mode=args.mode,
            top_k=args.top_k,
            synthesize=not args.local,
        )
    except index.IndexError_ as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    print(result.text)
    if args.timing:
        print(f"\n{result.timing_line()}")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Open the local, read-only meeting-memory control room."""
    from pipeline import dashboard

    dashboard.run(host=args.host, port=args.port, open_browser=args.open)
    return 0


def cmd_people(args: argparse.Namespace) -> int:
    """Inspect and curate the people registry.

    Curating this is worth the effort: every spelling variant becomes a separate
    node in the knowledge graph, so one person recorded three ways is three
    disconnected entities that no query finds together.
    """
    db.init_db()
    with db.connect() as conn:
        if args.merge:
            source, target = args.merge
            rewritten = db.merge_person(conn, source, target)
            print(f"Merged {source!r} into {target!r} ({rewritten} speaker row(s) rewritten).")
            return 0

        if args.add:
            canonical, *aliases = args.add
            db.add_person(conn, canonical, aliases=aliases)
            print(f"Registered {canonical!r}" + (f" with aliases {aliases}" if aliases else ""))
            return 0

        people = db.list_people(conn)
        if not people:
            print("No people registered yet. They are added automatically as meetings resolve.")
            return 0
        print(f"{'name':<24} {'role':<14} {'meetings':>8}  aliases")
        for person in people:
            print(
                f"{person['canonical']!s:<24} {person['role'] or ''!s:<14} "
                f"{person['meetings'] or 0:>8}  {person['aliases'] or ''}"
            )
    return 0


def cmd_entities(args: argparse.Namespace) -> int:
    """Most-mentioned entities across the corpus.

    A useful health check on graph quality: if the top entries are generic words
    rather than product and people names, the compiler is emitting noise.
    """
    db.init_db()
    with db.connect() as conn:
        rows = db.entity_mentions(conn, args.limit)
    if not rows:
        print("No entities recorded yet.")
        return 0
    print(f"{'entity':<32} {'kind':<12} {'meetings':>8}")
    for row in rows:
        print(f"{row['name']!s:<32} {row['kind'] or ''!s:<12} {row['meetings']:>8}")
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    """Preflight the environment.

    Verifies nothing is obviously broken. It cannot tell you the minutes are any
    good - only running a real meeting through and reading the result does that.
    """
    from pipeline import doctor

    checks, ok = doctor.run()
    for check in checks:
        print(f"[{check.symbol}] {check.name:<22} {check.detail}")
        if check.fix and check.status != doctor.OK:
            print(f"{'':<9} -> {check.fix}")

    failed = sum(1 for c in checks if c.status == doctor.FAIL)
    warned = sum(1 for c in checks if c.status == doctor.WARN)
    print(f"\n{len(checks)} checks: {failed} failed, {warned} warnings")
    if ok:
        print(
            "\nEnvironment looks ready. This does NOT verify output quality - run one "
            "real meeting and read the transcript against the audio."
        )
    return 0 if ok else 1


def cmd_backup(args: argparse.Namespace) -> int:
    from pipeline import backup

    report = backup.run(Path(args.to), include_audio=not args.no_audio)
    print(report.summary())
    if report.errors:
        print(f"\n{len(report.errors)} error(s):")
        for error in report.errors[:20]:
            print(f"  {error}")
        return 1
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    """Requeue failed meetings at the given stage."""
    db.init_db()
    with db.connect() as conn:
        failed = db.pending(conn, db.FAILED)
        if not failed:
            print("Nothing failed.")
            return 0
        for meeting in failed:
            db.reset_to(conn, meeting.id, args.status)
            print(f"  requeued {meeting.label} -> {args.status}")
    print(f"\nRequeued {len(failed)} meeting(s).")
    return 0


# ── Argument parsing ──────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipeline",
        description="Compile meeting audio into minutes and a graph-RAG knowledge base.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--limit", type=int, default=None, help="process at most N meetings")
        sub.add_argument("--traceback", action="store_true", help="print full tracebacks")

    subparsers.add_parser("init", help="create directories and the manifest").set_defaults(
        func=cmd_init
    )
    subparsers.add_parser("status", help="pipeline state and stage timings").set_defaults(
        func=cmd_status
    )

    p_ingest = subparsers.add_parser("ingest", help="discover and dedup new audio")
    p_ingest.add_argument(
        "--then-run", action="store_true", help="continue through the remaining stages"
    )
    p_ingest.set_defaults(func=cmd_ingest)

    subparsers.add_parser("auth-drive", help="authorize private Google Drive access").set_defaults(
        func=cmd_auth_drive
    )
    p_capture = subparsers.add_parser("capture", help="download approved Drive audio")
    p_capture.add_argument("--dry-run", action="store_true", help="preview eligible Drive files")
    p_capture.add_argument(
        "--complete-backfill",
        action="store_true",
        help="disable the one-time backfill folder after it has been ingested",
    )
    p_capture.set_defaults(func=cmd_capture)

    p_transcribe = subparsers.add_parser(
        "transcribe", help="ASR + alignment + diarization (slow)"
    )
    add_common(p_transcribe)
    p_transcribe.add_argument(
        "--keep-going", action="store_true",
        help="exit 0 even if some files failed (interactive use only; the batch "
             "never sets this, or a total failure would look like success)",
    )
    p_transcribe.set_defaults(func=cmd_transcribe)

    p_speakers = subparsers.add_parser("speakers", help="resolve speaker labels to names")
    add_common(p_speakers)
    p_speakers.add_argument(
        "--owner", default=OWNER_NAME or None,
        help="your own name, used to disambiguate two-speaker recordings "
             "(defaults to MMC_OWNER_NAME)",
    )
    p_speakers.add_argument(
        "--no-llm", action="store_true", help="heuristics and overrides only"
    )
    p_speakers.add_argument(
        "--all", action="store_true", help="resolve across all meetings with unresolved speakers"
    )
    p_speakers.set_defaults(func=cmd_speakers)

    p_minutes = subparsers.add_parser("minutes", help="compile structured minutes")
    add_common(p_minutes)
    p_minutes.add_argument(
        "--recompile", action="store_true",
        help="rebuild minutes whose template_version is stale, from retained "
             "transcripts (no ASR cost)",
    )
    p_minutes.set_defaults(func=cmd_minutes)

    p_index = subparsers.add_parser("index", help="push minutes into LightRAG")
    add_common(p_index)
    p_index.set_defaults(func=cmd_index)

    p_run = subparsers.add_parser("run", help="every pending stage, in order")
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--owner", default=OWNER_NAME or None)
    p_run.add_argument("--no-llm", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_query = subparsers.add_parser("query", help="ask the knowledge base")
    p_query.add_argument("question")
    p_query.add_argument(
        "--mode", default=None,
        choices=["hybrid", "global", "local", "naive", "mix"],
        help="hybrid (default) balances graph and vector; global for questions "
             "whose answer spans many meetings",
    )
    p_query.add_argument("--top-k", type=int, default=None)
    p_query.add_argument(
        "--local", action="store_true",
        help="let LightRAG's local model write the answer instead of the "
             "subscription chain (faster to start, lower quality)",
    )
    p_query.add_argument(
        "--timing", action="store_true",
        help="report retrieval vs synthesis time, to see which phase is slow",
    )
    p_query.set_defaults(func=cmd_query)

    p_dashboard = subparsers.add_parser(
        "dashboard", help="browse and search the local meeting record"
    )
    p_dashboard.add_argument(
        "--host", default=DASHBOARD_HOST,
        help="bind address (default: loopback only)",
    )
    p_dashboard.add_argument(
        "--port", type=int, default=DASHBOARD_PORT,
        help=f"listen port (default: {DASHBOARD_PORT})",
    )
    p_dashboard.add_argument(
        "--open", action="store_true", help="open the dashboard in the default browser"
    )
    p_dashboard.set_defaults(func=cmd_dashboard)

    p_people = subparsers.add_parser("people", help="inspect the people registry")
    p_people.add_argument(
        "--add", nargs="+", metavar=("CANONICAL", "ALIAS"),
        help="register a canonical name and optional aliases",
    )
    p_people.add_argument(
        "--merge", nargs=2, metavar=("FROM", "INTO"),
        help="fold one person into another, rewriting existing records",
    )
    p_people.set_defaults(func=cmd_people)

    p_entities = subparsers.add_parser("entities", help="most-mentioned entities")
    p_entities.add_argument("--limit", type=int, default=50)
    p_entities.set_defaults(func=cmd_entities)

    subparsers.add_parser(
        "doctor", help="preflight the environment before a real batch"
    ).set_defaults(func=cmd_doctor)

    p_backup = subparsers.add_parser(
        "backup", help="back up transcripts, minutes, audio and the manifest"
    )
    p_backup.add_argument("--to", required=True, help="destination directory")
    p_backup.add_argument(
        "--no-audio", action="store_true",
        help="skip audio (the bulkiest tier); a restore can then rebuild from "
             "transcripts but never re-transcribe",
    )
    p_backup.set_defaults(func=cmd_backup)

    p_retry = subparsers.add_parser("retry", help="requeue failed meetings")
    p_retry.add_argument(
        "--status", default=db.DISCOVERED, choices=db.STATUS_ORDER,
        help="stage to requeue at (default: discovered, i.e. re-transcribe)",
    )
    p_retry.set_defaults(func=cmd_retry)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())

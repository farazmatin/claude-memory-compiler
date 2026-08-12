"""Pipeline orchestrator.

Each stage claims meetings at one status and advances them to the next. Stages
run independently and are resumable, which is the central design property: ASR
costs 30-50 CPU-minutes per meeting and must never be repeated, so a crash during
minutes compilation loses minutes of work rather than hours.

    pipeline init                  create dirs and the manifest
    pipeline ingest                discover + dedup new audio
    pipeline transcribe            ASR + align + diarize      (the expensive one)
    pipeline speakers              resolve SPEAKER_xx -> names
    pipeline minutes               compile structured minutes
    pipeline index                 push minutes into LightRAG
    pipeline run                   every pending stage, in order
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

from pipeline import compile_minutes, db, index, ingest, speakers
from pipeline.config import OWNER_NAME, TEMPLATE_VERSION, ensure_dirs

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
    print(
        f"\nscanned {counts['scanned']}, ingested {counts['ingested']}, "
        f"duplicate {counts['duplicate']}, skipped {counts['skipped']}, "
        f"failed {counts['failed']}"
    )
    if args.then_run:
        # Skip ingest inside the chain: it just ran, and re-running it would
        # re-hash the whole inbox for nothing.
        return _run_all(
            argparse.Namespace(limit=None, no_llm=False, owner=None),
            include_ingest=False,
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
        if not meeting.audio_path:
            with db.connect() as conn:
                db.mark_failed(conn, meeting.id, "no audio_path recorded")
            failures += 1
            continue

        with db.connect() as conn:
            run_id = db.start_stage(conn, meeting.id, STAGE_TRANSCRIBE)
        try:
            transcript = backend.transcribe(
                Path(meeting.audio_path), meeting.id, prompt
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
            print(
                f"    {len(transcript.segments)} segments, "
                f"{len(transcript.speaker_labels)} speaker(s)"
            )
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
                db.finish_stage(
                    conn, run_id, True,
                    f"{len(resolved)} resolved, {len(unresolved)} unresolved",
                )
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
            try:
                transcript = asr.load_transcript(meeting.id)
                resolved = db.get_speakers(conn, meeting.id)
                path, document = compile_minutes.compile_meeting(
                    conn, meeting, transcript, resolved
                )
                words = len(document.split())
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
                doc_id, replaced = index.replace_minutes(path, meeting.lightrag_doc_id)
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
        stages.append(("ingest", cmd_ingest, argparse.Namespace(then_run=False)))
    stages += [
        ("transcribe", cmd_transcribe, argparse.Namespace(
            limit=args.limit, keep_going=True, traceback=False)),
        ("speakers", cmd_speakers, argparse.Namespace(
            limit=args.limit, owner=getattr(args, "owner", None),
            no_llm=args.no_llm, traceback=False)),
        ("minutes", cmd_minutes, argparse.Namespace(
            limit=args.limit, recompile=False, traceback=False)),
        ("index", cmd_index, argparse.Namespace(limit=args.limit)),
    ]

    failed: list[str] = []
    for name, handler, stage_args in stages:
        print(f"\n=== {name} ===")
        try:
            if handler(stage_args):  # type: ignore[operator]
                failed.append(name)
        except Exception as exc:
            failed.append(name)
            print(f"  stage crashed: {type(exc).__name__}: {exc}")
            traceback.print_exc()

    if failed:
        print(f"\nFAILED stages: {', '.join(failed)}")
        print("Run `pipeline status` for detail.")
        return 1
    print("\nAll stages completed.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    ensure_dirs()
    db.init_db()
    return _run_all(args)


def cmd_query(args: argparse.Namespace) -> int:
    try:
        print(index.query(args.question, mode=args.mode, top_k=args.top_k))
    except index.IndexError_ as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    return 0


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

    p_transcribe = subparsers.add_parser(
        "transcribe", help="ASR + alignment + diarization (slow)"
    )
    add_common(p_transcribe)
    p_transcribe.add_argument(
        "--keep-going", action="store_true", help="exit 0 even if some files failed"
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
    p_query.set_defaults(func=cmd_query)

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

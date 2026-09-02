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
    pipeline voice                 re-band the voice queue; apply confident matches
    pipeline minutes               compile structured minutes
    pipeline graph-sync            publish subscription-authored graph records
    pipeline graph-sync            author the graph from the manifest
    pipeline chunk-index           build the BM25 index over compiled minutes
    pipeline watch                 process each newly-arrived Drive recording
    pipeline dashboard             browse and search the local meeting record
    pipeline query "question"      ask the knowledge base
    pipeline status                where everything is, plus stage timings
    pipeline retry                 requeue failed meetings

`pipeline watch` is the normal continuous entry point. It polls Drive, then
uses Replicate and the subscription-authored stages only for newly captured
recordings. `pipeline run` remains the explicit catch-up/recovery command.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import traceback
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

from pipeline import (
    capture,
    chunk_index,
    compile_minutes,
    db,
    entities,
    index,
    ingest,
    people_merge,
    speakers,
    voices,
)
from pipeline.config import (
    AUTO_REQUEUE_LIMIT,
    DASHBOARD_HOST,
    DASHBOARD_PORT,
    DB_DIR,
    DB_PATH,
    MIN_MEETING_SEC,
    MIN_TRANSCRIPT_WORDS,
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
                error = (meeting.error or "").splitlines()
                print(f"  {meeting.label}: {error[0] if error else ''}")

        # Meetings currently sitting at FAILED are listed above, but a stage
        # that failed and was later retried successfully leaves that meeting at
        # a healthy status - invisible here and everywhere else a human looks.
        # This corpus once had 99 failed transcribe runs, 2 failed minutes and
        # 1 failed index that nothing surfaced for exactly that reason.
        history = db.recent_stage_failures(conn)
        if history:
            print(f"\nRecent stage failures (last {len(history)}):")
            for row in history:
                detail = (row["detail"] or "").splitlines()[0] if row["detail"] else ""
                print(f"  {row['label']}: {row['stage']} - {detail[:120]}")

    try:
        from pipeline import graph_sync

        health = index.document_health()
        graph_entities = len(graph_sync.graph_labels())
        print("\nLightRAG readiness:")
        print(f"  {'documents stored':<23} {health.documents_stored}")
        print(f"  {'documents processed':<23} {health.documents_processed}")
        print(f"  {'vector chunks ready':<23} {health.vector_chunks_ready}")
        print(f"  {'graph entities ready':<23} {graph_entities}")
        print(f"  {'failed':<23} {health.failed}")
        print(f"  {'active':<23} {health.active}")
        print(f"  {'pipeline busy':<23} {str(health.pipeline_busy).lower()}")
        print(f"  {'recovery required':<23} {str(health.recovery_required).lower()}")
        if health.latest_message:
            print(f"  latest: {health.latest_message}")
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
    except Exception as exc:
        err_msg = f"Google Drive capture failed: {exc}"
        print(err_msg, file=sys.stderr)
        from pipeline import alert
        alert.send(["drive_capture"], detail=f"{err_msg}\n\nRe-authorize with:\n  uv run pipeline auth-drive")
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
    if counts["failed"]:
        from pipeline import alert
        alert.send(["drive_capture"], detail=f"Drive capture had {counts['failed']} failed downloads.")
        return 1
    return 0


# ── Stage runners ─────────────────────────────────────────────────────

def _is_transient(exc: BaseException) -> bool:
    """Is this failure worth requeueing on the next run?

    Only network faults. A rejected token, a missing file, or a malformed
    transcript fails identically every time, and requeueing one would spin every
    run forever without ever producing minutes.
    """
    import httpx

    if getattr(exc, "transient", False):
        return True
    return isinstance(exc, httpx.TransportError | TimeoutError | ConnectionError)


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
                db.mark_failed(conn, meeting.id, detail, retryable=_is_transient(exc))

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
    skipped = 0
    for position, meeting in enumerate(queue, 1):
        print(f"[{position}/{len(queue)}] {meeting.label}")
        with db.connect() as conn:
            run_id = db.start_stage(conn, meeting.id, STAGE_MINUTES)
            resolved = db.get_speakers(conn, meeting.id)
        try:
            transcript = asr.load_transcript(meeting.id)

            # Accidental phone-in-pocket recordings and short test clips still cost
            # a full LLM compile and land a noise document in the meeting library -
            # at roughly five a week that is pure recurring waste. Checked here,
            # before compile_meeting spends the LLM call. Parked rather than
            # dropped: a genuine sub-two-minute decision is rare but real, and
            # --force compiles one deliberately.
            duration = meeting.duration_sec
            if duration is None:
                duration = transcript.duration_sec or 0.0
            word_count = sum(len(seg.text.split()) for seg in transcript.segments)
            if not args.force and (
                duration < MIN_MEETING_SEC or word_count < MIN_TRANSCRIPT_WORDS
            ):
                reason = (
                    f"junk recording: {duration:.0f}s / {word_count} words, under "
                    f"the {MIN_MEETING_SEC:.0f}s / {MIN_TRANSCRIPT_WORDS}-word floor. "
                    f"Parked, not discarded - `pipeline retry --status "
                    f"speakers_resolved` then `pipeline minutes --force` compiles "
                    f"it anyway."
                )
                with db.connect() as conn:
                    db.finish_stage(conn, run_id, False, reason)
                    db.mark_failed(conn, meeting.id, reason)
                skipped += 1
                print(f"    SKIPPED {reason}")
                continue

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

    print(f"\nCompiled {len(queue) - failures - skipped}/{len(queue)}.")
    if skipped:
        print(
            f"{skipped} meeting(s) parked as junk recordings (see `pipeline status`)."
        )
    return 1 if failures else 0


def cmd_chunk_index(args: argparse.Namespace) -> int:
    """Rebuild the local BM25 index over compiled minutes.

    Cheap and safe to re-run: a meeting whose chunks all hash the same is
    skipped, so the normal case writes nothing. `--rebuild` forces the write
    anyway, which is what to reach for after changing the chunk geometry.
    """
    db.init_db()
    with db.connect() as conn:
        if args.meeting:
            # A mistyped id and an already-current meeting both write zero
            # chunks. Reporting them the same way would let a typo read as
            # success, so the manifest is consulted before anything is claimed.
            meeting = db.get_meeting(conn, args.meeting)
            if meeting is None:
                print(f"No meeting {args.meeting} in the manifest.", file=sys.stderr)
                return 1
            if not meeting.minutes_path:
                print(f"{meeting.label}: no minutes compiled yet.", file=sys.stderr)
                return 1
            written = chunk_index.reindex_meeting(conn, args.meeting, force=args.rebuild)
            if written:
                print(f"{meeting.label}: indexed {written} chunk(s).")
            else:
                print(f"{meeting.label}: unchanged (no chunks written).")
            return 0

        stats = chunk_index.reindex_all(conn, force=args.rebuild)
        print(
            f"{stats['meetings']} meeting(s) with minutes: "
            f"{stats['reindexed']} reindexed, {stats['unchanged']} unchanged, "
            f"{stats['unreadable']} unreadable."
        )
        print(f"Wrote {stats['chunks']} chunk(s).")
        _, reason = chunk_index.index_status(conn)
        print(reason)
        # An unreadable minutes file is a real gap in the index, not a warning to
        # scroll past: the meeting is silently unsearchable until it is fixed.
        return 1 if stats["unreadable"] else 0


def cmd_graph_sync(args: argparse.Namespace) -> int:
    """Populate the knowledge graph from entities the minutes stage already extracted.

    The subscription model that writes the minutes has already produced the
    entities and relations, so this publishes those exact records. LightRAG is
    storage and traversal only and never invokes a model.
    """
    from pipeline import graph_sync

    db.init_db()
    try:
        index.health()
    except index.IndexError_ as exc:
        print(f"{exc}\nStart it with: docker compose up -d")
        return 1

    before = len(graph_sync.graph_labels())
    print(f"graph holds {before} entities before sync")
    report = graph_sync.sync()
    print(report.summary())
    for err in report.errors[:10]:
        print(f"  {err[:160]}")
    # Dropped records are not failures, but they are data the graph will never
    # hold. Print them so a junk extraction stays visible instead of silent.
    for drop in report.drops[:10]:
        print(f"  dropped {drop[:160]}")
    after = len(graph_sync.graph_labels())
    print(f"graph holds {after} entities after sync")
    if not after:
        return 1
    # A populated graph is not evidence that THIS run worked. Reporting success
    # after every write was refused would leave the graph quietly describing a
    # previous corpus.
    # A write the service reported as 5xx but the graph demonstrably kept counts
    # as a write. It is the normal outcome on this deployment - LightRAG commits
    # the record before upserting a vector it has no embedder for - and treating
    # it as a refusal reported a corpus-wide failure that had not happened.
    wrote = (
        report.entities_written
        + report.relations_written
        + report.entities_verified
        + report.relations_verified
    )
    if not wrote and report.errors:
        print(
            f"graph-sync wrote nothing: {len(report.errors)} write(s) refused. "
            "The graph still holds the PREVIOUS corpus. Review the errors and re-run."
        )
        return 1
    # Publication is the terminal context stage in the active build. Advance the
    # queued meetings only after the graph is demonstrably populated and this
    # sync reported no refused writes. The legacy document id remains untouched.
    with db.connect() as conn:
        queue = db.pending(conn, db.MINUTES_COMPILED, getattr(args, "limit", None))
        for meeting in queue:
            run_id = db.start_stage(conn, meeting.id, STAGE_INDEX)
            db.finish_stage(conn, run_id, True, "subscription-authored graph published")
            db.advance(conn, meeting.id, db.INDEXED)
    if queue:
        print(f"published context for {len(queue)} meeting(s)")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Sync all eligible professional minutes to the Product Manager repo."""
    from pipeline.compile_minutes import is_professional_minute
    from pipeline.config import ENABLE_PM_EXPORT, EXPORT_PM_MINUTES_DIR, MINUTES_DIR

    if not ENABLE_PM_EXPORT:
        print("[INFO] PM Export is disabled via MMC_ENABLE_PM_EXPORT=0")
        return 0

    EXPORT_PM_MINUTES_DIR.mkdir(parents=True, exist_ok=True)
    all_files = sorted(MINUTES_DIR.glob("*.md"))
    print(f"\n[SCAN] Evaluating {len(all_files)} minutes in: {MINUTES_DIR}")
    print(f"[TARGET] Export destination: {EXPORT_PM_MINUTES_DIR}\n")

    synced = 0
    skipped = 0
    for path in all_files:
        try:
            document = path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"  [WARN] Failed to read {path.name}: {e}")
            continue

        is_prof, reason = is_professional_minute(path, document)
        if not is_prof:
            skipped += 1
            continue

        dest_file = EXPORT_PM_MINUTES_DIR / path.name
        needs_write = False
        if (
            not dest_file.exists()
            or dest_file.stat().st_mtime < path.stat().st_mtime
            or dest_file.stat().st_size != path.stat().st_size
        ):
            needs_write = True

        if needs_write:
            dest_file.write_text(document, encoding="utf-8")
            print(f"  + Exported: {path.name} ({reason})")
        synced += 1

    print("\n" + "=" * 60)
    print(f"Export Complete: {synced} professional minutes in target, {skipped} personal/family quarantined.")
    print("=" * 60 + "\n")
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    """Index minutes and advance only after terminal document processing."""
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
    incomplete = 0
    for position, meeting in enumerate(queue, 1):
        print(f"[{position}/{len(queue)}] {meeting.label}")
        if not meeting.minutes_path:
            failures += 1
            print("    FAILED manifest has no minutes_path")
            continue
        path = Path(meeting.minutes_path)
        if not path.exists():
            failures += 1
            print(f"    FAILED minutes file missing: {path}")
            continue

        with db.connect() as conn:
            run_id = db.start_stage(conn, meeting.id, STAGE_INDEX)
            try:
                augment = entities.render_for_index(
                    db.get_entities(conn, meeting.id),
                    db.get_relations(conn, meeting.id),
                )
                doc_id, processed = index.replace_minutes(
                    path,
                    meeting.lightrag_doc_id,
                    augment=augment,
                )
                if not processed:
                    incomplete += 1
                    detail = f"document {doc_id} did not reach processed state"
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

    completed = len(queue) - failures - incomplete
    print(f"\nIndexed {completed}/{len(queue)}.")
    if incomplete:
        print(
            f"{incomplete} meeting(s) stayed at minutes_compiled because their "
            "documents did not reach terminal processed state."
        )
    return 1 if (failures or incomplete) else 0


def cmd_index_repair_preview(args: argparse.Namespace) -> int:
    """Write an exact, read-only plan for reconciling pending index records."""
    db.init_db()
    output_path = Path(args.to)
    if output_path.exists():
        print(f"Refusing to overwrite existing preview: {output_path}", file=sys.stderr)
        return 2

    targets: list[index.RepairTarget] = []
    missing_files: list[dict[str, str]] = []
    with db.connect() as conn:
        queue = db.pending(conn, db.MINUTES_COMPILED, args.limit)
        for meeting in queue:
            if not meeting.minutes_path:
                missing_files.append(
                    {"meeting_id": meeting.id, "reason": "manifest has no minutes_path"}
                )
                continue
            path = Path(meeting.minutes_path)
            if not path.exists():
                missing_files.append(
                    {
                        "meeting_id": meeting.id,
                        "minutes_path": str(path),
                        "reason": "minutes file is missing",
                    }
                )
                continue
            augment = entities.render_for_index(
                db.get_entities(conn, meeting.id),
                db.get_relations(conn, meeting.id),
            )
            desired_doc_id = index.compute_doc_id(path.read_text(encoding="utf-8") + augment)
            targets.append(
                index.RepairTarget(
                    meeting_id=meeting.id,
                    file_source=path.name,
                    desired_doc_id=desired_doc_id,
                    manifest_doc_id=meeting.lightrag_doc_id,
                )
            )

    try:
        preview = index.build_repair_preview(targets)
    except index.IndexError_ as exc:
        print(f"Could not build index repair preview: {exc}", file=sys.stderr)
        return 1

    artifact = {
        "schema_version": 1,
        "fingerprint": preview.fingerprint,
        "pipeline": {
            "busy": preview.pipeline_busy,
            "recovery_required": preview.recovery_required,
            "latest_message": preview.latest_message,
        },
        "items": [asdict(item) for item in preview.items],
        "missing_files": missing_files,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    actions = Counter(item.action for item in preview.items)
    print(f"Index repair preview: {len(preview.items)} document(s)")
    for action, count in sorted(actions.items()):
        print(f"  {action:<24} {count}")
    if missing_files:
        print(f"  {'missing_file':<24} {len(missing_files)}")
    print(f"Fingerprint: {preview.fingerprint}")
    print(f"Saved private preview: {output_path}")
    print("Nothing was changed. No document, graph, or manifest row was modified.")
    return 0


@contextlib.contextmanager
def pipeline_lease() -> Iterator[None]:
    """Serialize pipeline runs across the watcher, the dashboard, and the CLI.

    Three things start the same stages: `pipeline run`, the dashboard's Sync &
    Process Recordings button, and the watcher.  Nothing used to stop two of
    them working the same queue, and a second run re-compiling a meeting the
    first had already retitled is how minutes get orphaned on disk.

    Deliberately a different lease than the watcher's own: that one is held for
    the watcher's whole life, so sharing it would refuse every run for as long
    as the watcher stayed up.
    """
    from pipeline import watcher

    with watcher.WatchLease(DB_DIR / "pipeline-run.lock", label="A pipeline run"):
        yield


def run_all_stages(args: argparse.Namespace, include_ingest: bool = True) -> int:
    """Every stage in order, for a caller that already holds pipeline_lease().

    The dashboard takes the lease around its whole worker so a single-stage
    button and a full run cannot interleave, and then needs a way in that does
    not try to take it a second time.
    """
    return _run_all(args, include_ingest=include_ingest)


def _run_all(args: argparse.Namespace, include_ingest: bool = True) -> int:
    """Walk every stage in order, oldest meeting first.

    Returns non-zero if any stage failed. The command is deliberately
    operator-started, so failure remains visible to the person who requested it.
    """
    db.init_db()

    # A meeting parked by a dropped upload is invisible to every later stage:
    # `pending(DISCOVERED)` skips FAILED rows, so the run reports "Nothing to
    # transcribe" and no minutes are ever compiled. Spend one retry from its
    # budget here instead of waiting for someone to read the manifest.
    with db.connect() as conn:
        requeued = db.requeue_transient(conn, db.DISCOVERED, AUTO_REQUEUE_LIMIT)
    if requeued:
        print(
            f"Requeued {len(requeued)} meeting(s) that failed on a network fault: "
            + ", ".join(m[:12] for m in requeued)
        )

    stages: list[tuple[str, object, argparse.Namespace]] = []
    if include_ingest:
        stages.append(("capture", cmd_capture, argparse.Namespace(dry_run=False, complete_backfill=False)))
        stages.append(("ingest", cmd_ingest, argparse.Namespace(then_run=False)))
    stages += [
        # keep_going stays False here so failures are never reported as success.
        ("transcribe", cmd_transcribe, argparse.Namespace(
            limit=args.limit, keep_going=False, traceback=False)),
        ("speakers", cmd_speakers, argparse.Namespace(
            limit=args.limit, owner=getattr(args, "owner", None),
            no_llm=args.no_llm, traceback=False)),
        ("minutes", cmd_minutes, argparse.Namespace(
            limit=args.limit, recompile=False, traceback=False, force=False)),
        ("graph-sync", cmd_graph_sync, argparse.Namespace(limit=args.limit)),
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
    from pipeline import watcher

    ensure_dirs()
    db.init_db()
    try:
        with pipeline_lease():
            return _run_all(args)
    except watcher.WatcherAlreadyRunning as exc:
        print(f"{exc} Wait for it to finish, or stop it first.", file=sys.stderr)
        return 1


def cmd_watch(args: argparse.Namespace) -> int:
    """Continuously process a newly-arrived Drive recording, once and in order."""
    from pipeline import watcher

    ensure_dirs()
    db.init_db()

    def process_pending() -> int:
        # The dashboard button can be pressed mid-cycle, so the watcher takes the
        # run lease per cycle rather than assuming it is the only caller.
        try:
            with pipeline_lease():
                return _run_all(
                    argparse.Namespace(limit=args.limit, no_llm=False, owner=args.owner),
                    include_ingest=False,
                )
        except watcher.WatcherAlreadyRunning as exc:
            print(f"{exc} Leaving this cycle to it.", file=sys.stderr)
            return 0

    def cycle() -> watcher.CycleResult:
        try:
            return watcher.run_cycle(
                capture.run,
                lambda: cmd_ingest(argparse.Namespace(then_run=False)),
                process_pending,
            )
        except Exception as exc:
            print(f"Drive watcher capture failed: {exc}", file=sys.stderr)
            from pipeline import alert

            alert.send(["drive_watcher"], detail=str(exc))
            return watcher.CycleResult(0, 0, processed=False, failed=True)

    try:
        with watcher.WatchLease(DB_DIR / "drive-watcher.lock"):
            if args.catch_up:
                print("Drive watcher catch-up: processing existing pending recordings.")
                if process_pending():
                    return 1
            if args.once:
                result = cycle()
                print(
                    f"Drive watcher: {'failed' if result.failed else 'processed' if result.processed else 'waiting'}; "
                    f"scanned {result.scanned}, downloaded {result.downloaded}."
                )
                return 1 if result.failed else 0
            print(
                f"Drive watcher running every {args.interval_sec:g}s. "
                "New Drive audio is processed with Replicate as soon as it is captured."
            )
            return watcher.watch(cycle, interval_sec=args.interval_sec)
    except watcher.WatcherAlreadyRunning as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_query(args: argparse.Namespace) -> int:
    from pipeline import answer

    try:
        result = answer.ask(args.question, mode=args.mode, top_k=args.top_k)
    except index.IndexError_ as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    print(result.text)
    if args.timing:
        print(f"\n{result.timing_line()}")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Open the local meeting-memory control room."""
    from pipeline import dashboard, dashboard_auth

    try:
        dashboard.run(host=args.host, port=args.port, open_browser=args.open)
    except dashboard_auth.AuthError as exc:
        # A misconfiguration, not a crash: print the fix, not a traceback.
        print(exc, file=sys.stderr)
        print(
            f"\nSuggested token: MMC_DASHBOARD_TOKEN={dashboard_auth.generate_token()}",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_people(args: argparse.Namespace) -> int:
    """Inspect and curate the people registry.

    Curating this is worth the effort: every spelling variant becomes a separate
    node in the knowledge graph, so one person recorded three ways is three
    disconnected entities that no query finds together.
    """
    db.init_db()
    if getattr(args, "repair_merges", False):
        apply_value = getattr(args, "apply", False)
        expected_digest = (getattr(args, "expected_digest", None) or "").strip()
        if isinstance(apply_value, str):
            if not expected_digest:
                print(
                    "--repair-merges --apply requires --expected-digest.",
                    file=sys.stderr,
                )
                return 2
            try:
                result = people_merge.apply_legacy_repair(
                    Path(apply_value), expected_digest=expected_digest
                )
            except (ValueError, people_merge.PreviewDriftError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(
                f"Legacy merge repair {result.digest}: "
                f"{result.aliases_deleted} alias(es) retired, "
                f"{result.suggestions_rewritten} suggestion(s) rewritten, "
                f"{result.suggestions_cleared} cleared, "
                f"{result.minutes_rewritten} minutes rewritten, "
                f"{result.minutes_unchanged} unchanged, "
                f"{result.minutes_missing} missing, "
                f"{result.rewrite_conflicts} conflicting, "
                f"{result.pending_rewrites} pending."
            )
            if result.already_applied:
                print("This exact repair was already applied; only resumable jobs were checked.")
            return 1 if result.rewrite_conflicts or result.pending_rewrites else 0

        if apply_value is True:
            print(
                "--repair-merges --apply requires the preview artifact path.",
                file=sys.stderr,
            )
            return 2
        preview_to = getattr(args, "preview_to", None)
        if not preview_to:
            print(
                "--repair-merges preview requires --preview-to PATH.",
                file=sys.stderr,
            )
            return 2
        proposed = people_merge.preview_legacy_repair(
            excluded_aliases=getattr(args, "exclude_alias", ()) or ()
        )
        written = people_merge.write_legacy_repair_preview(
            proposed, Path(preview_to)
        )
        print("Legacy merge repair preview:")
        for alias, target in proposed.mappings:
            print(f"  {alias!r} -> {target!r}")
        print(
            f"  {len(proposed.mappings)} candidate alias(es), "
            f"{len(proposed.excluded_aliases)} excluded, "
            f"{len(proposed.suggestion_updates)} suggestion rewrite(s), "
            f"{len(proposed.proposed_clears)} proposed clear(s), "
            f"{proposed.files_changed} file(s), "
            f"{proposed.literal_matches} literal match(es), "
            f"{len(proposed.missing_files)} missing, "
            f"{len(proposed.conflicts)} warning(s).\n"
            f"Preview: {written}\n"
            f"Digest: {proposed.digest}"
        )
        print("Nothing was changed.")
        return 0

    if getattr(args, "resume_merge_rewrites", False):
        result = people_merge.resume_pending_rewrites()
        print(
            "Merge rewrites: "
            f"{result.minutes_rewritten} applied, "
            f"{result.minutes_unchanged} unchanged, "
            f"{result.minutes_missing} missing, "
            f"{result.rewrite_conflicts} conflicting, "
            f"{result.pending_rewrites} pending."
        )
        return 1 if result.rewrite_conflicts or result.pending_rewrites else 0

    if args.merge:
        source, target = args.merge
        proposed = people_merge.preview([source], target)
        print(
            f"Merge preview: {source!r} -> {proposed.actual_target!r}\n"
            f"  {proposed.speaker_rows} speaker row(s), "
            f"{proposed.affected_meetings} meeting(s), "
            f"{proposed.files_changed} file(s), "
            f"{proposed.literal_matches} literal match(es)\n"
            f"  missing: {len(proposed.missing_files)}, "
            f"warnings: {len(proposed.conflicts)}\n"
            f"Digest: {proposed.digest}"
        )
        apply_value = getattr(args, "apply", False)
        if not apply_value:
            print("Nothing was changed.")
            return 0
        if isinstance(apply_value, str):
            print(
                "ordinary --merge uses --apply without a path.", file=sys.stderr
            )
            return 2
        expected_digest = (getattr(args, "expected_digest", None) or "").strip()
        if not expected_digest:
            print("--apply requires --expected-digest from the preview.", file=sys.stderr)
            return 2
        try:
            result = people_merge.merge(
                [source], target, expected_digest=expected_digest
            )
        except people_merge.PreviewDriftError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(
            f"Merged {source!r} into {result.target!r}: "
            f"{result.speaker_rows} speaker row(s), "
            f"{result.minutes_rewritten} minutes rewritten, "
            f"{result.minutes_unchanged} unchanged, "
            f"{result.minutes_missing} missing, "
            f"{result.rewrite_conflicts} conflicting, "
            f"{result.pending_rewrites} pending."
        )
        return 1 if result.rewrite_conflicts or result.pending_rewrites else 0

    with db.connect() as conn:
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


def cmd_voice(args: argparse.Namespace) -> int:
    """Re-score the voice queue and apply what the matcher is confident about.

    Two operations, deliberately separate. `--rematch` only re-bands and
    re-clusters: it never touches a name, so it is safe to run any time. Naming
    is `--apply-auto`, which is a dry run unless `--apply` is given, because the
    first real invocation against a mature corpus requeues dozens of meetings
    for a minutes recompile and a reindex.
    """
    from datetime import datetime

    from pipeline import backup
    from pipeline.config import TZ

    db.init_db()
    if not (args.rematch or args.apply_auto or args.embed):
        args.apply_auto = True
    # Committing a band nobody just computed is how a stale band becomes a name.
    # The preview stays write-free, so only the committing path re-bands first.
    if args.apply:
        args.rematch = True

    with db.connect() as conn:
        try:
            namespace = voices.active_namespace(conn)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"namespace: {namespace}")

    if args.embed:
        from pipeline import voice_embed

        if not voice_embed.configured():
            print(
                "no embedding provider configured; set MMC_REMOTE_VOICE_MODEL. "
                "This is the only paid call in the voice path, so it has no default.",
                file=sys.stderr,
            )
            return 2
        from pipeline.voice_embed_replicate import ReplicateVoiceBackend

        embedded = voice_embed.run(
            backend=ReplicateVoiceBackend(),
            namespace=namespace,
            meeting_id=args.meeting,
            limit=args.limit,
            force=args.force,
        )
        print(
            f"embed: {embedded.meetings} meeting(s), {embedded.embedded} labels embedded, "
            f"{embedded.bootstrapped} bootstrapped, {embedded.skipped} skipped"
        )
        for failure in embedded.failures[:10]:
            print(f"  FAILED {failure}")

    with db.connect() as conn:
        if args.rematch:
            promoted = voices.rematch_pending(conn, namespace)
            clusters = voices.cluster_pending(conn, namespace)
            print(f"rematch: {promoted} promoted to auto, {clusters} review cluster(s)")

        if not args.apply_auto:
            return 0

        preview = voices.apply_auto(
            conn, namespace=namespace, dry_run=True, limit=args.limit
        )
        _print_auto_apply(preview, committed=False)
        if not args.apply:
            print()
            print("Dry run. Re-run with --apply to commit.")
            return 0

    stamp = f"{datetime.now(TZ):%Y%m%d-%H%M%S}"
    destination = DB_PATH.parent / "backups" / f"manifest-{stamp}-pre-auto-apply.db"
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup.backup_sqlite(DB_PATH, destination)
    print()
    print(f"manifest backed up to {destination}")

    with db.connect() as conn:
        result = voices.apply_auto(
            conn, namespace=namespace, dry_run=False, limit=args.limit
        )
        voices.cluster_pending(conn, namespace)
    _print_auto_apply(result, committed=True)
    if result.meetings_requeued:
        print(f"Run `pipeline run` to recompile {result.meetings_requeued} meeting(s).")
    return 0


def _print_auto_apply(result: voices.AutoApplyResult, *, committed: bool) -> None:
    verb = "applied" if committed else "would apply"
    print(
        f"{verb} {len(result.applied)}, skipped {len(result.skipped)}, "
        f"demoted {len(result.demoted)}; "
        f"{result.meetings_requeued} meeting(s) requeued for minutes + reindex"
    )
    names = Counter(canonical for _, _, canonical in result.applied)
    if names:
        print("  names: " + ", ".join(f"{n} x{c}" for n, c in names.most_common(8)))
    for meeting_id, label, reason in result.skipped[:10]:
        print(f"  skip  {meeting_id[:12]} {label}: {reason}")
    for meeting_id, label, reason in result.demoted[:10]:
        print(f"  review {meeting_id[:12]} {label}: {reason}")


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
        description="Compile meeting audio into minutes and bounded graph context.",
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
    p_minutes.add_argument(
        "--force", action="store_true",
        help="compile even a meeting under the junk-recording floor "
             "(MMC_MIN_MEETING_SEC / MMC_MIN_TRANSCRIPT_WORDS)",
    )
    p_minutes.set_defaults(func=cmd_minutes)

    p_export = subparsers.add_parser(
        "export", help="sync/export professional minutes to Product Manager repo"
    )
    p_export.set_defaults(func=cmd_export)

    p_index = subparsers.add_parser(
        "index", help="legacy document repair path (not used by the normal batch)"
    )
    add_common(p_index)
    p_index.set_defaults(func=cmd_index)

    p_index_preview = subparsers.add_parser(
        "index-repair-preview",
        help="write a read-only plan for reconciling pending LightRAG documents",
    )
    add_common(p_index_preview)
    p_index_preview.add_argument(
        "--to",
        required=True,
        help="new private JSON artifact path; existing files are never overwritten",
    )
    p_index_preview.set_defaults(func=cmd_index_repair_preview)

    p_graph = subparsers.add_parser(
        "graph-sync", help="author the LightRAG graph from the manifest's entities"
    )
    p_graph.set_defaults(func=cmd_graph_sync)

    p_chunk = subparsers.add_parser(
        "chunk-index", help="build the local BM25 index over compiled minutes"
    )
    p_chunk.add_argument(
        "--rebuild",
        action="store_true",
        help="rewrite chunks even where content hashes are unchanged",
    )
    p_chunk.add_argument("--meeting", help="index one meeting id instead of the whole corpus")
    p_chunk.set_defaults(func=cmd_chunk_index)

    p_run = subparsers.add_parser("run", help="every pending stage, in order")
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--owner", default=OWNER_NAME or None)
    p_run.add_argument("--no-llm", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_watch = subparsers.add_parser(
        "watch", help="continuously process new private Drive recordings (Replicate only)"
    )
    p_watch.add_argument(
        "--interval-sec", type=float, default=60.0,
        help="Drive polling interval in seconds (default: 60)",
    )
    p_watch.add_argument("--limit", type=int, default=None)
    p_watch.add_argument("--owner", default=OWNER_NAME or None)
    p_watch.add_argument(
        "--catch-up", action="store_true",
        help="also process recordings already pending when the watcher starts",
    )
    p_watch.add_argument("--once", action="store_true", help="run one capture poll and exit")
    p_watch.set_defaults(func=cmd_watch)

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
        help="preview folding one person into another",
    )
    p_people.add_argument(
        "--apply", nargs="?", const=True, metavar="PREVIEW_PATH",
        help="apply --merge, or apply a legacy repair preview artifact path",
    )
    p_people.add_argument(
        "--expected-digest",
        help="SHA-256 printed by the exact merge preview to apply",
    )
    p_people.add_argument(
        "--resume-merge-rewrites", action="store_true",
        help="resume hash-checked minutes rewrites left by an interrupted merge",
    )
    p_people.add_argument(
        "--repair-merges", action="store_true",
        help="preview or apply the one-time digest-bound legacy merge repair",
    )
    p_people.add_argument(
        "--preview-to",
        help="private JSON path for a non-mutating legacy repair preview",
    )
    p_people.add_argument(
        "--exclude-alias", action="append", default=[],
        help="exclude one legacy alias key from the repair preview (repeatable)",
    )
    p_people.set_defaults(func=cmd_people)

    p_entities = subparsers.add_parser("entities", help="most-mentioned entities")
    p_entities.add_argument("--limit", type=int, default=50)
    p_entities.set_defaults(func=cmd_entities)

    p_voice = subparsers.add_parser(
        "voice", help="re-score the voice queue and apply confident matches"
    )
    p_voice.add_argument(
        "--rematch", action="store_true",
        help="re-score and re-cluster pending labels; never names anyone",
    )
    p_voice.add_argument(
        "--apply-auto", action="store_true",
        help="write auto-banded matches into the speakers table (the default)",
    )
    p_voice.add_argument(
        "--apply", action="store_true",
        help="commit --apply-auto instead of previewing; implies --rematch, so a "
             "stale band is never committed as a name",
    )
    p_voice.add_argument(
        "--embed", action="store_true",
        help="produce voice vectors for meetings that have none (the only paid call)",
    )
    p_voice.add_argument("--meeting", help="restrict --embed to one meeting id")
    p_voice.add_argument(
        "--force", action="store_true", help="re-embed labels that already have a vector",
    )
    p_voice.add_argument("--limit", type=int, help="cap the rows or meetings considered")
    p_voice.set_defaults(func=cmd_voice)

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

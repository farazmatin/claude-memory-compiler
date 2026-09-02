"""Is a candidate encoder comparable with the vectors already in the corpus?

The corpus holds 268 vectors under `pyannote/wespeaker-voxceleb-resnet34-LM` and
23 enrolled people built from them. If a new encoder produces vectors that live
in the same space, those people carry over and nothing is lost. If it does not,
every voiceprint restarts from whatever gets re-embedded.

That is a one-number decision and it costs a few meetings' embedding to answer,
so answer it before backfilling anything.

## Method

For each sampled label the probe embeds the same audio with the candidate and
cosines the result against that label's ALREADY STORED vector - self-similarity.

It also cosines each new vector against a *different* person's stored vector -
cross-similarity. That control is the part that makes the answer mean anything:
if every vector in a space is broadly similar to every other, high
self-similarity proves nothing. Comparability requires self to be high AND the
gap over cross to be wide.

## Usage

    ./.venv/Scripts/python.exe scripts/probe_voice_comparability.py owner/model:version

Read-only: the manifest is opened `mode=ro` and nothing is written. It does spend
money - one embedding call per sampled meeting.
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import asr, db, voice_embed, voices
from pipeline.config import DB_PATH

STORED_NAMESPACE = "pyannote/wespeaker-voxceleb-resnet34-LM"


def sample(conn, namespace: str, meetings: int) -> dict[str, list[sqlite3.Row]]:
    """Labels that already have a stored vector, grouped by meeting.

    Prefers meetings with several such labels, so one embedding call yields
    several comparisons and the bill stays small.
    """
    rows = conn.execute(
        """
        SELECT meeting_id, label, embedding, dim, resolved_as, best_canonical
        FROM speaker_matches
        WHERE embedding IS NOT NULL AND model = ?
        ORDER BY speech_sec DESC
        """,
        (namespace,),
    ).fetchall()

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["meeting_id"], []).append(row)
    ranked = sorted(grouped.items(), key=lambda kv: -len(kv[1]))
    return dict(ranked[:meetings])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model", help="candidate encoder, pinned as owner/name:version")
    parser.add_argument("--meetings", type=int, default=4, help="how many meetings to embed")
    parser.add_argument(
        "--stored-namespace", default=STORED_NAMESPACE, help="namespace to compare against"
    )
    args = parser.parse_args()

    from pipeline.voice_embed_replicate import ReplicateVoiceBackend

    backend = ReplicateVoiceBackend(args.model)

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    batches = sample(conn, args.stored_namespace, args.meetings)
    if not batches:
        print(f"no stored vectors under {args.stored_namespace}", file=sys.stderr)
        return 2

    print(f"candidate : {args.model}")
    print(f"stored    : {args.stored_namespace}")
    print(f"sampling  : {sum(len(v) for v in batches.values())} labels "
          f"across {len(batches)} meetings\n")

    fresh: dict[tuple[str, str], object] = {}
    stored: dict[tuple[str, str], object] = {}

    for meeting_id, labels in batches.items():
        meeting = db.get_meeting(conn, meeting_id)
        if not meeting:
            continue
        try:
            transcript = asr.load_transcript(meeting_id)
            audio = voice_embed.audio_for(meeting)
        except Exception as exc:
            print(f"  skip {meeting_id[:12]}: {exc}")
            continue

        regions = [
            voice_embed.LabelRegion(row["label"], start, end)
            for row in labels
            for start, end in voice_embed.capped_regions(
                voice_embed.label_regions(transcript, row["label"])
            )
        ]
        if not regions:
            print(f"  skip {meeting_id[:12]}: no speech regions")
            continue

        try:
            response = backend.embed(audio, regions, encoder=args.model)
        except Exception as exc:
            print(f"  FAILED {meeting_id[:12]}: {exc}")
            continue

        for row in labels:
            vector = response.embeddings.get(row["label"])
            if not vector:
                continue
            key = (meeting_id, row["label"])
            fresh[key] = vector
            stored[key] = voices.unpack(row["embedding"], row["dim"])
        print(f"  embedded {meeting_id[:12]} ({len(labels)} labels)")

    if not fresh:
        print("\nnothing embedded; cannot decide", file=sys.stderr)
        return 1

    self_scores = [voices.cosine(fresh[k], stored[k]) for k in fresh]
    cross_scores = [
        voices.cosine(fresh[a], stored[b])
        for a in fresh
        for b in stored
        if a != b
    ]

    self_median = statistics.median(self_scores)
    cross_median = statistics.median(cross_scores) if cross_scores else 0.0
    gap = self_median - cross_median

    print(f"\nself-similarity  (same label, both encoders) : median {self_median:.3f}"
          f"  min {min(self_scores):.3f}  n={len(self_scores)}")
    print(f"cross-similarity (different labels)          : median {cross_median:.3f}"
          f"  n={len(cross_scores)}")
    print(f"separation                                    : {gap:.3f}")

    print()
    if self_median >= 0.80 and gap >= 0.30:
        print("COMPARABLE. The stored vectors can be aliased into this namespace:")
        print("  the 23 enrolled people carry over and no re-enrollment is needed.")
    elif self_median >= 0.50 and gap >= 0.20:
        print("PARTIALLY COMPARABLE. Related but not interchangeable. Re-embed rather")
        print("  than alias, and expect voiceprints to need rebuilding from confirmations.")
    else:
        print("NOT COMPARABLE. Treat this as a fresh namespace: enrollment restarts,")
        print("  and the historical vectors stay for audit only.")
    print("\nNothing was written. The decision is yours to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

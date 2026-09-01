# Testing

Run the narrowest relevant checks first.

    uv run pytest tests/test_replicate_asr.py tests/test_doctor.py tests/test_cli_invariants.py -q
    uv run ruff check .

For a broader check:

    uv run pytest

## Important coverage

- Replicate is the sole backend constructor.
- A missing Replicate token fails before transcription.
- The pipeline contains no voice-enrollment stage.
- Clustering one voice namespace never deletes another's clusters.
- An over-segmented meeting never yields an auto-applied name.
- Auto-apply never downgrades a label a human confirmed.
- Auto-apply is a dry run unless `--apply` is given.
- Drive capture dry-run remains non-mutating.
- Graph context remains bounded and provenance-bearing.

Runtime quality still requires review of a real meeting: transcription accuracy,
speaker attribution, minutes quality, and graph usefulness cannot be proven by
unit tests alone.

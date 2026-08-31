# Testing

Run the narrowest relevant checks first.

    uv run pytest tests/test_replicate_asr.py tests/test_doctor.py tests/test_cli_invariants.py -q
    uv run pytest tests/test_voice_embed.py tests/test_voices.py tests/test_replicate_voice.py -q
    uv run ruff check .

For a broader check:

    uv run pytest

## Important coverage

- Replicate is the sole backend constructor.
- A missing Replicate token fails before transcription.
- No speaker-embedding weights load locally; enrollment is remote-only.
- The voice stage stays out of `run`/`watch` until it is explicitly switched on.
- An auto-applied voice match is `inferred`, never `confirmed`.
- Vectors from one encoder never match a voiceprint built by another.
- Drive capture dry-run remains non-mutating.
- Graph context remains bounded and provenance-bearing.

Runtime quality still requires review of a real meeting: transcription accuracy,
speaker attribution, minutes quality, and graph usefulness cannot be proven by
unit tests alone.

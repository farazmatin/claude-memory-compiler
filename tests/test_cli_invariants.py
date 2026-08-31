"""Structural invariants in cli.py that comments alone cannot protect.

These assert on source rather than behaviour when a retired capability must stay
absent from the executable pipeline.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

from pipeline import cli


def _statement_order(func, *needles: str) -> list[int]:
    """First source line of each needle inside func, comments excluded.

    Parsed rather than grepped: a comment mentioning the call reads identically to
    the call itself, which is exactly the trap this guards against.
    """
    source = inspect.getsource(func)
    tree = ast.parse(textwrap.dedent(source))
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        rendered = ast.unparse(node.func)
        for needle in needles:
            if needle in rendered and needle not in found:
                found[needle] = node.lineno
    missing = [n for n in needles if n not in found]
    assert not missing, f"{func.__name__} no longer calls: {missing}"
    return [found[n] for n in needles]


def test_transcribe_has_no_voice_enrollment_path():
    source = inspect.getsource(cli.cmd_transcribe)
    assert "enroll" not in source
    assert "voice embedding" not in source


def test_no_model_weights_are_loaded_anywhere_in_the_pipeline():
    """The provider policy, asserted rather than remembered.

    Every model this product runs - transcription, alignment, diarization, and
    now speaker embedding - runs remotely. The rule survives only if importing
    the machinery back in is an error rather than a review comment, because the
    convenient way to add a stage is always to pip install the model.
    """
    banned = ("torch", "whisperx", "pyannote", "speechbrain", "nemo")
    offenders: list[str] = []

    for path in sorted(Path(cli.__file__).parent.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0].lower() in banned:
                    offenders.append(f"{path.name}:{node.lineno} imports {name}")

    assert not offenders, "model weights must never be loaded locally: " + "; ".join(offenders)


def test_the_voice_stage_is_not_in_run_by_default():
    """Explicit-first rollout. `run`/`watch` may include the voice stage only
    behind a stored setting, so enabling a new paid provider is a decision
    somebody makes rather than a consequence of upgrading."""
    source = inspect.getsource(cli._run_all)
    assert "_voice_stage_enabled(args)" in source
    gate = inspect.getsource(cli._voice_stage_enabled)
    assert "VOICE_STAGE_IN_RUN" in gate
    assert cli.VOICE_STAGE_IN_RUN == "voice.stage_in_run"
    assert "no_voice" in gate


def test_people_merge_uses_the_single_preview_bound_workflow():
    """CLI adapters must not recreate database/voice merge ordering."""
    preview_line, merge_line = _statement_order(
        cli.cmd_people, "people_merge.preview", "people_merge.merge"
    )
    assert preview_line < merge_line
    source = inspect.getsource(cli.cmd_people)
    assert "db.merge_person" not in source
    assert "voices.merge_people" not in source

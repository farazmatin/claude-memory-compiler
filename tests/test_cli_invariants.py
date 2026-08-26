"""Structural invariants in cli.py that comments alone cannot protect.

These assert on source order rather than behaviour, which is unusual - but the
property is a source-order property, and getting it wrong is silent: the pipeline
keeps working and simply never accumulates voiceprints.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

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


def test_transcribe_embeds_voices_before_releasing_the_audio():
    """The embed must precede the cleanup that deletes the waveform.

    cleanup_transcribed_audio removes local audio for any meeting with a
    drive_sources row - every Drive-captured meeting, i.e. the normal path. Run the
    embed afterwards and there is nothing left to embed, so the enrollable set can
    never grow past whatever backlog happens to still have audio on disk. Nothing
    fails loudly if these are swapped; voice recognition just never improves.
    """
    embed_line, cleanup_line = _statement_order(
        cli.cmd_transcribe, "enroll.enroll_meeting", "capture.cleanup_transcribed_audio"
    )
    assert embed_line < cleanup_line, (
        "enroll.enroll_meeting must come BEFORE capture.cleanup_transcribed_audio "
        f"in cmd_transcribe (found {embed_line} vs {cleanup_line})"
    )


def test_people_merge_uses_the_single_preview_bound_workflow():
    """CLI adapters must not recreate database/voice merge ordering."""
    preview_line, merge_line = _statement_order(
        cli.cmd_people, "people_merge.preview", "people_merge.merge"
    )
    assert preview_line < merge_line
    source = inspect.getsource(cli.cmd_people)
    assert "db.merge_person" not in source
    assert "voices.merge_people" not in source

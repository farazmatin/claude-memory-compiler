"""Exercise shipped dashboard controls through Node without a browser."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SORT_HARNESS = REPO_ROOT / "tests" / "js" / "check_controls.mjs"
MERGE_HARNESS = REPO_ROOT / "tests" / "js" / "check_merge_controls.mjs"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)


def test_app_js_parses():
    result = subprocess.run(
        ["node", "--check", str(REPO_ROOT / "pipeline" / "static" / "app.js")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_merge_controls_execute_real_shipped_functions():
    result = subprocess.run(
        ["node", str(MERGE_HARNESS)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "checks passed" in result.stdout


def test_sort_and_filter_logic_holds_for_clusters_and_one_offs():
    result = subprocess.run(
        ["node", str(SORT_HARNESS)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "checks passed" in result.stdout

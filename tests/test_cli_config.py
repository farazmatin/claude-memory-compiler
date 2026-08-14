"""The setup-facing CLI: `pipeline config` and `pipeline reindex`.

Both exist to make an unattended machine settable-up in one command. `config` is
the only writer of `.env`, so it carries the promise that no secret is ever
printed; `reindex` is the one step of the Postgres migration that can silently do
nothing, which is exactly the failure it has to avoid.
"""

from __future__ import annotations

import argparse

import pytest

from pipeline import cli, db, env
from tests.conftest import make_meeting

# ── config ────────────────────────────────────────────────────────────

@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(env, "ENV_FILE", path)
    example = tmp_path / ".env.example"
    example.write_text(
        "# template\nMMC_LIGHTRAG_API_KEY=\nPOSTGRES_PASSWORD=\nHF_TOKEN=\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(env, "ENV_EXAMPLE_FILE", example)
    for key in (*env.REQUIRED_SECRETS, *env.MANUAL_SECRETS):
        monkeypatch.delenv(key, raising=False)
    return path


def test_config_init_generates_the_local_secrets(env_file, capsys):
    assert cli.cmd_config(argparse.Namespace(config_command="init")) == 0

    values = env.read_values(env_file)
    assert values["MMC_LIGHTRAG_API_KEY"] and values["POSTGRES_PASSWORD"]
    # HF_TOKEN cannot be generated - it needs an account and two accepted licences.
    assert not values["HF_TOKEN"]

    printed = capsys.readouterr().out
    assert values["POSTGRES_PASSWORD"] not in printed, "a generated secret must not be echoed"


def test_config_init_is_safe_to_re_run(env_file):
    cli.cmd_config(argparse.Namespace(config_command="init"))
    first = env.read_values(env_file)
    cli.cmd_config(argparse.Namespace(config_command="init"))

    assert env.read_values(env_file) == first, (
        "re-running setup must not rotate the password the database already expects"
    )


def test_config_set_reads_the_value_from_stdin(env_file, monkeypatch, capsys):
    """Never an argument: arguments land in shell history and in `ps`."""
    monkeypatch.setattr("sys.stdin", _Stdin("  hf_token_value\n"))

    assert cli.cmd_config(argparse.Namespace(config_command="set", key="HF_TOKEN")) == 0
    assert env.read_values(env_file)["HF_TOKEN"] == "hf_token_value"
    assert "hf_token_value" not in capsys.readouterr().out


def test_config_set_refuses_an_empty_value(env_file, monkeypatch):
    monkeypatch.setattr("sys.stdin", _Stdin("   \n"))
    assert cli.cmd_config(argparse.Namespace(config_command="set", key="HF_TOKEN")) == 1
    assert env.status("HF_TOKEN", env_file) == env.MISSING


def test_config_show_exits_non_zero_while_anything_is_missing(env_file):
    assert cli.cmd_config(argparse.Namespace(config_command="show", key=None)) == 1
    cli.cmd_config(argparse.Namespace(config_command="init"))
    env.set_value("HF_TOKEN", "token", env_file)
    assert cli.cmd_config(argparse.Namespace(config_command="show", key=None)) == 0


def test_config_show_single_key_answers_in_the_exit_code(env_file, capsys):
    """This is how the PowerShell setup asks, instead of parsing .env itself."""
    assert cli.cmd_config(argparse.Namespace(config_command="show", key="HF_TOKEN")) == 1
    env.set_value("HF_TOKEN", "token", env_file)
    assert cli.cmd_config(argparse.Namespace(config_command="show", key="HF_TOKEN")) == 0
    assert "token" not in capsys.readouterr().out.replace("HF_TOKEN", "")


class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


# ── reindex ───────────────────────────────────────────────────────────

def test_reindex_clears_the_recorded_document_id(manifest, monkeypatch):
    """The new store has never seen the old ids.

    Left in place, the index stage tries to delete a document that was never
    there, decides it cannot safely replace it, and skips the meeting - so the
    migration silently indexes nothing.
    """
    make_meeting(manifest, "m1", "2026-08-10", status=db.INDEXED, lightrag_doc_id="doc-old")
    monkeypatch.setattr(cli.db, "connect", _fixed_connection(manifest))
    monkeypatch.setattr(cli.db, "init_db", lambda *a, **k: None)

    assert cli.cmd_reindex(argparse.Namespace(limit=None, queue_only=True)) == 0

    meeting = db.get_meeting(manifest, "m1")
    assert meeting.status == db.MINUTES_COMPILED
    assert meeting.lightrag_doc_id is None


def test_reindex_then_indexes_unless_told_not_to(manifest, monkeypatch):
    make_meeting(manifest, "m1", "2026-08-10", status=db.INDEXED, lightrag_doc_id="doc-old")
    monkeypatch.setattr(cli.db, "connect", _fixed_connection(manifest))
    monkeypatch.setattr(cli.db, "init_db", lambda *a, **k: None)

    indexed: list[object] = []
    monkeypatch.setattr(cli, "cmd_index", lambda args: indexed.append(args) or 0)

    assert cli.cmd_reindex(argparse.Namespace(limit=None, queue_only=False)) == 0
    assert len(indexed) == 1, "reindex without --queue-only should push straight away"


def test_reindex_with_nothing_indexed_is_a_no_op(manifest, monkeypatch):
    monkeypatch.setattr(cli.db, "connect", _fixed_connection(manifest))
    monkeypatch.setattr(cli.db, "init_db", lambda *a, **k: None)

    called: list[object] = []
    monkeypatch.setattr(cli, "cmd_index", lambda args: called.append(args) or 0)

    assert cli.cmd_reindex(argparse.Namespace(limit=None, queue_only=False)) == 0
    assert called == []


def _fixed_connection(conn):
    """Hand `cli` the test's open connection wherever it asks for one."""
    import contextlib

    @contextlib.contextmanager
    def connect(*_args, **_kwargs):
        yield conn

    return connect

"""The `.env` file: parsing, loading and editing.

These exist because a setting that reaches `docker compose` and not the pipeline
fails in the worst way available - silently, at runtime, weeks later. The parser
therefore has to agree with compose's, and `set_value` has to leave a file that
still means what it meant before it was edited.
"""

from __future__ import annotations

import os

from pipeline import env

# ── Parsing ───────────────────────────────────────────────────────────

def test_parse_handles_the_shapes_compose_accepts():
    values = env.parse(
        "\n".join([
            "# a comment",
            "",
            "PLAIN=value",
            "export EXPORTED=value",
            "  SPACED = spaced ",
            'DOUBLE="two words"',
            "SINGLE='two words'",
            "TRAILING=value # not part of it",
            "HASHED=pass#word",
            "EMPTY=",
            "NOT_AN_ASSIGNMENT",
        ])
    )
    assert values == {
        "PLAIN": "value",
        "EXPORTED": "value",
        "SPACED": "spaced",
        "DOUBLE": "two words",
        "SINGLE": "two words",
        "TRAILING": "value",
        # No leading space before the '#', so it is part of the password. Getting
        # this wrong truncates a generated secret to something that still looks
        # plausible in the file.
        "HASHED": "pass#word",
        "EMPTY": "",
    }
    assert "NOT_AN_ASSIGNMENT" not in values


def test_parse_ignores_comment_lines_that_look_like_assignments():
    assert env.parse("# HF_TOKEN=leftover\nHF_TOKEN=real") == {"HF_TOKEN": "real"}


# ── Loading ───────────────────────────────────────────────────────────

def test_load_populates_the_environment(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("MMC_TEST_ONE=from-file\n", encoding="utf-8")
    monkeypatch.delenv("MMC_TEST_ONE", raising=False)

    assert env.load(path) is True
    assert os.environ["MMC_TEST_ONE"] == "from-file"


def test_a_real_environment_variable_wins(tmp_path, monkeypatch):
    """The file is the default, not the authority.

    A scheduled task or a one-off shell export has to be able to override a value
    without editing the file the rest of the machine shares.
    """
    path = tmp_path / ".env"
    path.write_text("MMC_TEST_TWO=from-file\n", encoding="utf-8")
    monkeypatch.setenv("MMC_TEST_TWO", "from-environment")

    env.load(path)
    assert os.environ["MMC_TEST_TWO"] == "from-environment"


def test_load_without_a_file_is_not_an_error(tmp_path):
    assert env.load(tmp_path / "absent.env") is False


# ── Editing ───────────────────────────────────────────────────────────

def test_set_value_replaces_in_place_and_keeps_comments(tmp_path):
    """Appending a second assignment would leave the file with two answers."""
    path = tmp_path / ".env"
    path.write_text(
        "# keep me\nHF_TOKEN=\n# and me\nOTHER=untouched\n", encoding="utf-8"
    )

    env.set_value("HF_TOKEN", "token-value", path)

    text = path.read_text(encoding="utf-8")
    assert text.count("HF_TOKEN=") == 1
    assert "# keep me" in text and "# and me" in text
    assert env.read_values(path) == {"HF_TOKEN": "token-value", "OTHER": "untouched"}


def test_set_value_replaces_an_exported_assignment(tmp_path):
    path = tmp_path / ".env"
    path.write_text("export HF_TOKEN=old\n", encoding="utf-8")

    env.set_value("HF_TOKEN", "new", path)

    assert path.read_text(encoding="utf-8").count("HF_TOKEN") == 1
    assert env.read_values(path)["HF_TOKEN"] == "new"


def test_set_value_appends_a_key_that_is_not_there(tmp_path):
    path = tmp_path / ".env"
    path.write_text("OTHER=value\n", encoding="utf-8")

    env.set_value("MMC_OWNER_NAME", "Ada Lovelace", path)
    assert env.read_values(path)["MMC_OWNER_NAME"] == "Ada Lovelace"


def test_values_needing_quotes_survive_a_round_trip(tmp_path):
    """A name with a space, a quote or a trailing space must come back intact."""
    path = tmp_path / ".env"
    for value in ("Ada Lovelace", 'quote"inside', "back\\slash", "trailing ", "#leading"):
        env.set_value("MMC_OWNER_NAME", value, path)
        assert env.read_values(path)["MMC_OWNER_NAME"] == value


def test_single_quoted_values_are_literal(tmp_path):
    """Matching compose: no escape processing inside single quotes."""
    assert env.parse(r"KEY='back\slash'") == {"KEY": r"back\slash"}


def test_set_value_updates_the_running_process(tmp_path, monkeypatch):
    """Otherwise setup writes a token and the very next check reports it missing."""
    monkeypatch.delenv("MMC_TEST_THREE", raising=False)
    env.set_value("MMC_TEST_THREE", "now-set", tmp_path / ".env")
    assert os.environ["MMC_TEST_THREE"] == "now-set"


# ── Creating ──────────────────────────────────────────────────────────

def test_create_from_example_never_overwrites(tmp_path):
    """An existing .env holds the password the running database still expects."""
    path = tmp_path / ".env"
    path.write_text("POSTGRES_PASSWORD=already-in-use\n", encoding="utf-8")
    example = tmp_path / ".env.example"
    example.write_text("POSTGRES_PASSWORD=\n", encoding="utf-8")

    assert env.create_from_example(path, example) is False
    assert env.read_values(path)["POSTGRES_PASSWORD"] == "already-in-use"


def test_create_from_example_seeds_a_new_file(tmp_path):
    path = tmp_path / ".env"
    example = tmp_path / ".env.example"
    example.write_text("# template\nMMC_LIGHTRAG_API_KEY=\n", encoding="utf-8")

    assert env.create_from_example(path, example) is True
    assert "# template" in path.read_text(encoding="utf-8")


def test_fill_generated_secrets_only_fills_blanks(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "MMC_LIGHTRAG_API_KEY=\nPOSTGRES_PASSWORD=already-in-use\n", encoding="utf-8"
    )

    written = env.fill_generated_secrets(path)

    assert written == ["MMC_LIGHTRAG_API_KEY"]
    values = env.read_values(path)
    assert values["POSTGRES_PASSWORD"] == "already-in-use"
    assert len(values["MMC_LIGHTRAG_API_KEY"]) >= 32


def test_generated_secrets_are_url_safe():
    """The Postgres password ends up in a connection string."""
    secret = env.generate_secret()
    assert "/" not in secret and "+" not in secret and "=" not in secret


def test_fill_generated_secrets_is_idempotent(tmp_path):
    path = tmp_path / ".env"
    path.write_text("MMC_LIGHTRAG_API_KEY=\nPOSTGRES_PASSWORD=\n", encoding="utf-8")

    env.fill_generated_secrets(path)
    first = env.read_values(path)
    assert env.fill_generated_secrets(path) == []
    assert env.read_values(path) == first, "re-running setup must not rotate secrets"


# ── Status reporting ──────────────────────────────────────────────────

def test_status_reports_configured_without_the_value(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("HF_TOKEN=a-secret-value\n", encoding="utf-8")
    monkeypatch.delenv("HF_TOKEN", raising=False)

    state = env.status("HF_TOKEN", path)
    assert state == env.CONFIGURED
    assert "a-secret-value" not in state


def test_blank_and_whitespace_values_count_as_missing(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("HF_TOKEN=   \n", encoding="utf-8")
    monkeypatch.delenv("HF_TOKEN", raising=False)

    assert env.status("HF_TOKEN", path) == env.MISSING

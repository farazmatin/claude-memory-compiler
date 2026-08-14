"""Preflight checks.

These verify the checker reports accurately, including that it fails loudly on the
conditions that would otherwise degrade silently — a missing HF token being the
worst of them, because it costs every action item its owner.
"""

from __future__ import annotations

from pipeline import doctor


def test_run_never_raises_and_reports_status():
    """A broken individual check must not hide the others."""
    checks, ok = doctor.run()
    assert checks, "should produce checks even in a bare environment"
    assert isinstance(ok, bool)
    assert all(c.status in {doctor.OK, doctor.WARN, doctor.FAIL} for c in checks)


def test_a_broken_check_is_reported_not_fatal(monkeypatch):
    def exploding():
        raise RuntimeError("check itself is broken")

    monkeypatch.setattr(doctor, "ALL_CHECKS", (exploding, doctor.check_storage))
    checks, _ = doctor.run()

    assert any("errored" in c.detail for c in checks)
    assert any(c.name.startswith("dir ") for c in checks), "other checks still ran"


def test_missing_hf_token_fails_loudly(monkeypatch):
    """The most common silent degradation: no diarization means no action item
    owners, and the only signal is a printed warning mid-batch."""
    monkeypatch.setattr(doctor, "HF_TOKEN", None)
    monkeypatch.setattr(doctor, "ENABLE_DIARIZATION", True)

    checks = doctor.check_diarization()
    assert checks[0].status == doctor.FAIL
    assert "silently" in checks[0].detail
    assert "speaker-diarization-3.1" in checks[0].fix


def test_disabled_diarization_warns_about_owners(monkeypatch):
    monkeypatch.setattr(doctor, "ENABLE_DIARIZATION", False)
    checks = doctor.check_diarization()
    assert checks[0].status == doctor.WARN
    assert "owners" in checks[0].detail


def test_large_v3_on_cpu_is_flagged(monkeypatch):
    """Choosing large-v3 on CPU quietly makes the nightly batch impossible."""
    monkeypatch.setattr(doctor, "ASR_DEVICE", "cpu")
    monkeypatch.setattr(doctor, "ASR_MODEL", "large-v3")

    checks = doctor.check_asr()
    warnings = [c for c in checks if c.status == doctor.WARN]
    # Only reachable when whisperx imports; skip the assertion otherwise.
    try:
        import whisperx  # noqa: F401
    except ImportError:
        return
    assert warnings and "will not fit a night" in warnings[0].detail


def test_no_providers_is_a_failure(monkeypatch):
    """Without a provider, stages 3 and 4 cannot run at all."""
    from pipeline import llm

    class Absent:
        name = "fake"

        def available(self):
            return False

    monkeypatch.setattr(llm, "build_chain", lambda order=None: [Absent()])
    checks = doctor.check_providers()

    assert any(c.status == doctor.FAIL and c.name == "llm chain" for c in checks)


def test_one_available_provider_is_enough(monkeypatch):
    from pipeline import llm

    class Present:
        name = "gemini"

        def available(self):
            return True

    monkeypatch.setattr(llm, "build_chain", lambda order=None: [Present()])
    checks = doctor.check_providers()

    assert not any(c.status == doctor.FAIL for c in checks)
    assert any("will use gemini" in c.detail for c in checks)


def test_file_based_lightrag_storage_is_flagged(monkeypatch):
    """The JSON/NanoVectorDB defaults do not hold thousands of documents."""
    from pipeline import index

    monkeypatch.setattr(
        index, "health",
        lambda: {"status": "ok", "configuration": {"kv_storage": "JsonKVStorage"}},
    )
    monkeypatch.setattr(doctor, "LIGHTRAG_API_KEY", "set")

    checks = doctor.check_lightrag()
    assert any(
        c.name == "lightrag storage" and c.status == doctor.WARN for c in checks
    )


def test_postgres_storage_is_not_flagged(monkeypatch):
    from pipeline import index

    monkeypatch.setattr(
        index, "health",
        lambda: {"status": "ok", "configuration": {"kv_storage": "PGKVStorage"}},
    )
    monkeypatch.setattr(doctor, "LIGHTRAG_API_KEY", "set")

    checks = doctor.check_lightrag()
    assert not any(c.name == "lightrag storage" for c in checks)


def test_unreachable_lightrag_fails_with_the_fix(monkeypatch):
    from pipeline import index

    def boom():
        raise index.IndexError_("connection refused")

    monkeypatch.setattr(index, "health", boom)
    checks = doctor.check_lightrag()

    failure = next(c for c in checks if c.name == "lightrag")
    assert failure.status == doctor.FAIL
    assert "docker compose up" in failure.fix


def test_missing_api_key_warns(monkeypatch):
    monkeypatch.setattr(doctor, "LIGHTRAG_API_KEY", "")
    checks = doctor.check_lightrag()
    assert any(c.name == "lightrag auth" and c.status == doctor.WARN for c in checks)


def test_thin_glossary_is_flagged(monkeypatch, tmp_path):
    """A mangled product name fragments the knowledge graph, so this is cheap
    insurance worth nagging about."""
    from pipeline import asr

    monkeypatch.setattr(doctor, "GLOSSARY_FILE", tmp_path / "glossary.md")
    (tmp_path / "glossary.md").write_text("- OnlyOne\n", encoding="utf-8")
    monkeypatch.setattr(asr, "GLOSSARY_FILE", tmp_path / "glossary.md")

    checks = doctor.check_glossary()
    assert checks[0].status == doctor.WARN
    assert "product and people names" in checks[0].detail


def test_check_symbols_are_defined_for_every_status():
    for status in (doctor.OK, doctor.WARN, doctor.FAIL):
        assert doctor.Check("n", status, "d").symbol


def test_checks_serialize_for_the_setup_scripts():
    """`doctor --json` is the contract the PowerShell setup branches on."""
    payload = doctor.Check("drive auth", doctor.FAIL, "not authorized", "auth-drive").as_dict()
    assert payload == {
        "name": "drive auth",
        "status": doctor.FAIL,
        "detail": "not authorized",
        "fix": "auth-drive",
        "symbol": "FAIL",
    }


# ── Configuration ─────────────────────────────────────────────────────

def test_missing_required_secret_fails_and_says_where(tmp_path, monkeypatch):
    from pipeline import env

    monkeypatch.setattr(env, "ENV_FILE", tmp_path / ".env")
    for key in (*env.REQUIRED_SECRETS, *env.MANUAL_SECRETS):
        monkeypatch.delenv(key, raising=False)

    checks = doctor.check_env()

    env_file = next(c for c in checks if c.name == "env file")
    assert env_file.status == doctor.WARN
    assert str(tmp_path) in env_file.detail

    api_key = next(c for c in checks if c.name.endswith("MMC_LIGHTRAG_API_KEY"))
    assert api_key.status == doctor.FAIL
    assert api_key.detail.startswith(env.MISSING)
    assert "config init" in api_key.fix

    token = next(c for c in checks if c.name.endswith("HF_TOKEN"))
    assert token.status == doctor.WARN
    assert "config set HF_TOKEN" in token.fix


def test_configured_secrets_are_reported_without_their_values(tmp_path, monkeypatch):
    """A doctor report is meant to be safe to paste into a bug report."""
    from pipeline import env

    secret = "super-secret-value"
    path = tmp_path / ".env"
    path.write_text(
        f"MMC_LIGHTRAG_API_KEY={secret}\nPOSTGRES_PASSWORD={secret}\nHF_TOKEN={secret}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(env, "ENV_FILE", path)
    for key in (*env.REQUIRED_SECRETS, *env.MANUAL_SECRETS):
        monkeypatch.delenv(key, raising=False)

    checks = doctor.check_env()
    assert all(c.status == doctor.OK for c in checks)
    assert not any(secret in c.detail or secret in c.fix for c in checks)


# ── Postgres ──────────────────────────────────────────────────────────

def test_every_file_based_backend_is_named(monkeypatch):
    """All four stores have to move together; a half-migrated index is worse."""
    file_based = doctor._file_based_backends({
        "configuration": {
            "kv_storage": "PGKVStorage",
            "vector_storage": "NanoVectorDBStorage",
            "doc_status_storage": "JsonDocStatusStorage",
            "graph_storage": "PGTableGraphStorage",
        }
    })
    assert file_based == [
        "vector_storage=NanoVectorDBStorage",
        "doc_status_storage=JsonDocStatusStorage",
    ]


def test_backends_the_server_does_not_report_are_not_guessed_at():
    """An older LightRAG omitting a key is not evidence of a misconfiguration."""
    assert doctor._file_based_backends({"configuration": {"kv_storage": "PGKVStorage"}}) == []
    assert doctor._file_based_backends({}) == []


def test_postgres_storage_confirmed_when_every_store_is_pg(monkeypatch):
    from pipeline import index

    monkeypatch.setattr(index, "health", lambda: {"configuration": {
        "kv_storage": "PGKVStorage",
        "vector_storage": "PGVectorStorage",
        "doc_status_storage": "PGDocStatusStorage",
        "graph_storage": "PGTableGraphStorage",
    }})
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)

    checks = doctor.check_postgres()
    storage = next(c for c in checks if c.name == "postgres storage")
    assert storage.status == doctor.OK


def test_unreachable_lightrag_does_not_fail_postgres_twice(monkeypatch):
    """check_lightrag already reports the outage; two FAILs for one cause is noise."""
    from pipeline import index

    def boom():
        raise index.IndexError_("connection refused")

    monkeypatch.setattr(index, "health", boom)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)

    checks = doctor.check_postgres()
    assert all(c.status != doctor.FAIL for c in checks)


# ── Drive ─────────────────────────────────────────────────────────────

def test_unconfigured_drive_warns_rather_than_fails(monkeypatch):
    """Drive is optional: files can still be dropped into the inbox by hand."""
    monkeypatch.setattr(doctor, "DRIVE_FUTURE_FOLDER_ID", "")
    monkeypatch.setattr(doctor, "DRIVE_BACKFILL_FOLDER_ID", "")

    checks = doctor.check_drive()
    assert checks[0].status == doctor.WARN
    assert "inbox" in checks[0].detail


def test_configured_drive_without_authorization_fails(tmp_path, monkeypatch):
    """Unattended capture downloading nothing looks exactly like a quiet week."""
    client = tmp_path / "drive-client.json"
    client.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(doctor, "DRIVE_FUTURE_FOLDER_ID", "folder-id")
    monkeypatch.setattr(doctor, "DRIVE_CREDENTIALS_FILE", client)
    monkeypatch.setattr(doctor, "DRIVE_TOKEN_FILE", tmp_path / "absent-token.json")

    checks = doctor.check_drive()
    auth = next(c for c in checks if c.name == "drive auth")
    assert auth.status == doctor.FAIL
    assert "auth-drive" in auth.fix


def test_missing_oauth_client_points_at_the_path_it_wants(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "DRIVE_FUTURE_FOLDER_ID", "folder-id")
    monkeypatch.setattr(doctor, "DRIVE_CREDENTIALS_FILE", tmp_path / "drive-client.json")

    checks = doctor.check_drive()
    assert checks[0].status == doctor.FAIL
    assert "drive-client.json" in checks[0].detail


def test_revoked_drive_token_is_reported_as_a_failure(tmp_path, monkeypatch):
    from pipeline import capture

    client = tmp_path / "drive-client.json"
    client.write_text("{}", encoding="utf-8")
    token = tmp_path / "drive-token.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(doctor, "DRIVE_FUTURE_FOLDER_ID", "folder-id")
    monkeypatch.setattr(doctor, "DRIVE_CREDENTIALS_FILE", client)
    monkeypatch.setattr(doctor, "DRIVE_TOKEN_FILE", token)

    def revoked():
        raise capture.CaptureError("invalid_grant")

    monkeypatch.setattr(capture, "google_drive_client", revoked)

    checks = doctor.check_drive()
    auth = next(c for c in checks if c.name == "drive auth")
    assert auth.status == doctor.FAIL
    assert "auth-drive" in auth.fix


def test_working_drive_reports_what_it_can_see(tmp_path, monkeypatch):
    from pipeline import capture

    client = tmp_path / "drive-client.json"
    client.write_text("{}", encoding="utf-8")
    token = tmp_path / "drive-token.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(doctor, "DRIVE_FUTURE_FOLDER_ID", "folder-id")
    monkeypatch.setattr(doctor, "DRIVE_CREDENTIALS_FILE", client)
    monkeypatch.setattr(doctor, "DRIVE_TOKEN_FILE", token)

    class Listing:
        def list_files(self, folder_id):
            assert folder_id == "folder-id"
            return ["one", "two"]

    monkeypatch.setattr(capture, "google_drive_client", Listing)

    checks = doctor.check_drive()
    auth = next(c for c in checks if c.name == "drive auth")
    assert auth.status == doctor.OK
    assert "2 file(s)" in auth.detail


# ── Unattended operation ──────────────────────────────────────────────

def test_no_scheduled_batch_is_flagged(monkeypatch):
    """Everything else can be perfect and the corpus still stops growing."""
    class Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: Result())
    checks = doctor.check_nightly_task()

    assert checks[0].status == doctor.WARN
    assert "unattended" in checks[0].detail


def test_a_scheduled_batch_satisfies_the_check(monkeypatch):
    class Result:
        returncode = 0
        stdout = "0 1 * * * cd /srv/mmc && uv run pipeline run\n"

    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: Result())
    checks = doctor.check_nightly_task()
    assert checks[0].status == doctor.OK

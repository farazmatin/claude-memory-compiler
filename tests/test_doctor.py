"""Preflight checks.

These verify the checker reports accurately for remote transcription and the
remaining service dependencies.
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


def test_missing_replicate_token_fails_loudly(monkeypatch):
    monkeypatch.setattr(doctor, "REPLICATE_API_TOKEN", "")
    checks = doctor.check_asr()
    assert checks[0].status == doctor.FAIL
    assert "REPLICATE_API_TOKEN" in checks[0].detail


def test_diarization_waits_for_replicate(monkeypatch):
    monkeypatch.setattr(doctor, "REPLICATE_API_TOKEN", "")
    checks = doctor.check_diarization()
    assert checks[0].status == doctor.WARN
    assert "Replicate" in checks[0].detail


def test_voice_embedding_off_is_reported_as_a_state_not_a_problem(monkeypatch):
    """The configuration every install starts in. A WARN here would train the
    owner to ignore doctor's warnings."""
    monkeypatch.setattr(doctor, "REMOTE_VOICE_MODEL", "")
    checks = doctor.check_voice_embedding()
    assert [c.status for c in checks] == [doctor.OK]
    assert "off" in checks[0].detail
    assert "MMC_REMOTE_VOICE_MODEL" in checks[0].fix


def test_a_voice_model_without_a_token_fails(monkeypatch):
    """Configured but unusable is a misconfiguration, not a preference: every
    run would pay the planning cost and fail at the call."""
    monkeypatch.setattr(doctor, "REMOTE_VOICE_MODEL", "farazmatin/speaker-embed")
    monkeypatch.setattr(doctor, "REPLICATE_API_TOKEN", "")
    checks = doctor.check_voice_embedding()
    assert checks[0].status == doctor.FAIL
    assert "REPLICATE_API_TOKEN" in checks[0].detail


def test_replicate_diarization_check(monkeypatch):
    monkeypatch.setattr(doctor, "REPLICATE_API_TOKEN", "r8_fake")
    checks = doctor.check_diarization()
    assert checks[0].status == doctor.OK
    assert "remotely" in checks[0].detail


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

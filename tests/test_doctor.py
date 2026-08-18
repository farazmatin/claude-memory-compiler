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
    monkeypatch.setattr(doctor, "REPLICATE_API_TOKEN", "")
    monkeypatch.setattr(doctor, "ASR_BACKEND", "whisperx")
    monkeypatch.setattr(doctor, "HF_TOKEN", None)
    monkeypatch.setattr(doctor, "ENABLE_DIARIZATION", True)

    checks = doctor.check_diarization()
    assert checks[0].status == doctor.FAIL
    assert "silently" in checks[0].detail
    assert "speaker-diarization-3.1" in checks[0].fix


def test_disabled_diarization_warns_about_owners(monkeypatch):
    monkeypatch.setattr(doctor, "REPLICATE_API_TOKEN", "")
    monkeypatch.setattr(doctor, "ASR_BACKEND", "whisperx")
    monkeypatch.setattr(doctor, "ENABLE_DIARIZATION", False)
    checks = doctor.check_diarization()
    assert checks[0].status == doctor.WARN
    assert "owners" in checks[0].detail


def test_large_v3_on_cpu_is_flagged(monkeypatch):
    """Choosing large-v3 on CPU quietly makes the nightly batch impossible."""
    monkeypatch.setattr(doctor, "REPLICATE_API_TOKEN", "")
    monkeypatch.setattr(doctor, "ASR_BACKEND", "whisperx")
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


def test_replicate_diarization_check(monkeypatch):
    monkeypatch.setattr(doctor, "REPLICATE_API_TOKEN", "r8_fake")
    monkeypatch.setattr(doctor, "ASR_BACKEND", "replicate")
    checks = doctor.check_diarization()
    assert checks[0].status == doctor.OK
    assert "Replicate GPU" in checks[0].detail


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

"""Unit tests for Replicate serverless GPU ASR backend."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.replicate_asr import ReplicateBackend, ReplicateError


@pytest.fixture
def mock_audio(tmp_path: Path) -> Path:
    """Create a minimal fake WAV audio file."""
    wav = tmp_path / "sample.wav"
    # 44-byte WAV header + 32000 bytes of PCM (1 second of 16kHz mono)
    wav.write_bytes(b"RIFF" + b"\x00" * 40 + b"\x00" * 32000)
    return wav


def test_replicate_backend_init():
    backend = ReplicateBackend(
        api_token="test-token",
        model_name="custom/whisperx",
        language="en",
        diarize=True,
    )
    assert backend.name == "replicate:custom/whisperx"
    assert backend.api_token == "test-token"
    assert backend.diarize is True


def test_replicate_backend_missing_token(mock_audio: Path):
    backend = ReplicateBackend(api_token="")
    with pytest.raises(ReplicateError, match="REPLICATE_API_TOKEN is unset"):
        backend.transcribe(mock_audio, "m1", "")


def test_replicate_upload_file(tmp_path: Path):
    backend = ReplicateBackend(api_token="r8_test123")
    fake_wav = tmp_path / "audio.wav"
    fake_wav.write_bytes(b"RIFF" + b"\x00" * 40)

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"urls": {"get": "https://replicate.delivery/pbxt/test.wav"}}

    mock_client = MagicMock()
    mock_client.post.return_value = mock_resp

    url = backend._upload_file(mock_client, fake_wav)
    assert url == "https://replicate.delivery/pbxt/test.wav"
    mock_client.post.assert_called_once()


def test_replicate_create_prediction():
    backend = ReplicateBackend(
        api_token="r8_test123",
        model_name="victor-upmeet/whisperx",
        diarize=True,
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"id": "pred_12345", "status": "starting"}

    mock_client = MagicMock()
    mock_client.post.return_value = mock_resp

    pred = backend._create_prediction(
        mock_client,
        audio_url="https://replicate.delivery/test.wav",
        initial_prompt="Glossary: Project Atlas.",
    )

    assert pred["id"] == "pred_12345"
    call_args = mock_client.post.call_args
    assert "/predictions" in call_args[0][0]
    assert call_args[1]["json"]["version"] == "655845d6190ef70573c669245f245892cd039df4b880a1e3a65852c09252f5cc"
    payload = call_args[1]["json"]["input"]
    assert payload["audio_file"] == "https://replicate.delivery/test.wav"
    assert payload["diarization"] is True
    assert payload["initial_prompt"] == "Glossary: Project Atlas."


def test_replicate_poll_prediction_success():
    backend = ReplicateBackend(api_token="r8_test123", poll_interval_sec=0.01)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "id": "pred_12345",
        "status": "succeeded",
        "output": {
            "segments": [
                {
                    "start": 0.0,
                    "end": 2.5,
                    "text": "Hello world",
                    "speaker": "SPEAKER_00",
                    "words": [{"start": 0.0, "end": 1.0, "word": "Hello", "speaker": "SPEAKER_00"}],
                }
            ],
            "language": "en",
        },
    }

    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp

    result = backend._poll_prediction(mock_client, "pred_12345")
    assert result["status"] == "succeeded"
    assert len(result["output"]["segments"]) == 1


def test_replicate_poll_prediction_failed():
    backend = ReplicateBackend(api_token="r8_test123", poll_interval_sec=0.01)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "id": "pred_12345",
        "status": "failed",
        "error": "CUDA out of memory",
    }

    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp

    with pytest.raises(ReplicateError, match="CUDA out of memory"):
        backend._poll_prediction(mock_client, "pred_12345")


def test_replicate_transcribe_end_to_end(mock_audio: Path, monkeypatch):
    backend = ReplicateBackend(
        api_token="r8_valid_token",
        model_name="victor-upmeet/whisperx",
        poll_interval_sec=0.01,
    )

    # Mock normalize_audio_for_upload to avoid needing ffmpeg in unit test
    from pipeline import replicate_asr

    monkeypatch.setattr(replicate_asr, "normalize_audio_for_upload", lambda src, dst: src)

    upload_resp = MagicMock(status_code=201)
    upload_resp.json.return_value = {"urls": {"get": "https://replicate.delivery/audio.wav"}}

    create_resp = MagicMock(status_code=201)
    create_resp.json.return_value = {"id": "pred_abc", "status": "starting"}

    poll_resp = MagicMock(status_code=200)
    poll_resp.json.return_value = {
        "id": "pred_abc",
        "status": "succeeded",
        "output": {
            "segments": [
                {
                    "start": 0.5,
                    "end": 3.0,
                    "text": "Decision: we will use Postgres.",
                    "speaker": "SPEAKER_01",
                    "words": [
                        {"start": 0.5, "end": 1.0, "word": "Decision:", "speaker": "SPEAKER_01"},
                        {"start": 1.1, "end": 3.0, "word": "we will use Postgres.", "speaker": "SPEAKER_01"},
                    ],
                }
            ],
            "language": "en",
        },
    }

    def mock_post(url, *args, **kwargs):
        if "/files" in url:
            return upload_resp
        return create_resp

    mock_client_instance = MagicMock()
    mock_client_instance.post.side_effect = mock_post
    mock_client_instance.get.return_value = poll_resp

    with patch("httpx.Client", return_value=mock_client_instance):
        mock_client_instance.__enter__.return_value = mock_client_instance
        transcript = backend.transcribe(mock_audio, "test_meeting_01", "Glossary: Postgres")

    assert transcript.meeting_id == "test_meeting_01"
    assert transcript.model == "replicate:victor-upmeet/whisperx"
    assert transcript.language == "en"
    assert len(transcript.segments) == 1
    assert transcript.segments[0].text == "Decision: we will use Postgres."
    assert transcript.segments[0].speaker == "SPEAKER_01"
    assert transcript.speaker_labels == ["SPEAKER_01"]


def test_default_backend_selection(monkeypatch):
    from pipeline import asr

    monkeypatch.setattr(asr, "REPLICATE_API_TOKEN", "r8_some_token")
    backend = asr.default_backend()
    assert backend.__class__.__name__ == "ReplicateBackend"

    monkeypatch.setattr(asr, "REPLICATE_API_TOKEN", "")
    with pytest.raises(RuntimeError, match="REPLICATE_API_TOKEN is required"):
        asr.default_backend()

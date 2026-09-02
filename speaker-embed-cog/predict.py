"""Speaker embedding cog: one vector per diarized label, one call per meeting.

The caller sends a meeting's audio plus the speech regions whisperx already
assigned to each label, and gets back one vector per label. Label-to-person
mapping never crosses the wire - this only answers "what does this label sound
like".

## Matching the existing corpus, exactly

The pipeline already holds vectors produced by the retired local stage, and
whether they remain comparable decides whether ~23 enrolled people carry over or
enrollment restarts from zero. Comparability is not automatic - it depends on
reproducing the original procedure precisely:

  * the same weights (pyannote/wespeaker-voxceleb-resnet34-LM),
  * 16 kHz mono float32 audio,
  * a label's regions CONCATENATED into one clip and embedded in a SINGLE
    forward pass with `window="whole"`.

That last point matters most and is the one the design document got wrong: it
describes returning "one duration-weighted centroid per label", i.e. embedding
each region separately and averaging. Averaging per-region embeddings gives a
different vector from embedding the concatenation, so a centroid would look
plausible, score badly against the stored corpus, and be diagnosed as "the new
encoder is not comparable" when in fact only the procedure differed. The
original's own comment is explicit: "a single forward pass over the label's
actual speech is what the model in window='whole' mode expects".

## Encoders

Only `wespeaker-resnet34-lm` ships in this image. It is the one that makes the
existing corpus reusable, so it is the one worth building first; `titanet-large`
and `ecapa-voxceleb` from the design's shortlist are benchmark candidates that
can be added later without changing this interface. An unknown encoder name is
refused rather than silently substituted - serving the wrong weights under a
requested name would poison the namespace it was stored under.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path as FsPath

import numpy as np
import torch
from cog import BasePredictor, Input, Path

SAMPLE_RATE = 16_000

# Baked in at build time by scripts/fetch_weights.py, so a cold start never
# reaches Hugging Face and never needs a token at run time.
WEIGHTS = {
    "wespeaker-resnet34-lm": FsPath("/src/weights/wespeaker-resnet34-lm.bin"),
}


def decode(audio_path: FsPath) -> np.ndarray:
    """Decode any input container to 16 kHz mono float32.

    ffmpeg rather than a torch decoder: the input is whatever the recorder and
    the upload path produced, and ffmpeg is the only thing that reliably reads
    all of it.
    """
    process = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-threads", "0", "-i", str(audio_path),
            "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-",
        ],
        capture_output=True,
        check=True,
    )
    return np.frombuffer(process.stdout, dtype=np.float32)


class Predictor(BasePredictor):
    def setup(self) -> None:
        from pyannote.audio import Inference, Model

        self.inference = {}
        for name, path in WEIGHTS.items():
            if not path.exists():
                continue
            model = Model.from_pretrained(str(path))
            # window="whole" - one vector for whatever audio it is given, which
            # is what the concatenate-then-embed procedure requires.
            self.inference[name] = Inference(model, window="whole")
        if not self.inference:
            raise RuntimeError("no encoder weights were baked into this image")

    def predict(
        self,
        audio: Path = Input(description="Meeting audio, any container ffmpeg can read"),
        regions: str = Input(
            description=(
                'JSON array of {"label", "start", "end"} in seconds. Every region '
                "for a label is concatenated and embedded in one forward pass."
            )
        ),
        encoder: str = Input(
            default="wespeaker-resnet34-lm",
            description="Which encoder to serve. Unknown names are refused.",
        ),
    ) -> dict:
        if encoder not in self.inference:
            raise ValueError(
                f"unknown encoder {encoder!r}; this image serves "
                f"{sorted(self.inference)}. Refusing rather than substituting: the "
                "caller namespaces stored vectors by the encoder it asked for."
            )

        try:
            parsed = json.loads(regions)
        except json.JSONDecodeError as exc:
            raise ValueError(f"regions was not valid JSON: {exc}") from exc
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("regions must be a non-empty JSON array")

        by_label: dict[str, list[tuple[float, float]]] = {}
        for item in parsed:
            try:
                label = str(item["label"])
                start, end = float(item["start"]), float(item["end"])
            except (TypeError, KeyError, ValueError) as exc:
                raise ValueError(f"malformed region {item!r}: {exc}") from exc
            if end > start:
                by_label.setdefault(label, []).append((start, end))

        waveform = decode(FsPath(str(audio)))
        inference = self.inference[encoder]

        embeddings: dict[str, list[float]] = {}
        speech_sec: dict[str, float] = {}
        skipped: dict[str, str] = {}

        for label, spans in by_label.items():
            clips = [
                waveform[int(start * SAMPLE_RATE) : int(end * SAMPLE_RATE)]
                for start, end in sorted(spans)
            ]
            clips = [c for c in clips if c.size > 0]
            if not clips:
                # Never embed silence: a vector from nothing still looks like a
                # vector to everything downstream.
                skipped[label] = "no audio in the requested regions"
                continue

            clip = np.concatenate(clips)
            tensor = torch.from_numpy(np.ascontiguousarray(clip)).unsqueeze(0)
            vector = inference({"waveform": tensor, "sample_rate": SAMPLE_RATE})
            embeddings[label] = np.asarray(vector, dtype="float64").reshape(-1).tolist()
            speech_sec[label] = float(clip.size) / SAMPLE_RATE

        if not embeddings:
            raise ValueError(f"no label yielded usable audio: {skipped}")

        dims = {len(v) for v in embeddings.values()}
        if len(dims) != 1:
            raise RuntimeError(f"encoder returned mixed dimensions: {sorted(dims)}")

        return {
            "embeddings": embeddings,
            "dim": dims.pop(),
            "encoder": encoder,
            "speech_sec": speech_sec,
            "skipped": skipped,
        }

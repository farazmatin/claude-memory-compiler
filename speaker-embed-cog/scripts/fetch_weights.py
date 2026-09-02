"""Download the encoder checkpoints into ./weights before `cog build`.

Kept out of the image build so the
gated download happens once, with credentials that never reach the published
image, and so the built version hash pins an exact checkpoint.
"""

from __future__ import annotations

import os
from pathlib import Path

from huggingface_hub import hf_hub_download

# repo id -> local filename. Each entry must match a key in predict.WEIGHTS.
# pyannote.audio 3.3.2 loads a local checkpoint directly: its architecture and
# Lightning hyperparameters are embedded in pytorch_model.bin. The repository's
# config.yaml is fetched only for Hugging Face download counters and is not used
# by Model.from_pretrained for a local file, so there is nothing else to bake.
CHECKPOINTS = {
    "wespeaker-resnet34-lm": ("pyannote/wespeaker-voxceleb-resnet34-LM", "pytorch_model.bin"),
}


def main() -> int:
    # Optional: pyannote/wespeaker-voxceleb-resnet34-LM is not gated (verified
    # against the HF API). Passed when present so a checkpoint that later
    # becomes gated, or a private one added to CHECKPOINTS, still resolves.
    token = os.environ.get("HF_TOKEN") or None

    out = Path("weights")
    out.mkdir(parents=True, exist_ok=True)
    for name, (repo_id, filename) in CHECKPOINTS.items():
        path = hf_hub_download(repo_id=repo_id, filename=filename, token=token)
        dest = out / f"{name}.bin"
        dest.write_bytes(Path(path).read_bytes())
        print(f"{name}: {dest} ({dest.stat().st_size / 1e6:.1f} MB) from {repo_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

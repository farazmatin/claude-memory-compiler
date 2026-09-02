"""Download the encoder checkpoints into ./weights before `cog build`.

Run in CI with HF_TOKEN in the environment. Kept out of the image build so the
gated download happens once, with credentials that never reach the published
image, and so the built version hash pins an exact checkpoint.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download

# repo id -> local filename. Each entry must match a key in predict.WEIGHTS.
CHECKPOINTS = {
    "wespeaker-resnet34-lm": ("pyannote/wespeaker-voxceleb-resnet34-LM", "pytorch_model.bin"),
}


def main() -> int:
    token = os.environ.get("HF_TOKEN")
    if not token:
        print("HF_TOKEN is unset; the pyannote checkpoints are gated", file=sys.stderr)
        return 2

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

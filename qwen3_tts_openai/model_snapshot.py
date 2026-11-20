from __future__ import annotations

import json
import os
from pathlib import Path

from qwen3_tts_openai.errors import ServiceError
from qwen3_tts_openai.storage import file_hash, write_json


def resolve_hub_snapshot(model_id: str, revision: str, hf_home: Path, *, offline: bool) -> Path:
    from huggingface_hub import snapshot_download

    # In the pinned Hub client, concurrent downloads can race its Windows symlink probe.
    return Path(snapshot_download(model_id, revision=revision, cache_dir=hf_home / "hub",
                                  local_files_only=offline, ignore_patterns=["*.bin", "*.pt", "*.pth"],
                                  max_workers=1 if os.name == "nt" else 8))


def verify_hub_weights(snapshot: Path, model_id: str, hf_home: Path, *, offline: bool, record: bool = False):
    files = {file.relative_to(snapshot).as_posix(): file for file in sorted(snapshot.rglob("*.safetensors"))}
    if not files:
        raise ServiceError("model_cache_missing", "Snapshot contains no safetensors weights; pre-download the complete pinned model", 503)

    # Hub caches use blob symlinks when available and regular files on other filesystems.
    needs_receipt = any(not file.is_symlink() for file in files.values())
    receipt = hf_home / "verified-snapshots" / f"{model_id.replace('/', '--')}-{snapshot.name}.json"
    refresh_receipt = needs_receipt and not offline and (record or not receipt.is_file())
    if refresh_receipt:
        from huggingface_hub import HfApi

        info = HfApi().model_info(model_id, revision=snapshot.name, files_metadata=True)
        if info.sha != snapshot.name:
            raise ServiceError("model_cache_unverified", "Hub metadata does not match the resolved model revision", 503)
        expected = {file.rfilename: file.lfs.sha256 for file in info.siblings or []
                    if file.rfilename.endswith(".safetensors") and file.lfs}
    elif needs_receipt:
        try:
            document = json.loads(receipt.read_text(encoding="utf-8"))
            if (document["model_id"], document["revision"]) != (model_id, snapshot.name):
                raise ValueError("Verification receipt does not identify this model revision")
            expected = {name: weight["sha256"] for name, weight in document["weights"].items()}
        except (FileNotFoundError, ValueError, TypeError, KeyError, AttributeError) as error:
            raise ServiceError("model_cache_unverified", "No valid verification receipt for this snapshot. Run scripts/download_models.py --http --online", 503) from error
    else:
        expected = {name: file.resolve().name for name, file in files.items()}

    if files.keys() != expected.keys():
        raise ServiceError("model_cache_corrupt", "Checkpoint weight files do not match the pinned revision; restore the complete snapshot", 503)
    weights = {}
    for name, file in files.items():
        actual = file_hash(file)
        if actual != expected[name]:
            raise ServiceError("model_cache_corrupt", "Checkpoint SHA256 does not match the pinned revision; restore it before loading", 503, {"file": name})
        weights[name] = {"sha256": actual, "bytes": file.stat().st_size}
    if record or refresh_receipt:
        write_json(receipt, {"model_id": model_id, "revision": snapshot.name, "weights": weights})
    return weights

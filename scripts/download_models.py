"""Pre-download complete pinned snapshots including speech_tokenizer assets; no GPU needed."""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import Settings


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--combined", action="store_true", help="Also download the expression checkpoint")
    parser.add_argument("--http", action="store_true", help="Use resumable HTTP instead of Xet for this download")
    parser.add_argument("--online", action="store_true", help="Explicitly download even when the service runs offline")
    parser.add_argument("--retries", type=int, default=3, choices=range(0, 11), help="Resume interrupted HTTP transfers up to this many times")
    args = parser.parse_args()
    settings = Settings.from_env()
    settings.configure_process()
    if args.http:
        os.environ["HF_HUB_DISABLE_XET"] = "1"
    if args.online:
        os.environ["HF_HUB_OFFLINE"] = "0"
        os.environ["TRANSFORMERS_OFFLINE"] = "0"
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
    from huggingface_hub import snapshot_download
    from requests.exceptions import ChunkedEncodingError, ConnectionError, Timeout
    from storage import file_hash, write_json
    items = [(settings.model_id, settings.model_revision)]
    if args.combined:
        items.append((settings.expression_model_id, settings.expression_model_revision))
    for model_id, revision in items:
        for attempt in range(args.retries + 1):
            try:
                path = Path(snapshot_download(model_id, revision=revision, cache_dir=settings.hf_home / "hub", ignore_patterns=["*.bin", "*.pt", "*.pth"]))
                break
            except (ChunkedEncodingError, ConnectionError, Timeout) as error:
                if attempt == args.retries:
                    raise
                print(f"Transfer interrupted ({type(error).__name__}); resuming cached partials, retry {attempt + 1}/{args.retries}", flush=True)
                time.sleep(min(5, attempt + 1))
        weights = {}
        for file in sorted(path.rglob("*.safetensors")):
            expected = file.resolve().name
            actual = file_hash(file)
            if len(expected) != 64 or actual != expected:
                raise RuntimeError(f"Checkpoint integrity failed: {file.relative_to(path)}; do not load this snapshot")
            weights[str(file.relative_to(path))] = {"sha256": actual, "bytes": file.stat().st_size}
        if not weights:
            raise RuntimeError("Snapshot contains no safetensors weights")
        write_json(settings.hf_home / "verified-snapshots" / f"{model_id.replace('/', '--')}-{revision}.json", {"model_id": model_id, "revision": path.name, "weights": weights})
        print(f"Verified {len(weights)} weight files: {path}", flush=True)

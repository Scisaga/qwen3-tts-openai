"""Pre-download complete pinned snapshots including speech_tokenizer assets; no GPU needed."""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qwen3_tts_openai.config import Settings
from qwen3_tts_openai.model_snapshot import resolve_hub_snapshot, verify_hub_weights


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
    from requests.exceptions import ChunkedEncodingError, ConnectionError, Timeout
    offline = settings.hf_hub_offline and not args.online
    items = [(settings.model_id, settings.model_revision)]
    if args.combined:
        items.append((settings.expression_model_id, settings.expression_model_revision))
    for model_id, revision in items:
        for attempt in range(args.retries + 1):
            try:
                path = resolve_hub_snapshot(model_id, revision, settings.hf_home, offline=offline)
                break
            except (ChunkedEncodingError, ConnectionError, Timeout) as error:
                if attempt == args.retries:
                    raise
                print(f"Transfer interrupted ({type(error).__name__}); resuming cached partials, retry {attempt + 1}/{args.retries}", flush=True)
                time.sleep(min(5, attempt + 1))
        weights = verify_hub_weights(path, model_id, settings.hf_home, offline=offline, record=True)
        print(f"Verified {len(weights)} weight files: {path}", flush=True)

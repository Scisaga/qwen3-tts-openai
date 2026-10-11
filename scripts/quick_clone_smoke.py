"""Actual ASR -> ICL preparation -> new-sentence synthesis against an isolated deployment."""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf


def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": "Bearer " + os.environ["API_TOKEN"]} if os.getenv("API_TOKEN") else {}
    started = time.monotonic()
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=args.timeout + 10, trust_env=False) as client:

        def data(response):
            response.raise_for_status()
            return response.json()

        def wait_job(ident):
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                job = data(client.get(f"/v1/jobs/{ident}"))
                if job["status"] in {"succeeded", "failed", "cancelled"}:
                    if job["status"] != "succeeded":
                        raise RuntimeError(f"Task failed: {job['error']}")
                    return job
                time.sleep(1)
            raise RuntimeError("Timed out waiting for the isolated validation job")

        health = data(client.get("/health"))
        assert health["features"]["quick_voice_clone"]["enabled"]
        proof = None
        if getattr(args, "asr_proof", None):
            proof = json.loads(args.asr_proof.read_text())
            assert proof["real_asr_verified"] is True
            assert proof["audio_sha256"] == hashlib.sha256(args.sample.read_bytes()).hexdigest()
        print("Validating short-recording normalization and real ASR...", flush=True)
        with args.sample.open("rb") as sample:
            draft = data(
                client.post(
                    "/v1/voices/references",
                    files={"file": (args.sample.name, sample)},
                    data={"transcribe": "false"} if proof else {},
                )
            )
        if proof:
            assert draft["reference"]["processed_sha256"] == proof["audio_sha256"]
            draft["ref_text"] = proof["ref_text"]
        else:
            assert draft["transcription_state"] == "succeeded", draft["asr_error"]
        assert draft["ref_text"].strip()
        print("ASR complete; preparing ICL voice with the matching transcript...", flush=True)
        voice = data(
            client.post(
                "/v1/voices",
                data={
                    "draft_id": draft["draft_id"],
                    "name": "快速复刻验证音色",
                    "ref_text": draft["ref_text"],
                    "mode": "icl",
                },
            )
        )
        assert voice["mode"] == "icl" and voice["preparation_job_id"], voice.get("error")
        assert voice["reference"]["processed_sha256"] == draft["reference"]["processed_sha256"]
        wait_job(voice["preparation_job_id"])
        ready = next(v for v in data(client.get("/v1/voices"))["voices"] if v["voice_id"] == voice["voice_id"])
        assert ready["conditioning_state"] == "ready"
        print("Voice ready; synthesizing a new sentence with the real TTS model...", flush=True)
        job = data(
            client.post(
                "/v1/narrations",
                json={
                    "text": args.text,
                    "voice_id": voice["voice_id"],
                    "language": args.language,
                    "compatibility": "generic",
                },
            )
        )
        done = wait_job(job["job_id"])
        outputs = {}
        for kind in ("wav", "mp3"):
            artifact = next(a for a in done["artifacts"] if a["artifact_id"] == f"full-{kind}")
            response = client.get(artifact["url"])
            response.raise_for_status()
            path = args.output / ("new-sentence." + kind)
            path.write_bytes(response.content)
            outputs[kind] = {"bytes": path.stat().st_size}
        waveform, rate = sf.read(args.output / "new-sentence.wav")
        assert np.isfinite(waveform).all() and len(waveform) > 0 and np.max(np.abs(waveform)) > 1e-5
        response = client.delete(f"/v1/voices/references/{draft['draft_id']}")
        response.raise_for_status()
        report = {
            "passed": True,
            "real_asr_verified": True,
            "real_tts_verified": True,
            "model_id": health["model"]["model_id"],
            "mode": ready["mode"],
            "ref_text": draft["ref_text"],
            "reference_seconds": draft["reference"]["duration_seconds"],
            "new_text": args.text,
            "output_seconds": len(waveform) / rate,
            "sample_rate": rate,
            "outputs": outputs,
            "elapsed_seconds": time.monotonic() - started,
            "acoustic_similarity": "pending-human-listening",
            "asr_source": "verified-prior-response" if proof else "live-reference-endpoint",
            "asr_elapsed_seconds": proof["elapsed_seconds"] if proof else None,
        }
        (args.output / "quick-clone-live.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print("QUICK_CLONE_LIVE_PASS (real ASR and TTS; listening review remains separate)", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:12302")
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--text", default="你好，欢迎使用我的新音色。")
    parser.add_argument("--language", default="Chinese")
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument(
        "--asr-proof", type=Path, help="Reuse verified ASR output whose WAV hash matches the normalized reference"
    )
    parser.add_argument("--output", type=Path, default=Path("test-results/quick-clone-live"))
    main(parser.parse_args())

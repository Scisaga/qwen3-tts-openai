"""One official GPU holder and isolated service data; synthetic reference is diagnostic only."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qwen3_tts_openai.config import Settings
from qwen3_tts_openai.contracts import GenerationOptions, NarrationRequest
from qwen3_tts_openai.synthesis_service import SynthesisService
from qwen3_tts_openai.storage import write_json


def main(args):
    if not args.sample.is_file():
        raise ValueError("Reference WAV must exist before starting a GPU diagnostic")
    args.output.mkdir(parents=True, exist_ok=True)
    settings = Settings.from_env().model_copy(update={"data_dir": args.output / "data", "hf_hub_offline": True, "preload_model": True})
    settings.configure_process()
    service = SynthesisService(settings)
    result = {"scope": "official Qwen GPU/service diagnostic", "reference_kind": args.reference_kind,
              "reference_mode": args.mode, "text": args.text,
              "reference_path": str(args.sample.resolve()), "human_voice_similarity_verified": False,
              "listening_acceptance": "pending", "production_voice_library_modified": False}
    started = time.monotonic()
    def wait(ident):
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            job = service.jobs.get(ident)
            if job["state"] in {"succeeded", "failed", "cancelled"}:
                if job["state"] != "succeeded":
                    raise RuntimeError(json.dumps(job["error"], ensure_ascii=False))
                return job
            time.sleep(.2)
        raise TimeoutError("Diagnostic job did not finish")
    try:
        service.start()
        while not service.ready():
            if service.backend.health["state"] == "failed":
                raise RuntimeError(json.dumps(service.backend.health, ensure_ascii=False))
            if time.monotonic() - started > 300:
                raise TimeoutError("Model did not become ready")
            time.sleep(.25)
        result["loaded_health"] = service.health()
        print("Official model ready on the configured GPU", flush=True)
        voice = service.register_voice(args.sample, "isolated-diagnostic", args.ref_text, args.mode)
        wait(voice["preparation_job_id"])
        print("Reference conditioning prepared", flush=True)
        request = NarrationRequest(text=args.text, voice_id=voice["voice_id"], generation=GenerationOptions(seed=0, max_new_tokens=args.max_new_tokens, do_sample=not args.deterministic))
        submitted = service.submit(request)
        job = wait(submitted["job_id"])
        result["job"] = service.public_job(job)
        result["output_directory"] = str((settings.data_dir / "outputs" / job["id"]).resolve())
        result["final_health"] = service.health()
        result["elapsed_seconds"] = time.monotonic() - started
        result["execution"] = "succeeded"
        print("Official inference and MP3/WAV artifact pipeline succeeded", flush=True)
    except Exception as error:
        result.update(execution="failed", error={"type": type(error).__name__, "message": str(error)})
        raise
    finally:
        service.close()
        write_json(args.output / "diagnostic.json", result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--ref-text", required=True)
    parser.add_argument("--mode", choices=["icl", "x_vector_only"], default="icl")
    parser.add_argument("--text", default="你好，配音服务已经恢复。")
    parser.add_argument("--max-new-tokens", type=int, default=256)
    parser.add_argument("--deterministic", action="store_true")
    parser.add_argument("--reference-kind", choices=["synthetic", "authorized"], required=True)
    parser.add_argument("--output", type=Path, default=Path("test-results/gpu-diagnostic"))
    main(parser.parse_args())

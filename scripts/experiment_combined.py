"""Real Base vs injected-reference CustomVoice A/B experiment; requires authorized audio/GPU.

This writes measured data and listening artifacts, never promotes capabilities automatically.
Run while this project's service is stopped; it owns only its own model process.
"""
import argparse
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qwen3_tts_openai.audio_processing import AudioProcessor
from qwen3_tts_openai.backends.runtime import ModelRuntime
from qwen3_tts_openai.config import Settings
from qwen3_tts_openai.contracts import GenerationOptions
from qwen3_tts_openai.storage import Database, file_hash, write_json
from qwen3_tts_openai.voice_store import VoiceStore


def unit(voice, text, instruction=""):
    return {"spoken_text": text, "voice_snapshot": voice, "model_instruction": {"effective": instruction}}


def main(args):
    original = Settings.from_env()
    settings = original.model_copy(update={"backend": "qwen-base", "data_dir": args.output / "state"})
    settings.configure_process()
    args.output.mkdir(parents=True, exist_ok=True)
    audio = AudioProcessor(settings)
    audio.self_check()
    voices = VoiceStore(Database(settings.data_dir / "metadata.sqlite3"), settings.data_dir / "voices", audio)
    model = {"model_id": settings.model_id, "revision": settings.model_revision, "dtype": settings.dtype, "adapter_version": "official-qwen-0.1.1-binding-v1"}
    a = voices.register(args.sample_a, "reference-A", args.ref_text_a, "x_vector_only", model)
    b = voices.register(args.sample_b, "reference-B", args.ref_text_b, "x_vector_only", model)
    result = {"experiment": "Base-x-vector vs CustomVoice-reference-x-vector + instruct_ids", "capability_status": "experimental", "parameter_path": "CustomVoice.model.generate(input_ids, instruct_ids, voice_clone_prompt.ref_spk_embedding); speech_tokenizer.decode", "reference_mode": "x_vector_only for both adapters; CustomVoice has no speaker_encoder/validated ICL", "references": [{"voice_id": v["voice_id"], "sha256": v["reference"]["original_sha256"]} for v in (a,b)], "text": args.text, "seed": args.seed, "samples": [], "listening_review": {"status": "pending", "identity_similarity": None, "two_voice_isolation": None, "instruction_effect": None, "naturalness": None, "chapter_seams": None}}
    runtime = ModelRuntime(settings)
    options = GenerationOptions(seed=args.seed).model_dump()
    options["max_new_tokens"] = settings.max_new_tokens
    try:
        for voice in (a, b):
            runtime.prepare(voice)
            runtime.ensure_generation(voice)
            tag = "base-A" if voice == a else "base-B"
            raw = args.output / f"{tag}.wav"
            stats = runtime.generate(unit(voice,args.text), options, "Chinese", raw)
            result["samples"].append({"tag":tag,"sha256":file_hash(raw),"measurements":stats,"status":runtime.health})
        runtime.stop()
        combined = settings.model_copy(update={"backend":"qwen-combined-experimental"})
        runtime = ModelRuntime(combined)
        for tag, voice, instruction in (("combined-A-neutral",a,""),("combined-A-expressive",a,args.instruction),("combined-B-neutral",b,""),("combined-A-switch-back",a,"")):
            start=time.monotonic()
            runtime.ensure_generation(voice)
            raw=args.output/f"{tag}.wav"
            stats=runtime.generate(unit(voice,args.text,instruction),options,"Chinese",raw)
            processed=args.output/f"{tag}-processed.wav"
            audio.process(raw,processed,.9,.1,-1)
            audio.encode(processed,args.output/f"{tag}.mp3")
            result["samples"].append({"tag":tag,"instruction":instruction,"sha256":file_hash(raw),"measurements":stats,"status":runtime.health,"total_seconds":time.monotonic()-start})
        result["execution"]="completed-awaiting-listening"
    except Exception as e:
        result["execution"]="failed"
        result["error"]={"type":type(e).__name__,"message":str(e)}
        raise
    finally:
        runtime.stop()
        write_json(args.output/"experiment.json",result)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-a",type=Path,required=True)
    parser.add_argument("--sample-b",type=Path,required=True)
    parser.add_argument("--ref-text-a",default="")
    parser.add_argument("--ref-text-b",default="")
    parser.add_argument("--text",default="今天终于见到你了，我有很多事情想和你分享。")
    parser.add_argument("--instruction",default="用非常愉快、兴奋的语气说话。")
    parser.add_argument("--seed",type=int,default=0)
    parser.add_argument("--output",type=Path,default=Path("test-results/combined-ab"))
    main(parser.parse_args())

"""Official Python bindings. The combined experiment is isolated in its own method.

Calls private token preparation helpers only for the explicitly experimental adapter;
there are no global patches. Pin qwen-tts before changing this binding.
"""
from __future__ import annotations

import gc
import importlib.metadata
import json
import os
import re
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from config import Settings
from errors import ServiceError
from storage import digest, file_hash, write_json

ADAPTER_VERSION = "official-qwen-0.1.1-binding-v1"
PRESETS = ("vivian", "serena", "uncle_fu", "dylan", "eric", "ryan", "aiden", "ono_anna", "sohee")


def cuda_driver_diagnostics():
    """Read kernel and actually loaded libcuda versions without changing the host."""
    result = {"kernel_driver": None, "user_driver_library": None, "driver_version_mismatch": None}
    try:
        match = re.search(r"Kernel Module\s+(\d+\.\d+\.\d+)", Path("/proc/driver/nvidia/version").read_text())
        if match:
            result["kernel_driver"] = match.group(1)
        loaded = re.search(r"/[^\s]*libcuda\.so\.(\d+\.\d+\.\d+)", Path("/proc/self/maps").read_text())
        if loaded:
            result["user_driver_library"] = loaded.group(1)
        if result["kernel_driver"] and result["user_driver_library"]:
            result["driver_version_mismatch"] = result["kernel_driver"] != result["user_driver_library"]
    except OSError:
        pass
    return result


def capabilities(settings: Settings):
    if settings.backend == "qwen-base":
        clone, instruction, combined = "supported", "unsupported", "unsupported"
    elif settings.backend == "qwen-custom":
        clone, instruction, combined = "unsupported", "unsupported" if "0.6B" in settings.model_id else "supported", "unsupported"
    else:
        clone, instruction, combined = "experimental", "experimental", "experimental"
    return {"voice_clone": clone, "instruction_control": instruction, "combined_clone_instruction": combined, "reference_conditioning": "x-vector-only-experimental" if settings.backend == "qwen-combined-experimental" else "presets-only" if settings.backend == "qwen-custom" else "icl-and-x-vector", "control_granularity": "sentence-or-short-paragraph", "word_alignment": "unsupported", "numeric_controls": "rubberband-and-ffmpeg", "acoustic_validation": "pending-human-listening"}


class OfficialQwen:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = None
        self.loaded_kind = None
        self.identity = {}
        self.hardware = {}
        self.snapshots = {}
        self.prompt_root = settings.data_dir / "cache" / "conditions"
        self.prompt_root.mkdir(parents=True, exist_ok=True)

    def verify_device(self):
        import torch
        if not torch.cuda.is_available():
            raise ServiceError("gpu_unavailable", "CUDA unavailable. Check NVIDIA driver/library versions, Container Toolkit and GPU_DEVICE; CPU fallback is disabled", 503, cuda_driver_diagnostics())
        if torch.cuda.device_count() != 1:
            raise ServiceError("gpu_selection", "Exactly one GPU must be visible; set GPU_DEVICE to the RTX 2080 Ti UUID", 503)
        props = torch.cuda.get_device_properties(0)
        if "2080 Ti" not in props.name or (props.major, props.minor) != (7, 5):
            raise ServiceError("gpu_mismatch", f"Expected RTX 2080 Ti (sm_75), got {props.name} sm_{props.major}{props.minor}; refusing inference", 503)
        if props.total_memory < 10 * 1024**3:
            raise ServiceError("gpu_memory_budget", "Expected at least 10GiB device memory for the 11GB baseline budget", 503)
        flags = torch._C._cuda_getArchFlags()
        if not flags or "sm_75" not in flags:
            raise ServiceError("torch_architecture", "Installed PyTorch CUDA build does not include sm_75 kernels", 503)
        self.hardware = {"gpu": props.name, "uuid": str(getattr(props, "uuid", "unavailable")), "compute_capability": [props.major, props.minor], "total_memory_bytes": props.total_memory, "torch": torch.__version__, "cuda_build": torch.version.cuda, "cuda_arch_flags": flags, "dependencies": {p: importlib.metadata.version(p) for p in ("qwen-tts", "transformers", "accelerate", "torchaudio", "numpy", "soundfile")}}

    def snapshot(self, kind):
        from huggingface_hub import snapshot_download
        model_id, revision = (self.settings.expression_model_id, self.settings.expression_model_revision) if kind == "expression" else (self.settings.model_id, self.settings.model_revision)
        key = (model_id, revision)
        if key not in self.snapshots:
            path = Path(model_id)
            if path.is_dir():
                path = path.resolve()
                # Local checkpoints are trusted administrator configuration, never HTTP uploads.
                hashes = {str(p.relative_to(path)): file_hash(p) for p in sorted(path.rglob("*")) if p.is_file()}
                resolved = digest(hashes)
            else:
                try:
                    path = Path(snapshot_download(model_id, revision=revision, cache_dir=self.settings.hf_home / "hub", local_files_only=self.settings.hf_hub_offline, ignore_patterns=["*.bin", "*.pt", "*.pth"]))
                except Exception as error:
                    if self.settings.hf_hub_offline:
                        raise ServiceError("model_cache_missing", "Pinned model snapshot is not available offline. Run scripts/download_models.py --http --online before starting the service", 503, {"model_id": model_id, "revision": revision}) from error
                    raise
                resolved = path.name
            weights = {}
            for file in sorted(path.rglob("*.safetensors")):
                actual = file_hash(file)
                if not Path(model_id).is_dir():
                    expected = file.resolve().name
                    if not re.fullmatch(r"[0-9a-f]{64}", expected) or actual != expected:
                        raise ServiceError("model_cache_corrupt", "Checkpoint SHA256 does not match its pinned cache blob; restore it before loading", 503, {"file": str(file.relative_to(path))})
                weights[str(file.relative_to(path))] = {"sha256": actual, "bytes": file.stat().st_size}
            if not weights:
                raise ServiceError("model_cache_missing", "Snapshot contains no safetensors weights; pre-download the complete pinned model", 503)
            self.snapshots[key] = (path, {"model_id": model_id, "requested_revision": revision, "resolved_revision": resolved, "weights": weights, "dtype": self.settings.dtype, "attention": self.settings.attn_implementation, "adapter_version": ADAPTER_VERSION})
        return self.snapshots[key]

    def unload(self):
        import torch
        self.model = None
        self.loaded_kind = None
        gc.collect()
        torch.cuda.empty_cache()

    def select(self, kind):
        import torch
        from qwen_tts import Qwen3TTSModel
        if self.model is not None and self.loaded_kind == kind:
            return self.status()
        self.verify_device()
        if self.model is not None:
            self.unload()
        started = time.monotonic()
        path, identity = self.snapshot(kind)
        # Resolving the complete snapshot first also pins processor and nested tokenizer assets.
        model = Qwen3TTSModel.from_pretrained(str(path), device_map="cuda:0", dtype=torch.float16, attn_implementation=self.settings.attn_implementation, local_files_only=True)
        expected = "custom_voice" if kind == "expression" or self.settings.backend == "qwen-custom" else "base"
        if model.model.tts_model_type != expected:
            raise ServiceError("model_type_mismatch", f"Backend requires {expected}, checkpoint is {model.model.tts_model_type}", 503)
        self.model, self.loaded_kind = model, kind
        self.identity = {**identity, "tts_model_type": model.model.tts_model_type, "tts_model_size": model.model.tts_model_size, "load_seconds": time.monotonic() - started, "loaded_memory_bytes": torch.cuda.memory_allocated(0)}
        return self.status()

    def status(self):
        return {"identity": self.identity, "hardware": self.hardware, "loaded_kind": self.loaded_kind}

    def prompt_key(self, voice):
        _, identity = self.snapshot("base")
        return digest({"voice_conditioning_key": voice["conditioning_key"], "reference_sha256": voice["reference"]["original_sha256"], "model": identity, "mode": "x_vector_only" if self.settings.backend == "qwen-combined-experimental" else voice["mode"]})

    def prepare(self, voice):
        import torch
        from qwen_tts.inference.qwen3_tts_model import VoiceClonePromptItem
        key = self.prompt_key(voice)
        path, meta = self.prompt_root / f"{key}.npz", self.prompt_root / f"{key}.json"
        if path.is_file() and meta.is_file():
            try:
                doc = json.loads(meta.read_text())
                if doc["sha256"] == file_hash(path) and doc["key"] == key:
                    with np.load(path, allow_pickle=False) as data:
                        item = VoiceClonePromptItem(ref_code=torch.from_numpy(data["ref_code"].copy()) if doc["has_ref_code"] else None, ref_spk_embedding=torch.from_numpy(data["ref_spk_embedding"].copy()), x_vector_only_mode=doc["x_vector_only_mode"], icl_mode=doc["icl_mode"], ref_text=doc["ref_text"])
                    return [item], {**doc, "cache_hit": True}
            except (ValueError, OSError, KeyError, json.JSONDecodeError):
                pass
        if self.loaded_kind != "base":
            self.select("base")
        reference = self.settings.data_dir / "voices" / voice["voice_id"] / "reference.wav"
        if file_hash(reference) != voice["reference"]["processed_sha256"]:
            raise ServiceError("reference_corrupt", "Reference WAV hash changed; re-register the voice", 500)
        waveform, sr = sf.read(reference, dtype="float32")
        x_vector = voice["mode"] == "x_vector_only" or self.settings.backend == "qwen-combined-experimental"
        items = self.model.create_voice_clone_prompt(ref_audio=(waveform, sr), ref_text=voice["ref_text"] if not x_vector else None, x_vector_only_mode=x_vector)
        item = items[0]
        arrays = {"ref_spk_embedding": item.ref_spk_embedding.detach().cpu().float().numpy()}
        if item.ref_code is not None:
            arrays["ref_code"] = item.ref_code.detach().cpu().numpy()
        temp = path.with_suffix(".tmp.npz")
        np.savez_compressed(temp, **arrays)
        os.replace(temp, path)
        _, identity = self.snapshot("base")
        doc = {"key": key, "sha256": file_hash(path), "model": identity, "x_vector_only_mode": x_vector, "icl_mode": not x_vector, "ref_text": item.ref_text, "has_ref_code": item.ref_code is not None, "speaker_embedding_dimension": int(arrays["ref_spk_embedding"].size), "cache_hit": False}
        write_json(meta, doc)
        # Conditions stay on CPU in the cache. Only one requested voice moves to the GPU per call.
        item.ref_spk_embedding = item.ref_spk_embedding.detach().cpu()
        if item.ref_code is not None:
            item.ref_code = item.ref_code.detach().cpu()
        return items, doc

    def generate(self, unit, generation, language, target):
        import torch
        params = {**generation}
        seed = params.pop("seed")
        params["max_new_tokens"] = params.get("max_new_tokens") or self.settings.max_new_tokens
        params.update(non_streaming_mode=True, subtalker_dosample=params["do_sample"], subtalker_top_k=params["top_k"], subtalker_top_p=params["top_p"], subtalker_temperature=params["temperature"])
        torch.manual_seed(seed)
        torch.cuda.reset_peak_memory_stats(0)
        started = time.monotonic()
        instruction = unit["model_instruction"]["effective"]
        if self.settings.backend == "qwen-custom" and "0b6" in self.model.model.tts_model_size and instruction:
            raise ServiceError("instruction_unsupported", "0.6B CustomVoice cannot execute instruct; use 1.7B or explicitly degrade before submission", 422)
        voice = unit["voice_snapshot"]
        prompt_meta = None
        if self.settings.backend != "qwen-custom":
            prompt, prompt_meta = self.prepare(voice)
        if self.settings.backend == "qwen-combined-experimental":
            self.select("expression")
        decoder_steps = [0]
        def count_step(module, inputs, outputs):
            decoder_steps[0] += 1
        # Observe the talker without replacing methods or globally patching the upstream package.
        hook = self.model.model.talker.register_forward_hook(count_step)
        try:
            if self.settings.backend == "qwen-custom":
                wavs, sr = self.model.generate_custom_voice(text=unit["spoken_text"], language=language, speaker=voice["preset"], instruct=instruction, **params)
            elif self.settings.backend == "qwen-base":
                if instruction:
                    raise ServiceError("instruction_unsupported", "Base cannot execute instructions; compile with explicit degradation", 422)
                wavs, sr = self.model.generate_voice_clone(text=unit["spoken_text"], language=language, voice_clone_prompt=prompt, **params)
            else:
                wavs, sr = self.combined_generate(unit["spoken_text"], language, instruction, prompt, params)
        finally:
            hook.remove()
        audio = np.asarray(wavs[0], dtype=np.float32)
        if not audio.size or not np.isfinite(audio).all():
            raise ServiceError("invalid_model_audio", "Model returned empty or non-finite audio", 500)
        if not decoder_steps[0]:
            raise ServiceError("generation_diagnostics_failed", "Talker step diagnostics are unavailable; refusing output without a truncation check", 500)
        if decoder_steps[0] >= params["max_new_tokens"]:
            raise ServiceError("generation_token_limit", "Decoder reached MAX_NEW_TOKENS; split the sentence or increase the explicit limit. Potentially truncated output was not accepted", 500)
        sf.write(target, audio, sr, subtype="FLOAT")
        torch.cuda.synchronize(0)
        seconds = time.monotonic() - started
        return {"duration_seconds": len(audio) / sr, "sample_rate": sr, "generation_seconds": seconds, "rtf": seconds / (len(audio) / sr), "decoder_steps": decoder_steps[0], "peak_memory_bytes": torch.cuda.max_memory_allocated(0), "prompt": prompt_meta, "identity": self.identity, "hardware": self.hardware, "checkpoint_generation_defaults": self.model.generate_defaults, "combined_route_validation": getattr(self, "combined_validation", None)}

    def combined_generate(self, text, language, instruction, prompt, params):
        """Experimental CustomVoice talker + Base x-vector; no claim of weight compatibility.

        Official low-level generate accepts both instruct_ids and voice_clone_prompt. CustomVoice
        has no speaker_encoder, so extraction must come from Base before unloading it.
        The A/B harness checks shape compatibility and emits audio for identity/emotion review.
        """
        import torch
        qwen = self.model
        if qwen.model.tts_model_type != "custom_voice" or "0b6" in qwen.model.tts_model_size:
            raise ServiceError("combined_model_mismatch", "Combined experiment requires 1.7B CustomVoice weights", 503)
        item = prompt[0]
        dim = qwen.model.talker.get_input_embeddings().weight.shape[1]
        if item.ref_spk_embedding.numel() != dim:
            raise ServiceError("combined_weight_incompatible", "Base reference embedding does not match CustomVoice talker dimension", 503)
        self.combined_validation = {"reference_embedding_dimension": item.ref_spk_embedding.numel(), "custom_talker_dimension": dim, "parameter_path": "instruct_ids + voice_clone_prompt.ref_spk_embedding, x_vector_only", "semantic_weight_compatibility": "unvalidated-requires-listening"}
        qwen._validate_languages([language])
        input_ids = qwen._tokenize_texts([qwen._build_assistant_text(text)])
        instruct_ids = qwen._tokenize_texts([qwen._build_instruct_text(instruction)]) if instruction else [None]
        conditions = {"ref_spk_embedding": [item.ref_spk_embedding], "ref_code": [None], "x_vector_only_mode": [True], "icl_mode": [False]}
        with torch.inference_mode():
            codes, _ = qwen.model.generate(input_ids=input_ids, instruct_ids=instruct_ids, voice_clone_prompt=conditions, languages=[language], speakers=[None], **params)
            return qwen.model.speech_tokenizer.decode([{"audio_codes": codes[0]}])

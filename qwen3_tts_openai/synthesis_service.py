from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import soundfile as sf

from qwen3_tts_openai.asr_client import ASR_LANGUAGES, AsrClient
from qwen3_tts_openai.audio_processing import AudioProcessor, PROCESSING_VERSION
from qwen3_tts_openai.backends.qwen import ADAPTER_VERSION, PRESETS
from qwen3_tts_openai.backends.runtime import ModelRuntime
from qwen3_tts_openai.config import Settings
from qwen3_tts_openai.contracts import NarrationRequest, SpeechRequest
from qwen3_tts_openai.director_compiler import compile_narration
from qwen3_tts_openai.errors import Cancelled, ServiceError
from qwen3_tts_openai.job_store import JobStore, TERMINAL
from qwen3_tts_openai.job_progress import NarrationProgress
from qwen3_tts_openai.reference_store import ReferenceStore
from qwen3_tts_openai.storage import Database, digest, file_hash, write_json
from qwen3_tts_openai.voice_store import VoiceStore

log = logging.getLogger(__name__)


class SynthesisService:
    def __init__(self, settings: Settings, backend=None):
        self.settings = settings
        settings.configure_process()
        for name in ("voices", "jobs", "cache/raw", "cache/processed", "cache/assemblies", "outputs"):
            (settings.data_dir / name).mkdir(parents=True, exist_ok=True)
        database = Database(settings.data_dir / "metadata.sqlite3")
        self.audio = AudioProcessor(settings)
        self.asr = AsrClient(settings)
        self.references = ReferenceStore(settings.data_dir / "reference-drafts", self.audio, settings.reference_draft_ttl_seconds)
        self.voices = VoiceStore(database, settings.data_dir / "voices", self.audio)
        self.jobs = JobStore(database, settings.queue_limit)
        self.backend = backend or ModelRuntime(settings)
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.worker = None
        self.lock_file = None
        self.active_job = None
        self.started = False

    def start(self):
        self.lock_file = (self.settings.data_dir / ".service.lock").open("a+")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            self.lock_file.close()
            raise RuntimeError("DATA_DIR is already owned by another service; use exactly one API worker") from e
        self.jobs.recover()
        for job in self.jobs.list(1000):
            if job["kind"] == "voice_prepare" and job["state"] in {"failed", "cancelled"}:
                self.voices.preparation_finished(job["request"]["voice"], job["error"] or {"type": "cancelled", "message": "Preparation cancelled; retry explicitly"})
        self.started = True
        self.worker = threading.Thread(target=self._worker_loop, name="tts-durable-queue", daemon=True)
        self.worker.start()

    def close(self):
        self.stop_event.set()
        self.wake.set()
        self.backend.stop()
        if self.worker:
            self.worker.join(timeout=5)
        if self.lock_file and (not self.worker or not self.worker.is_alive()):
            self.lock_file.close()
        self.started = False

    def health(self):
        return {"service": "qwen3-tts-openai", "alive": self.started, "inference_ready": self.ready(), "model": dict(self.backend.health), "audio": dict(self.audio.status), "active_job_id": self.active_job, "queue_limit": self.settings.queue_limit, "gpu_device_configured": bool(self.settings.gpu_device), "port": self.settings.port, "api_auth_enabled": bool(self.settings.api_token), "admin_enabled": bool(self.settings.admin_token), "features": {"quick_voice_clone": self.quick_clone_status()}}

    def quick_clone_status(self):
        configured = bool(self.settings.asr_base_url)
        supported = self.settings.backend == "qwen-base"
        asr = self.asr.snapshot()
        return {**asr, "configured": configured, "backend_supported": supported,
                "enabled": configured and supported, "available": configured and supported and asr["available"],
                "max_reference_seconds": min(30, self.settings.max_reference_seconds),
                "max_upload_bytes": self.settings.max_upload_bytes,
                "draft_ttl_seconds": self.settings.reference_draft_ttl_seconds,
                "https_url": self.settings.https_public_url,
                "https_local_ca": bool(self.settings.https_public_url and self.settings.https_local_ca)}

    def require_quick_clone(self):
        if not self.settings.asr_base_url:
            raise ServiceError("quick_clone_disabled", "未配置 ASR_BASE_URL，快速复刻功能未启用。", 403)
        if self.settings.backend != "qwen-base":
            raise ServiceError("voice_clone_unsupported", "快速复刻需要 qwen-base 完整参考模式，请检查后端配置。", 422)

    def create_reference(self, source, language=None, transcribe=True):
        self.require_quick_clone()
        if language and language not in ASR_LANGUAGES:
            raise ServiceError("invalid_language", "请选择支持的录音语言，或使用自动识别。", 422)
        self.disk_check()
        draft = self.references.create(source)
        if not transcribe:
            return draft
        return self.references.transcribe(draft["draft_id"], self.asr, language)

    def transcribe_reference(self, ident, language=None):
        self.require_quick_clone()
        return self.references.transcribe(ident, self.asr, language)

    def register_reference(self, ident, name, ref_text, mode=None, voice_id=None):
        self.require_quick_clone()
        if mode not in {None, "icl"}:
            raise ServiceError("invalid_reference_mode", "快速复刻使用完整参考模式，请确认录音对应文字。", 422)
        return self.references.register(ident, name, ref_text, self.register_voice, voice_id)

    def ready(self):
        inference_kind_ready = self.settings.backend != "qwen-combined-experimental" or self.backend.health.get("loaded_kind") == "expression"
        return self.started and self.audio.status["state"] == "ready" and self.backend.health["state"] == "ready" and inference_kind_ready

    def resolve_voice(self, ident):
        if self.settings.backend == "qwen-custom":
            preset = ident.removeprefix("preset:").lower()
            if not ident.startswith("preset:") or preset not in PRESETS:
                raise ServiceError("voice_mapping_required", "CustomVoice only accepts listed preset:<speaker> voices; reference cloning requires Base or the experimental adapter", 422)
            return {"voice_id": "preset:" + preset, "name": preset, "preset": preset, "conditioning_state": "ready"}
        if ident.startswith("preset:"):
            raise ServiceError("voice_unsupported", "Base requires a registered reference voice; presets are available only with qwen-custom", 422)
        return self.voices.get(ident)

    def list_voices(self):
        if self.settings.backend == "qwen-custom":
            return [self.resolve_voice("preset:" + p) for p in PRESETS]
        return self.voices.list()

    def compile(self, request: NarrationRequest):
        try:
            return compile_narration(request, self.settings, self.backend.capabilities, self.resolve_voice)
        except (TypeError, AttributeError, ValueError, OverflowError) as e:
            # Invalid dynamically-shaped source JSON is an input error, never an internal crash.
            raise ServiceError("invalid_director", f"Invalid director/SSML value: {e}", 422) from e

    def disk_check(self):
        if shutil.disk_usage(self.settings.data_dir).free < self.settings.min_free_disk_bytes:
            raise ServiceError("disk_full", "Insufficient free disk; free space in DATA_DIR or adjust MIN_FREE_DISK_BYTES", 507)

    def submit(self, request: NarrationRequest, raw_input: str | None = None):
        if self.stop_event.is_set():
            raise ServiceError("service_stopping", "Service is shutting down", 503)
        plan = self.compile(request)
        if not plan["valid"]:
            raise ServiceError("preflight_failed", "Resolve preflight errors or explicitly allow a supported degradation", 422, plan)
        self.disk_check()
        job = self.jobs.submit("narration", {"input": request.model_dump(), "raw_input": raw_input}, plan)
        self.wake.set()
        return self.public_job(job)

    def register_voice(self, source, name, ref_text, mode=None, voice_id=None):
        if self.settings.backend == "qwen-custom":
            raise ServiceError("voice_clone_unsupported", "CustomVoice exposes presets; use qwen-base to register reference audio", 422)
        if self.settings.backend == "qwen-combined-experimental" and (mode == "icl" or mode is None and ref_text.strip()):
            raise ServiceError("reference_mode_unsupported", "Combined experiment currently uses x_vector_only. Select that mode explicitly; complete ICL is available with qwen-base", 422)
        if not name.strip() or len(name) > 100 or len(ref_text) > 4000:
            raise ServiceError("invalid_voice_metadata", "Name must be 1-100 characters and ref_text <=4000 characters", 422)
        self.disk_check()
        model = {"model_id": self.settings.model_id, "revision": self.settings.model_revision, "dtype": self.settings.dtype, "adapter_version": ADAPTER_VERSION}
        voice = self.voices.register(source, name, ref_text, mode, model, voice_id)
        try:
            job = self.prepare_voice(voice["voice_id"])
        except ServiceError as e:
            self.voices.preparation_finished(voice, e.payload()["error"])
            # Preserve a receipt for callers retrying a draft after a queue rejection.
            failed = {**self.voices.get(voice["voice_id"]), "preparation_job_id": None}
            raise ServiceError(e.code, e.message, e.status, {"voice": failed, "cause": e.details}) from e
        return {**voice, "preparation_job_id": job["job_id"]}

    def prepare_voice(self, ident):
        voice = self.voices.get(ident)
        job = self.jobs.submit("voice_prepare", {"voice": voice})
        self.wake.set()
        return self.public_job(job)

    def edit_voice(self, ident, changes):
        voice, changed = self.voices.edit(ident, changes)
        if changed:
            try:
                voice["preparation_job_id"] = self.prepare_voice(ident)["job_id"]
            except ServiceError as e:
                self.voices.preparation_finished(voice, e.payload()["error"])
                raise
        return voice

    async def wait_job(self, ident, timeout):
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            job = await asyncio.to_thread(self.jobs.get, ident)
            if job["state"] in TERMINAL:
                return job
            await asyncio.sleep(0.1)
        await asyncio.to_thread(self.jobs.cancel, ident)
        raise ServiceError("sync_timeout", "Synchronous wait timed out; this job was cancelled at a safe boundary. Use /v1/narrations for longer jobs", 504, {"job_id": ident})

    async def speech(self, request: SpeechRequest, raw_input=None):
        if request.model not in {"qwen3-tts", self.settings.model_id}:
            raise ServiceError("model_unsupported", "Supported model is qwen3-tts or the configured MODEL_ID", 422)
        try:
            obj = json.loads(request.input)
            if isinstance(obj, dict) and ("pages" in obj or "narration_director" in obj):
                raise ServiceError("director_endpoint_required", "Send director JSON to /v1/narrations, not speech.input", 422)
        except json.JSONDecodeError:
            pass
        narration = NarrationRequest(text=request.input, voice_id=request.voice, speed=request.speed, compatibility="generic", voice_profile={"instructions": request.instructions or ""})
        job = await asyncio.to_thread(self.submit, narration, raw_input)
        result = await self.wait_job(job["job_id"], self.settings.sync_timeout_seconds)
        if result["state"] != "succeeded":
            error = result["error"] or {"type": "cancelled", "message": "Job cancelled"}
            raise ServiceError(error["type"], error["message"], error.get("http_status", 500), {"job_id": job["job_id"], "cause": error})
        return self.artifact(job["job_id"], "full-" + request.response_format)

    def public_job(self, job):
        artifacts = [{"artifact_id": ident, **{k: v for k, v in value.items() if k != "path"}, "url": f"/v1/jobs/{job['id']}/artifacts/{ident}"} for ident, value in (job.get("result") or {}).get("artifacts", {}).items()]
        return {"job_id": job["id"], "kind": job["kind"], "status": job["state"], "progress": job["progress"], "progress_detail": job.get("progress_detail"), "created_at": job["created"], "updated_at": job["updated"], "cancel_requested": bool(job["cancel_requested"]), "error": job["error"], "warnings": (job["plan"] or {}).get("issues", []), "artifacts": artifacts, "status_url": f"/v1/jobs/{job['id']}"}

    def get_job(self, ident):
        return self.public_job(self.jobs.get(ident))

    def cancel(self, ident):
        return self.public_job(self.jobs.cancel(ident))

    def artifact(self, ident, artifact_id):
        job = self.jobs.get(ident)
        value = (job.get("result") or {}).get("artifacts", {}).get(artifact_id)
        if value is None:
            raise ServiceError("artifact_not_found", "Unknown artifact_id; use the links returned by the job", 404)
        root = (self.settings.data_dir / "outputs" / ident).resolve()
        path = (root / value["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ServiceError("artifact_not_found", "Artifact file missing", 404)
        if file_hash(path) != value["sha256"]:
            raise ServiceError("artifact_corrupt", "Artifact hash mismatch; resubmit to restore from verified cache", 500)
        return path, value["media_type"]

    def _worker_loop(self):
        try:
            self.audio.self_check()
        except ServiceError:
            log.exception("Audio toolchain check failed")
        if self.settings.preload_model:
            try:
                self.backend.preload()
            except Exception:
                log.exception("Model preload failed; HTTP remains available")
        while not self.stop_event.is_set():
            try:
                job = self.jobs.claim()
                if not job:
                    self.wake.wait(0.5)
                    self.wake.clear()
                    continue
                self.active_job = job["id"]
                self._execute(job)
                self.active_job = None
            except Exception:
                log.exception("Durable worker error")
                self.wake.wait(0.5)

    def _execute(self, job):
        started = time.monotonic()
        ident = job["id"]
        def check():
            if self.stop_event.is_set() or self.jobs.cancelled(ident):
                raise Cancelled()
            if time.monotonic() - started > self.settings.job_timeout_seconds:
                raise ServiceError("job_timeout", "Job exceeded JOB_TIMEOUT_SECONDS; split the narration", 504)
            self.disk_check()
        try:
            check()
            self.jobs.progress(ident, .01, {"stage": "preparing"})
            if job["kind"] == "model_preload":
                self.backend.preload()
                check()
                self.jobs.finish(ident, "succeeded", {"model": self.backend.provenance()})
            elif job["kind"] == "voice_prepare":
                result = self.backend.prepare(job["request"]["voice"])
                check()
                voice = job["request"]["voice"]
                self.voices.preparation_finished(voice, details=result)
                self.jobs.finish(ident, "succeeded", {"voice_id": voice["voice_id"], "conditioning": result})
            else:
                if self.audio.status["state"] != "ready":
                    self.audio.self_check()
                result = self._produce(job, check, started)
                check()
                self.jobs.finish(ident, "succeeded", result)
        except Cancelled:
            self.jobs.finish(ident, "cancelled", error={"type": "cancelled", "message": "Cancelled at a safe generation/processing boundary"})
            if job["kind"] == "voice_prepare":
                self.voices.preparation_finished(job["request"]["voice"], {"type": "cancelled", "message": "Reference preparation cancelled; retry explicitly"})
        except Exception as e:
            log.exception("Job %s failed", ident)
            if isinstance(e, ServiceError):
                error = {**e.payload()["error"], "http_status": e.status}
            elif isinstance(e, OSError) and e.errno == errno.ENOSPC:
                error = {"type": "disk_full", "message": "DATA_DIR ran out of space; free space and resubmit", "http_status": 507}
            else:
                error = {"type": "execution_failed", "message": str(e)[-2000:], "http_status": 500}
            self.jobs.finish(ident, "failed", error=error)
            if job["kind"] == "voice_prepare":
                self.voices.preparation_finished(job["request"]["voice"], error)
        finally:
            # Persist an execution record even when an output did not complete.
            try:
                write_json(self.settings.data_dir / "jobs" / ident / "execution.json", {"job_id": ident, "elapsed_seconds": time.monotonic() - started, "state": self.jobs.get(ident)["state"], "error": self.jobs.get(ident)["error"]})
            except OSError:
                log.exception("Cannot persist execution record")

    def _cache(self, directory, key, extension):
        path = self.settings.data_dir / "cache" / directory / (key + extension)
        meta = path.with_suffix(path.suffix + ".json")
        if path.is_file() and meta.is_file():
            try:
                doc = json.loads(meta.read_text())
                if doc["key"] == key and doc["sha256"] == file_hash(path):
                    sf.info(path)
                    return path, meta, doc
            except (ValueError, OSError, KeyError, RuntimeError):
                pass
        return path, meta, None

    def _output_limit(self, output):
        total = sum(p.stat().st_size for p in output.rglob("*") if p.is_file())
        if total > self.settings.max_output_bytes:
            raise ServiceError("output_limit", "Job outputs exceed MAX_OUTPUT_BYTES; split the narration", 413)

    def _produce(self, job, check, started):
        plan, ident = job["plan"], job["id"]
        output = self.settings.data_dir / "outputs" / ident
        output.mkdir(exist_ok=True)
        write_json(output / "source-input.json", job["request"]["input"])
        source_files = {}
        for name, raw in job["request"]["input"].get("source_documents", {}).items():
            path = output / f"source-{name}.json"
            path.write_bytes(raw.encode("utf-8"))
            source_files[name] = {"sha256": file_hash(path), "bytes": path.stat().st_size}
        if job["request"].get("raw_input") is not None:
            (output / "original-request.json").write_text(job["request"]["raw_input"], encoding="utf-8")
        write_json(output / "execution-plan.json", plan)
        units, processed_paths = [], {}
        generation = {**plan["generation"], "max_new_tokens": plan["generation"].get("max_new_tokens") or self.settings.max_new_tokens}
        progress = NarrationProgress(self.jobs, ident, plan["units"])
        for index, unit in enumerate(plan["units"]):
            check()
            progress.update(index, {"stage": "preparing"})
            # Model identity is resolved before cache lookup; a moving revision cannot reuse stale audio.
            self.backend.ensure_generation(unit["voice_snapshot"])
            provenance = self.backend.provenance()
            voice = unit["voice_snapshot"]
            voice_projection = {k: voice.get(k) for k in ("reference", "ref_text", "mode", "model", "preset")}
            raw_key = digest({"text": unit["text"], "spoken_text": unit["spoken_text"], "voice": voice_projection, "expression": unit["model_instruction"]["effective"], "backend": provenance, "generation": generation, "language": plan["language"]})
            raw, raw_meta, raw_doc = self._cache("raw", raw_key, ".wav")
            raw_hit = raw_doc is not None
            if not raw_hit:
                temp = raw.with_suffix(".tmp.wav")
                try:
                    stats = self.backend.generate(unit, generation, plan["language"], temp,
                                                  progress=lambda event: progress.update(index, event))
                    check()
                    if temp.stat().st_size > self.settings.max_output_bytes or sf.info(temp).duration > self.settings.max_audio_seconds:
                        raise ServiceError("output_limit", "Generated audio exceeds output limits", 413)
                    os.replace(temp, raw)
                finally:
                    temp.unlink(missing_ok=True)
                raw_doc = {"key": raw_key, "sha256": file_hash(raw), "stats": stats}
                write_json(raw_meta, raw_doc)
            controls = {k: unit["postprocessing"][k] for k in ("tempo", "pitch_semitones", "gain_db")}
            progress.update(index, {"stage": "postprocessing"})
            if sf.info(raw).duration / controls["tempo"] > self.settings.max_audio_seconds:
                raise ServiceError("output_limit", "Slowed audio would exceed MAX_AUDIO_SECONDS", 413)
            post_key = digest({"raw_sha256": raw_doc["sha256"], "controls": controls, "sample_rate": self.settings.work_sample_rate, "channels": 1, "pipeline": PROCESSING_VERSION, "ffmpeg": self.audio.status.get("version"), "rubberband": self.audio.status.get("rubberband_cli")})
            processed, post_meta, post_doc = self._cache("processed", post_key, ".wav")
            post_hit = post_doc is not None
            post_started = time.monotonic()
            if not post_hit:
                self.audio.process(raw, processed, **controls)
                check()
                post_doc = {"key": post_key, "sha256": file_hash(processed), "frames": self.audio.frames(processed)}
                write_json(post_meta, post_doc)
            processed_paths[unit["unit_id"]] = processed
            for kind, source in (("raw", raw), ("processed", processed)):
                dest = output / f"{unit['unit_id']}-{kind}.wav"
                shutil.copyfile(source, dest)
            record = {"unit_id": unit["unit_id"], "page": unit["page"], "segment": unit["segment"], "voice_id": unit["voice_id"], "raw_key": raw_key, "processed_key": post_key, "raw_cache_hit": raw_hit, "processed_cache_hit": post_hit, "generation": raw_doc["stats"], "generation_measurement_source": "cache-entry" if raw_hit else "this-job", "generation_seconds_this_job": 0 if raw_hit else raw_doc["stats"]["generation_seconds"], "postprocessing_seconds": time.monotonic() - post_started, "controls": controls, "control_owners": unit["control_owners"], "model_instruction": unit["model_instruction"]}
            units.append(record)
            self._output_limit(output)
            progress.update(index, {"stage": "unit_complete"})
        check()
        self.jobs.progress(ident, .90, {"stage": "assembling"})
        # Explicit sub-frame silence prevents libmp3lame's tiny final-granule edge case.
        # Count it in both page and whole-narration PCM before encoding, never crop speech.
        frame_size = 576 if self.settings.work_sample_rate <= 24000 else 1152
        execution_events, page_frames, last_page = [], 0, None
        def codec_pad():
            extra = (-page_frames) % frame_size
            if extra:
                execution_events.append({"kind": "silence", "page": last_page, "pause_ms": extra * 1000 / self.settings.work_sample_rate, "reason": "mp3-frame-safety-padding", "owner": "mp3-encoding"})
        for event in plan["events"]:
            if last_page is not None and event["page"] != last_page:
                codec_pad()
                page_frames = 0
            last_page = event["page"]
            execution_events.append(event)
            if event["kind"] == "speech":
                page_frames += self.audio.frames(processed_paths[event["unit_id"]])
            elif event["kind"] == "silence":
                page_frames += round(event["pause_ms"] * self.settings.work_sample_rate / 1000)
        codec_pad()
        assembly_key = digest({"units": [u["processed_key"] for u in units], "events": execution_events, "pages": plan["pages"], "mp3": self.settings.mp3_bitrate, "sample_rate": self.settings.work_sample_rate, "ffmpeg": self.audio.status.get("version"), "rubberband": self.audio.status.get("rubberband_cli"), "version": PROCESSING_VERSION})
        assembly_dir = self.settings.data_dir / "cache" / "assemblies" / assembly_key
        assembly_hit = False
        meta = assembly_dir / "checksums.json"
        if meta.is_file():
            try:
                checksums = json.loads(meta.read_text())
                if all((assembly_dir / name).is_file() and file_hash(assembly_dir / name) == sha for name, sha in checksums.items()):
                    for name in checksums:
                        shutil.copyfile(assembly_dir / name, output / name)
                    assembly_hit = True
            except (ValueError, OSError):
                pass
        if not assembly_hit:
            timeline = self.audio.assemble(execution_events, processed_paths, output / "full.wav", check)
            self.jobs.progress(ident, .93, {"stage": "encoding"})
            self.audio.encode(output / "full.wav", output / "full.mp3")
            check()
            timeline["mp3"] = self.audio.encoded_info(output / "full.mp3")
            if abs(timeline["frames"] - timeline["mp3"]["decoded_frames"]) > 1:
                raise ServiceError("mp3_timeline_mismatch", "Final decoded MP3 sample count differs from PCM; timeline rejected", 500)
            timeline["pages"] = []
            for page in plan["pages"]:
                check()
                p = page["page"]
                wav, mp3 = output / f"page-{p:02d}.wav", output / f"page-{p:02d}.mp3"
                page_timeline = self.audio.assemble([e for e in execution_events if e["page"] == p], processed_paths, wav, check)
                self.audio.encode(wav, mp3)
                encoded = self.audio.encoded_info(mp3)
                if abs(page_timeline["frames"] - encoded["decoded_frames"]) > 1:
                    raise ServiceError("mp3_timeline_mismatch", f"Page {p} MP3 timeline mismatch", 500)
                events = [e for e in timeline["events"] if e["page"] == p]
                target = page.get("target_seconds")
                timeline["pages"].append({"page": p, "chapter": page.get("chapter"), "target_seconds": target, "target_delta_seconds": page_timeline["duration_seconds"] - target if target else None, "start_seconds": events[0]["start_seconds"] if events else 0, "end_seconds": events[-1]["end_seconds"] if events else 0, "duration_seconds": page_timeline["duration_seconds"], "mp3": encoded})
                self._output_limit(output)
                self.jobs.progress(ident, .95 + .035 * len(timeline["pages"]) / len(plan["pages"]), {"stage": "encoding"})
            write_json(output / "timeline.json", timeline)
            names = ["full.wav", "full.mp3", "timeline.json"] + [f"page-{p['page']:02d}.{ext}" for p in plan["pages"] for ext in ("wav", "mp3")]
            staging = Path(tempfile.mkdtemp(dir=assembly_dir.parent, prefix=".assembly-"))
            try:
                for name in names:
                    shutil.copyfile(output / name, staging / name)
                write_json(staging / "checksums.json", {name: file_hash(staging / name) for name in names})
                if assembly_dir.exists():
                    shutil.rmtree(assembly_dir)
                os.replace(staging, assembly_dir)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        check()
        self._output_limit(output)
        self.jobs.progress(ident, .99, {"stage": "saving"})
        manifest = {"schema_version": 1, "job_id": ident, "source_hash": plan["source_hash"], "text_hash": plan["text_hash"], "source_documents": source_files, "original_request_sha256": file_hash(output / "original-request.json") if (output / "original-request.json").is_file() else None, "model": self.backend.provenance(), "runtime": self.backend.health, "ffmpeg": self.audio.status, "generation": generation, "language": plan["language"], "units": units, "assembly_cache_key": assembly_key, "assembly_cache_hit": assembly_hit, "codec_padding": [e for e in execution_events if e.get("owner") == "mp3-encoding"], "warnings": plan["issues"], "elapsed_seconds": time.monotonic() - started, "capabilities": self.backend.capabilities, "listening_acceptance": "pending", "generation_granularity": "natural-sentence-or-short-paragraph; chapter-continuity-unverified"}
        write_json(output / "manifest.json", manifest)
        artifacts = {}
        for path in sorted(output.iterdir()):
            if path.suffix not in {".json", ".wav", ".mp3"}:
                continue
            artifact_id = path.stem + ("-" + path.suffix[1:] if path.suffix in {".wav", ".mp3"} else "")
            artifacts[artifact_id] = {"path": path.name, "media_type": {".wav": "audio/wav", ".mp3": "audio/mpeg", ".json": "application/json"}[path.suffix], "bytes": path.stat().st_size, "sha256": file_hash(path)}
        self._output_limit(output)
        return {"artifacts": artifacts, "elapsed_seconds": time.monotonic() - started}

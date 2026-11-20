"""Cancellation through the durable queue and a real, GPU-free spawned holder."""

import json
import multiprocessing
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from qwen3_tts_openai.backends.runtime import ModelRuntime
from qwen3_tts_openai.contracts import NarrationRequest
from qwen3_tts_openai.synthesis_service import SynthesisService
from tests.conftest import ControlledBackend, wait_done


def controlled_audio_holder(connection, started):
    """Behave like a model holder without importing Torch or loading a model."""
    try:
        while True:
            command, arguments = connection.recv()
            assert command == "generate"
            target = Path(arguments["target"])
            generation = arguments["generation"]
            if generation["controlled_block"]:
                target.write_bytes(b"incomplete generated audio")
                started.set()
                steps = 0
                while True:
                    if generation["controlled_progress"]:
                        steps += 1
                        connection.send({"event": "progress", "progress": {
                            "stage": "synthesizing", "decoder_steps": steps,
                        }})
                    time.sleep(.03)
            sf.write(target, np.zeros(14400, dtype=np.float32), 24000, subtype="PCM_16")
            connection.send({"ok": True, "result": {
                "generation_seconds": 0, "duration_seconds": .6, "controlled_reply": "fresh",
            }, "status": {"state": "ready"}})
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


class RpcGenerationBackend(ControlledBackend):
    """Use service fixtures for conditioning and real runtime RPC for generation."""

    def __init__(self, settings, emit_progress):
        super().__init__()
        self.runtime = ModelRuntime(settings)
        self.context = multiprocessing.get_context("spawn")
        self.holder_started = self.context.Event()
        self.emit_progress = emit_progress
        self.holders = []
        self.targets = []

    def generate(self, unit, generation, language, target, progress=None, check=None):
        self.calls.append(unit)
        self.targets.append(target)
        if self.runtime.process is None:
            self.runtime.connection, child = self.context.Pipe()
            self.runtime.process = self.context.Process(
                target=controlled_audio_holder, args=(child, self.holder_started), daemon=True,
            )
            self.runtime.process.start()
            child.close()
            self.holders.append(self.runtime.process)
        return self.runtime.generate(
            unit, {**generation, "controlled_block": len(self.calls) == 1,
                   "controlled_progress": self.emit_progress}, language, target,
            progress=progress, check=check,
        )

    def stop(self):
        self.runtime.stop()


@pytest.mark.parametrize("emit_progress", [False, True], ids=["silent-holder", "continuous-progress"])
def test_running_cancel_releases_holder_cleans_partial_audio_and_preserves_queue(
    settings, sample, monkeypatch, emit_progress,
):
    backend = RpcGenerationBackend(settings, emit_progress)
    service = SynthesisService(settings, backend)
    release_entered, allow_release = threading.Event(), threading.Event()
    release_states = []
    service.start()
    try:
        voice = service.register_voice(sample, "取消测试", "对应参考文字。", "icl")
        assert wait_done(service, voice["preparation_job_id"])["state"] == "succeeded"
        request = NarrationRequest(text="需要取消的第一句。", voice_id=voice["voice_id"])
        first = service.submit(request)["job_id"]
        assert backend.holder_started.wait(10), service.jobs.get(first)
        if emit_progress:
            deadline = time.monotonic() + 5
            detail = service.jobs.get(first)["progress_detail"] or {}
            while not detail.get("decoder_steps") and time.monotonic() < deadline:
                time.sleep(.02)
                detail = service.jobs.get(first)["progress_detail"] or {}
            assert detail.get("decoder_steps", 0) > 0
        owned = backend.runtime.process
        original_stop = backend.runtime.stop

        def observe_holder_release():
            if backend.runtime.process is owned:
                release_states.append(service.jobs.get(first)["state"])
                release_entered.set()
                assert allow_release.wait(10), "Test did not permit holder cleanup"
                original_stop()
                release_states.append(service.jobs.get(first)["state"])
            else:
                original_stop()

        monkeypatch.setattr(backend.runtime, "stop", observe_holder_release)
        queued = service.submit(request.model_copy(update={"text": "取消排队的第二句。"}))["job_id"]
        last = service.submit(request.model_copy(update={"text": "应当成功的最后一句。"}))["job_id"]

        assert service.cancel(queued)["status"] == "cancelled"
        assert backend.runtime.process is owned and owned.is_alive()
        assert not release_entered.is_set()

        assert service.cancel(first)["status"] == "running"
        assert release_entered.wait(10), service.jobs.get(first)
        assert service.jobs.get(first)["state"] == "running"
        assert owned.is_alive() and backend.targets[0].is_file()
        allow_release.set()

        cancelled = wait_done(service, first, timeout=10)
        assert cancelled["state"] == "cancelled" and cancelled["result"] is None
        assert owned._closed and release_states == ["running", "running"]
        assert not backend.targets[0].exists()

        completed = wait_done(service, last, timeout=10)
        assert completed["state"] == "succeeded", completed["error"]
        assert service.jobs.get(queued)["state"] == "cancelled"
        assert len(backend.calls) == 2 and len(backend.holders) == 2
        assert backend.holders[1] is not owned
        assert not list((settings.data_dir / "cache/raw").glob("*.tmp.wav"))
        manifest = json.loads(service.artifact(last, "manifest")[0].read_text(encoding="utf-8"))
        assert manifest["units"][0]["generation"]["controlled_reply"] == "fresh"
        assert not manifest["units"][0]["raw_cache_hit"]
        assert service.artifact(last, "full-wav")[0].is_file()
    finally:
        allow_release.set()
        service.close()


class BlockingStageBackend(ControlledBackend):
    def __init__(self, blocked_method):
        super().__init__()
        self.blocked_method = blocked_method
        self.entered = threading.Event()
        self.shutdown = threading.Event()

    def block(self, method, check):
        if self.blocked_method != method:
            return
        self.entered.set()
        assert callable(check), f"{method} did not receive the job cancellation check"
        deadline = time.monotonic() + 10
        while not self.shutdown.wait(.02):
            check()
            if time.monotonic() > deadline:
                raise AssertionError(f"{method} cancellation check did not stop the job")

    def preload(self, check=None):
        self.block("preload", check)
        return super().preload(check=check)

    def prepare(self, voice, check=None):
        self.block("prepare", check)
        return super().prepare(voice, check=check)

    def ensure_generation(self, voice, check=None):
        self.block("ensure_generation", check)
        return super().ensure_generation(voice, check=check)

    def stop(self):
        self.shutdown.set()
        super().stop()


@pytest.mark.parametrize("blocked_method", ["preload", "prepare", "ensure_generation"])
def test_service_forwards_cancellation_check_during_model_and_voice_preparation(
    settings, sample, blocked_method,
):
    backend = BlockingStageBackend(blocked_method)
    service = SynthesisService(settings, backend)
    service.start()
    try:
        if blocked_method == "preload":
            ident = service.jobs.submit("model_preload", {})["id"]
            service.wake.set()
        else:
            voice = service.register_voice(sample, "阶段取消测试", "对应参考文字。", "icl")
            ident = voice["preparation_job_id"]
            if blocked_method == "ensure_generation":
                assert wait_done(service, ident)["state"] == "succeeded"
                ident = service.submit(NarrationRequest(
                    text="准备模型期间取消。", voice_id=voice["voice_id"],
                ))["job_id"]
        assert backend.entered.wait(10), service.jobs.get(ident)
        service.cancel(ident)
        cancelled = wait_done(service, ident, timeout=5)
        assert cancelled["state"] == "cancelled", cancelled["error"]
        assert cancelled["error"]["type"] == "cancelled"
        assert cancelled["result"] is None and not backend.calls
        if blocked_method == "prepare":
            stored_voice = service.voices.get(voice["voice_id"])
            deadline = time.monotonic() + 5
            while stored_voice["conditioning_state"] == "pending" and time.monotonic() < deadline:
                time.sleep(.02)
                stored_voice = service.voices.get(voice["voice_id"])
            assert stored_voice["conditioning_state"] == "failed"
            assert stored_voice["error"]["type"] == "cancelled"
    finally:
        service.close()

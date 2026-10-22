import json
import multiprocessing
import sqlite3
import time

import numpy as np
import pytest
import soundfile as sf

from qwen3_tts_openai.backends.runtime import ModelRuntime
from qwen3_tts_openai.contracts import NarrationRequest
from qwen3_tts_openai.errors import ServiceError
from qwen3_tts_openai.job_progress import NarrationProgress
from qwen3_tts_openai.job_store import JobStore
from qwen3_tts_openai.storage import Database
from qwen3_tts_openai.synthesis_service import SynthesisService
from tests.conftest import ControlledBackend, wait_done


def progress_holder(connection):
    for number in range(2):
        connection.recv()
        for steps in (1, 12, 24):
            connection.send({"event": "progress", "progress": {"stage": "synthesizing", "decoder_steps": steps}})
        connection.send({"ok": True, "result": number, "status": {"state": "ready"}})


def endless_progress_holder(connection):
    connection.recv()
    while True:
        connection.send({"event": "progress", "progress": {"stage": "synthesizing", "decoder_steps": 1}})
        time.sleep(.03)


def attach_holder(runtime, target):
    context = multiprocessing.get_context("spawn")
    runtime.connection, child = context.Pipe()
    runtime.process = context.Process(target=target, args=(child,))
    runtime.process.start()
    child.close()


def test_rpc_delivers_events_and_waits_for_each_final_reply(settings):
    runtime = ModelRuntime(settings)
    attach_holder(runtime, progress_holder)
    events = []
    try:
        assert runtime._rpc("generate", {}, 5, events.append) == 0
        assert runtime._rpc("generate", {}, 5, events.append) == 1
        assert [e["decoder_steps"] for e in events] == [1, 12, 24, 1, 12, 24]
    finally:
        runtime.stop()


def test_progress_events_do_not_extend_generation_deadline(settings):
    runtime = ModelRuntime(settings)
    attach_holder(runtime, endless_progress_holder)
    events = []
    started = time.monotonic()
    with pytest.raises(ServiceError) as error:
        runtime._rpc("generate", {}, 1, events.append)
    assert error.value.code == "model_timeout"
    assert events and time.monotonic() - started < 2
    assert runtime.process is None


def test_failed_progress_sink_stops_holder_without_leaking_a_reply_into_next_request(settings):
    runtime = ModelRuntime(settings)
    attach_holder(runtime, endless_progress_holder)
    owned = runtime.process
    def fail(event):
        raise OSError("controlled storage failure")
    with pytest.raises(OSError, match="storage failure"):
        runtime._rpc("generate", {}, 5, fail)
    assert owned._closed and runtime.process is None


def test_job_database_migration_preserves_existing_job(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY,kind TEXT,state TEXT,request TEXT,plan TEXT,result TEXT,error TEXT,created REAL,updated REAL,progress REAL,cancel_requested INTEGER)")
        db.execute("INSERT INTO jobs VALUES ('old','narration','running','{}',NULL,NULL,NULL,1,1,.25,0)")
    store = JobStore(Database(path), 8)
    assert store.get("old")["progress"] == .25
    assert store.get("old")["progress_detail"] is None
    store.progress("old", .5, {"stage": "synthesizing", "decoder_steps": 12})
    assert store.get("old")["progress_detail"]["decoder_steps"] == 12


def test_running_progress_is_monotone_bounded_and_terminal_jobs_ignore_late_events(tmp_path):
    store = JobStore(Database(tmp_path / "jobs.sqlite3"), 8)
    ident = store.submit("narration", {})["id"]
    store.claim()
    progress = NarrationProgress(store, ident, [{"spoken_text": "测试第一段文字。"}, {"spoken_text": "这是第二段较长的测试文字。"}])
    values = []
    for index in (0, 1):
        for steps in (1, 10, 20, 40, 5000):
            progress.update(index, {"stage": "synthesizing", "decoder_steps": steps})
            job = store.get(ident)
            assert job["progress_detail"]["estimated"]
            values.append(job["progress"])
        progress.update(index, {"stage": "postprocessing"})
        values.append(store.get(ident)["progress"])
        progress.update(index, {"stage": "unit_complete"})
        values.append(store.get(ident)["progress"])
    assert values == sorted(values) and max(values) < 1
    store.progress(ident, .1)
    assert store.get(ident)["progress"] == max(values)
    store.finish(ident, "succeeded")
    progress.update(0, {"stage": "synthesizing", "decoder_steps": 2})
    assert store.get(ident)["progress"] == 1
    assert store.get(ident)["progress_detail"] == {"stage": "succeeded"}


class ProgressBackend(ControlledBackend):
    """Writes controlled tone frames and reports their real length for browser tests."""
    def __init__(self, delay=.04):
        super().__init__()
        self.frame_delay = delay
        self.progress_snapshots = []
        self.observe = None

    def generate(self, unit, generation, language, target, progress=None):
        started = time.monotonic()
        with sf.SoundFile(target, "w", samplerate=24000, channels=1, subtype="PCM_16") as audio:
            for frame in range(100):
                samples = np.arange(frame * 1920, (frame + 1) * 1920)
                audio.write(.1 * np.sin(samples * 2 * np.pi * 440 / 24000))
                if progress:
                    progress({"stage": "synthesizing", "decoder_steps": frame + 2})
                    if self.observe:
                        self.progress_snapshots.append(self.observe())
                time.sleep(self.frame_delay)
        return {"duration_seconds": 8.0, "generation_seconds": time.monotonic() - started}


def test_single_unit_reports_many_live_updates_before_audio_is_finished(settings, sample):
    backend = ProgressBackend(delay=0)
    service = SynthesisService(settings, backend)
    service.start()
    try:
        voice = service.register_voice(sample, "controlled progress", "对应参考文字。", "icl")
        assert wait_done(service, voice["preparation_job_id"])["state"] == "succeeded"
        backend.observe = lambda: service.public_job(service.jobs.get(service.active_job))
        ident = service.submit(NarrationRequest(text="这是一段足够长的测试文稿，用于检查段内进度。", voice_id=voice["voice_id"]))["job_id"]
        final = wait_done(service, ident)
        snapshots = backend.progress_snapshots
        assert len({round(s["progress"] * 100) for s in snapshots}) > 10
        assert all(s["status"] == "running" and s["progress"] < 1 for s in snapshots)
        assert snapshots[-1]["progress_detail"]["generated_audio_seconds"] == 8
        assert final["state"] == "succeeded" and final["progress"] == 1
        manifest = json.loads(service.artifact(ident, "manifest")[0].read_text())
        assert not manifest["units"][0]["raw_cache_hit"]
    finally:
        service.close()

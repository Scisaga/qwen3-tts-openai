import multiprocessing
import hashlib
import sys
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from qwen3_tts_openai.backends.runtime import ModelRuntime, model_process
from qwen3_tts_openai.config import Settings
from qwen3_tts_openai.errors import ServiceError
from qwen3_tts_openai.backends.qwen import OfficialQwen


def sleeper(connection):
    connection.recv()
    time.sleep(10)


def graph_failure(connection):
    connection.recv()
    connection.send({"ok": False, "error": {"type": "cuda_graph_failed", "message": "controlled capture failure"},
                     "http_status": 503, "status": {"inference_engine": "cuda-graph-predictor"}})
    time.sleep(10)


def test_watchdog_only_terminates_owned_model_process(settings):
    runtime=ModelRuntime(settings)
    context=multiprocessing.get_context("spawn")
    runtime.connection,child=context.Pipe()
    runtime.process=context.Process(target=sleeper,args=(child,))
    runtime.process.start()
    child.close()
    owned=runtime.process
    with pytest.raises(ServiceError) as caught:
        runtime._rpc("select",{},.2)
    assert caught.value.code=="model_timeout"
    assert runtime.process is None and runtime.health["state"]=="failed"
    assert owned._closed


def test_capture_failure_restarts_only_owned_holder_and_keeps_graph_configuration(settings):
    runtime = ModelRuntime(settings.model_copy(update={"cuda_graph_enabled": True}))
    context = multiprocessing.get_context("spawn")
    runtime.connection, child = context.Pipe()
    runtime.process = context.Process(target=graph_failure, args=(child,))
    runtime.process.start()
    child.close()
    owned = runtime.process
    with pytest.raises(ServiceError) as error:
        runtime._rpc("select", {}, 5)
    assert error.value.code == "cuda_graph_failed"
    assert owned._closed and runtime.process is None
    assert runtime.health["state"] == "failed"
    assert runtime.health["inference_engine"] == "cuda-graph-predictor"
    assert runtime.settings.cuda_graph_enabled


@pytest.mark.parametrize("values",[{"device_map":"auto"},{"device_map":"cpu"},{"gpu_device":"all"},{"gpu_device":"0,1"},{"dtype":"bfloat16"},{"attn_implementation":"flash_attention_2"},{"work_sample_rate":24000,"mp3_bitrate":"192k"},{"work_sample_rate":16000,"mp3_bitrate":"256k"},{"torch_num_threads":0},{"torch_num_interop_threads":0},{"torch_num_threads":65}])
def test_configuration_rejects_device_and_codec_fallbacks(values):
    with pytest.raises(ValueError):
        Settings(**values)


def test_gpu_identity_retains_host_uuid_after_container_remapping(tmp_path):
    driver = tmp_path / "gpus" / "0000:01:00.0"
    driver.mkdir(parents=True)
    uuid = "GPU-test-fixture"
    (driver / "information").write_text(f"Model: NVIDIA GeForce RTX 2080 Ti\nGPU UUID: {uuid}\nDevice Minor: 1\n")
    settings = Settings(gpu_device="0", gpu_host_device=uuid)
    selection = settings.gpu_selection(tmp_path / "gpus")
    assert selection["host_index"] == 1
    assert selection["host_uuid"] == uuid
    assert selection["host_name"] == "NVIDIA GeForce RTX 2080 Ti"
    assert selection["visible_selector"] == "0"
    assert selection["logical_device"] == "cuda:0"
    # Missing metadata cannot turn visible device 0 into a supposed host GPU 0.
    unknown = settings.gpu_selection(tmp_path / "missing")
    assert unknown["host_index"] is None and unknown["host_uuid"] == uuid
    assert Settings(gpu_device="1").gpu_selection(tmp_path / "missing")["host_index"] == 1


def test_model_snapshot_rejects_corrupt_cached_weights_before_loading(settings, tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    content = b"controlled integrity fixture, not model weights"
    blob = tmp_path / hashlib.sha256(content).hexdigest()
    blob.write_bytes(content)
    (snapshot / "model.safetensors").symlink_to(blob)
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=lambda *a, **kw: snapshot))
    adapter = OfficialQwen(settings)
    _, identity = adapter.snapshot("base")
    assert identity["weights"]["model.safetensors"]["sha256"] == blob.name
    blob.write_bytes(b"corrupt")
    with pytest.raises(ServiceError) as error:
        OfficialQwen(settings).snapshot("base")
    assert error.value.code == "model_cache_corrupt"


def test_incomplete_offline_model_has_actionable_error(settings, monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("missing snapshot")
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=unavailable))
    adapter = OfficialQwen(settings.model_copy(update={"hf_hub_offline": True}))
    with pytest.raises(ServiceError) as error:
        adapter.snapshot("base")
    assert error.value.code == "model_cache_missing"
    assert "--http --online" in error.value.message


def test_model_process_sets_thread_budget_before_adapter_initialization(settings, monkeypatch):
    state = {}
    fake_torch = SimpleNamespace(set_num_threads=lambda value: state.update(intraop=value),
                                set_num_interop_threads=lambda value: state.update(interop=value))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setenv("HF_HOME", "controlled-original")

    class Adapter:
        def __init__(self, configured):
            assert state == {"intraop": 2, "interop": 1}
            assert configured.torch_num_threads == 2
            state["initialized"] = True

    monkeypatch.setattr("qwen3_tts_openai.backends.qwen.OfficialQwen", Adapter)
    connection = SimpleNamespace(recv=lambda: ("stop", {}), close=lambda: state.update(closed=True))
    configured = settings.model_copy(update={"torch_num_threads": 2, "torch_num_interop_threads": 1})
    model_process(connection, configured.model_dump())
    assert state["initialized"] and state["closed"]


@pytest.mark.parametrize("fails", [False, True])
def test_generation_inference_context_and_hook_are_cleaned_on_exit(settings, monkeypatch, tmp_path, fails):
    import numpy as np

    state = {"inference": False, "removed": False}

    @contextmanager
    def inference_mode():
        state["inference"] = True
        try:
            yield
        finally:
            state["inference"] = False

    torch = SimpleNamespace(inference_mode=inference_mode, manual_seed=lambda seed: None,
                            cuda=SimpleNamespace(reset_peak_memory_stats=lambda device: None,
                                                 synchronize=lambda device: None,
                                                 max_memory_allocated=lambda device: 0))
    monkeypatch.setitem(sys.modules, "torch", torch)

    def register(hook):
        state["hook"] = hook
        return SimpleNamespace(remove=lambda: state.update(removed=True))

    def generate(**kwargs):
        assert state["inference"]
        state["hook"](None, None, None)
        if fails:
            raise RuntimeError("controlled generation failure")
        return [np.zeros(2400, dtype=np.float32)], 24000

    adapter = OfficialQwen(settings.model_copy(update={"backend": "qwen-custom"}))
    adapter.model = SimpleNamespace(model=SimpleNamespace(tts_model_size="1b7", talker=SimpleNamespace(register_forward_hook=register)),
                                    generate_custom_voice=generate, generate_defaults={})
    unit = {"spoken_text": "测试。", "voice_snapshot": {"preset": "vivian"}, "model_instruction": {"effective": ""}}
    generation = {"seed": 0, "do_sample": True, "top_k": 50, "top_p": 1.0, "temperature": 0.9, "max_new_tokens": 256}
    if fails:
        with pytest.raises(RuntimeError, match="controlled generation failure"):
            adapter.generate(unit, generation, "Chinese", tmp_path / "audio.wav")
    else:
        result = adapter.generate(unit, generation, "Chinese", tmp_path / "audio.wav")
        assert result["decoder_steps"] == 1 and (tmp_path / "audio.wav").is_file()
    assert not state["inference"] and state["removed"]

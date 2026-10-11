import multiprocessing
import hashlib
import sys
import time
from types import SimpleNamespace

import pytest

from backends.runtime import ModelRuntime
from config import Settings
from errors import ServiceError
from backends.qwen import OfficialQwen


def sleeper(connection):
    connection.recv()
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


@pytest.mark.parametrize("values",[{"device_map":"auto"},{"device_map":"cpu"},{"gpu_device":"all"},{"gpu_device":"0,1"},{"dtype":"bfloat16"},{"attn_implementation":"flash_attention_2"},{"work_sample_rate":24000,"mp3_bitrate":"192k"},{"work_sample_rate":16000,"mp3_bitrate":"256k"}])
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

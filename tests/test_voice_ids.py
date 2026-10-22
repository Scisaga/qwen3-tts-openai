from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from qwen3_tts_openai.app import create_app
from qwen3_tts_openai.contracts import NarrationRequest
from qwen3_tts_openai.storage import file_hash
from qwen3_tts_openai.synthesis_service import SynthesisService
from tests.conftest import ControlledBackend, wait_done


def test_semantic_id_is_used_by_upload_sample_compile_and_speech(settings, sample):
    app = create_app(settings, ControlledBackend())
    with TestClient(app) as client:
        response = client.post("/v1/voices", data={"name": "讲解女声", "voice_id": "narrator", "ref_text": "参考文字。"},
                               files={"file": ("reference.wav", sample.read_bytes())})
        assert response.status_code == 202, response.text
        voice = response.json()
        assert voice["voice_id"] == "narrator"
        assert wait_done(app.state.service, voice["preparation_job_id"])["state"] == "succeeded"
        assert client.get("/v1/voices/narrator/sample").content == sample.read_bytes()
        plan = app.state.service.compile(NarrationRequest(text="使用简短音色 ID。", voice_id="narrator"))
        assert plan["valid"] and all(u["voice_id"] == "narrator" for u in plan["units"])
        audio = client.post("/v1/audio/speech", json={"model": "qwen3-tts", "voice": "narrator", "input": "测试。", "response_format": "wav"})
        assert audio.status_code == 200 and audio.content.startswith(b"RIFF")
        edited = client.patch("/v1/voices/narrator", json={"name": "更新显示名称"})
        assert edited.json()["voice_id"] == "narrator"
        assert "preparation_job_id" not in edited.json()
    # The ID and reference path must survive a real service restart.
    with TestClient(create_app(settings, ControlledBackend())) as restarted:
        assert restarted.get("/v1/voices").json()["voices"][0]["voice_id"] == "narrator"
        assert restarted.get("/v1/voices/narrator/sample").content == sample.read_bytes()


@pytest.mark.parametrize(("name", "expected"), [("Studio Narrator", "studio-narrator"), ("温柔女声", "温柔女声"),
                                                ("  My__Voice · HD  ", "my-voice-hd"), ("🎙️", "voice"),
                                                ("References", "references-voice"), ("A" * 100, "a" * 40)])
def test_name_generates_readable_ids_with_short_collision_suffixes(settings, sample, name, expected):
    service = SynthesisService(settings, ControlledBackend())
    first = service.register_voice(sample, name, "参考文字。")
    second = service.register_voice(sample, name, "参考文字。")
    assert first["voice_id"] == expected
    assert second["voice_id"] == expected[:38].rstrip("-") + "-2"
    assert file_hash(service.voices.path(first["voice_id"], original=True)) == file_hash(sample)
    assert len(service.voices.list()) == 2


def test_concurrent_same_name_uploads_never_replace_each_other(settings, sample):
    service = SynthesisService(settings, ControlledBackend())
    with ThreadPoolExecutor(max_workers=4) as pool:
        voices = list(pool.map(lambda _: service.register_voice(sample, "Narrator", "参考文字。"), range(4)))
    assert {v["voice_id"] for v in voices} == {"narrator", "narrator-2", "narrator-3", "narrator-4"}
    assert len(service.voices.list()) == 4
    assert all(service.voices.path(v["voice_id"]).is_file() for v in voices)


def test_explicit_duplicate_id_does_not_overwrite_voice_or_reference(settings, sample):
    app = create_app(settings, ControlledBackend())
    with TestClient(app) as client:
        data = {"name": "原始音色", "voice_id": "narrator", "ref_text": "参考文字。"}
        files = {"file": ("reference.wav", sample.read_bytes())}
        first = client.post("/v1/voices", data=data, files=files).json()
        wait_done(app.state.service, first["preparation_job_id"])
        before = app.state.service.voices.get("narrator")
        conflict = client.post("/v1/voices", data={**data, "name": "另一个音色"}, files=files)
        assert conflict.status_code == 409 and conflict.json()["error"]["type"] == "voice_id_exists"
        assert app.state.service.voices.list() == [before]
        assert client.get("/v1/voices/narrator/sample").content == sample.read_bytes()
        assert len(app.state.service.backend.preparations) == 1
        assert {p.name for p in (settings.data_dir / "voices").iterdir()} == {"narrator"}


@pytest.mark.parametrize("ident", ["../outside", "a/b", "a\\b", ".", "..", "-start", "end-", "a--b", "Uppercase", "has_space", "references", "a" * 41])
def test_invalid_ids_are_rejected_without_creating_a_voice(settings, sample, ident):
    app = create_app(settings, ControlledBackend())
    with TestClient(app) as client:
        response = client.post("/v1/voices", data={"name": "测试", "voice_id": ident},
                               files={"file": ("reference.wav", sample.read_bytes())})
        assert response.status_code == 422 and response.json()["error"]["type"] == "invalid_voice_id"
        assert app.state.service.voices.list() == []
        assert list((settings.data_dir / "voices").iterdir()) == []

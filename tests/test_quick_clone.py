import json
import threading
import time

import httpx
import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import create_app
from asr_client import AsrClient
from config import Settings
from errors import ServiceError
from storage import write_json
from tests.conftest import ControlledBackend, wait_done


def quick_app(settings, handler=None):
    configured = settings.model_copy(update={"asr_base_url": "http://asr.test", "api_token": "quick-test-token"})
    app = create_app(configured, ControlledBackend())
    handler = handler or (
        lambda request: httpx.Response(
            200,
            json={"backend_ready": True}
            if request.url.path == "/health"
            else {"text": "这里是录音实际说出的文字。", "language": "Chinese"},
        )
    )
    app.state.service.asr = AsrClient(configured, httpx.MockTransport(handler))
    return app


HEADERS = {"Authorization": "Bearer quick-test-token"}


def stage(client, sample, filename=None, **data):
    return client.post(
        "/v1/voices/references",
        files={"file": (filename or sample.name, sample.read_bytes(), "audio/wav")},
        data=data,
        headers=HEADERS,
    )


def test_quick_clone_binds_audio_to_reviewed_text_and_reuses_existing_queue(settings, sample):
    calls = []

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"backend_ready": True})
        calls.append(request)
        return httpx.Response(200, json={"text": "原始识别文字。", "language": "Chinese"})

    app = quick_app(settings, handler)
    with TestClient(app) as client:
        assert (
            client.post("/v1/voices/references", files={"file": ("sample.wav", sample.read_bytes())}).status_code == 401
        )
        response = stage(client, sample, language="zh")
        assert response.status_code == 201, response.text
        draft = response.json()
        assert draft["ref_text"] == "原始识别文字。" and draft["transcription_state"] == "succeeded"
        assert draft["reference"]["sample_rate"] == settings.work_sample_rate
        assert draft["reference"]["channels"] == 1
        assert client.get(draft["sample_url"]).status_code == 401
        wav = client.get(draft["sample_url"], headers=HEADERS).content
        assert wav.startswith(b"RIFF") and wav in calls[0].content
        assert b'name="response_format"' in calls[0].content and b"json" in calls[0].content
        assert b'name="language"' in calls[0].content and b"zh" in calls[0].content
        data = {"draft_id": draft["draft_id"], "name": "快速复刻测试", "ref_text": "用户确认后的实际文字。"}
        registered = client.post("/v1/voices", data=data, headers=HEADERS)
        assert registered.status_code == 202, registered.text
        voice = registered.json()
        assert voice["mode"] == "icl" and voice["ref_text"] == data["ref_text"]
        assert voice["reference"]["processed_sha256"] == draft["reference"]["processed_sha256"]
        assert wait_done(app.state.service, voice["preparation_job_id"])["state"] == "succeeded"
        # Retrying a lost successful response does not register another voice or another GPU job.
        repeated = client.post("/v1/voices", data=data, headers=HEADERS)
        assert repeated.json()["voice_id"] == voice["voice_id"]
        assert len(client.get("/v1/voices", headers=HEADERS).json()["voices"]) == 1
        assert len(app.state.service.backend.preparations) == 1
        conflict = client.post("/v1/voices", data={**data, "ref_text": "另一段文字"}, headers=HEADERS)
        assert conflict.status_code == 409
        speech = client.post(
            "/v1/audio/speech",
            headers=HEADERS,
            json={
                "model": "qwen3-tts",
                "input": "这是新的试听句子。",
                "voice": voice["voice_id"],
                "response_format": "wav",
            },
        )
        assert speech.status_code == 200 and speech.content.startswith(b"RIFF"), speech.text[:300]
        assert client.delete(f"/v1/voices/references/{draft['draft_id']}", headers=HEADERS).status_code == 204
        assert client.get(draft["sample_url"], headers=HEADERS).status_code == 404
        assert app.state.service.voices.path(voice["voice_id"]).exists()


@pytest.mark.parametrize("failure", ["timeout", "unavailable", "empty", "malformed", "redirect", "large"])
def test_asr_failures_preserve_recording_for_manual_text_and_retry(settings, sample, failure):
    successful = False

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"backend_ready": True})
        if successful:
            return httpx.Response(200, json={"text": "重试成功后的文字。"})
        if failure == "timeout":
            raise httpx.ReadTimeout("controlled timeout", request=request)
        return {
            "unavailable": httpx.Response(503),
            "empty": httpx.Response(200, json={"text": " "}),
            "malformed": httpx.Response(200, content=b"not json"),
            "redirect": httpx.Response(307, headers={"Location": "http://other.test"}),
            "large": httpx.Response(200, content=b"x" * 65537),
        }[failure]

    app = quick_app(settings, handler)
    with TestClient(app) as client:
        draft = stage(client, sample).json()
        assert draft["transcription_state"] == "failed" and draft["asr_error"]
        assert client.get(draft["sample_url"], headers=HEADERS).status_code == 200
        successful = True
        retry = client.post(f"/v1/voices/references/{draft['draft_id']}/transcribe", headers=HEADERS)
        assert retry.status_code == 200 and retry.json()["ref_text"] == "重试成功后的文字。"
        response = client.post(
            "/v1/voices",
            headers=HEADERS,
            data={"draft_id": draft["draft_id"], "name": "手动修正", "ref_text": "手动确认的文字。"},
        )
        assert response.status_code == 202 and response.json()["mode"] == "icl"


def test_disabled_and_incompatible_backend_do_not_contact_asr(settings, sample):
    for backend in ["qwen-base", "qwen-custom", "qwen-combined-experimental"]:
        configured = settings.model_copy(
            update={"backend": backend, "asr_base_url": "" if backend == "qwen-base" else "http://asr.test"}
        )
        app = create_app(configured, ControlledBackend())

        def unexpected(request):
            raise AssertionError("Disabled quick clone must not contact upstream")

        app.state.service.asr = AsrClient(configured, httpx.MockTransport(unexpected))
        with TestClient(app) as client:
            assert client.get("/health").json()["features"]["quick_voice_clone"]["enabled"] is False
            response = client.post("/v1/voices/references", files={"file": ("test.wav", sample.read_bytes())})
            assert response.status_code == (403 if backend == "qwen-base" else 422)
            assert list(app.state.service.references.root.iterdir()) == []


def test_optional_asr_outage_does_not_change_tts_readiness(settings):
    app = quick_app(settings, lambda request: httpx.Response(503))
    with TestClient(app) as client:
        service = app.state.service
        service.backend.preload()
        deadline = time.monotonic() + 10
        while service.audio.status["state"] != "ready" and time.monotonic() < deadline:
            time.sleep(0.05)
        service.asr.probe()
        health = client.get("/health").json()
        assert health["features"]["quick_voice_clone"]["enabled"] is True
        assert health["features"]["quick_voice_clone"]["available"] is False
        assert client.get("/ready").status_code == 200


@pytest.mark.parametrize(
    "extension,codec",
    [("webm", "libopus"), ("m4a", "aac"), ("mp3", "libmp3lame"), ("ogg", "libopus"), ("flac", "flac")],
)
def test_browser_and_uploaded_formats_are_normalized(settings, sample, extension, codec):
    app = quick_app(settings)
    source = sample.with_suffix("." + extension)
    app.state.service.audio.run(
        [settings.ffmpeg_bin, "-nostdin", "-v", "error", "-y", "-i", str(sample), "-c:a", codec, str(source)]
    )
    with TestClient(app) as client:
        response = stage(client, source)
        assert response.status_code == 201, response.text
        draft = response.json()
        assert 1 <= draft["reference"]["duration_seconds"] <= 30
        assert draft["reference"]["sample_rate"] == 24000 and draft["reference"]["channels"] == 1


@pytest.mark.parametrize("kind", ["empty", "corrupt", "archive", "silent", "short", "long", "durationless_long"])
def test_invalid_recordings_leave_no_draft_or_voice(settings, sample, kind):
    filename = "input.wav"
    payload = sample.read_bytes()
    if kind in {"empty", "corrupt", "archive"}:
        payload = b"" if kind == "empty" else b"not an audio file"
        filename = "input.zip" if kind == "archive" else filename
    else:
        duration = 0.5 if kind == "short" else 31 if kind in {"long", "durationless_long"} else 2
        audio = (
            np.zeros(round(duration * 24000))
            if kind == "silent"
            else np.sin(np.arange(round(duration * 24000)) * 0.1) * 0.1
        )
        sf.write(sample, audio, 24000, subtype="PCM_16")
        payload = sample.read_bytes()
    app = quick_app(settings)
    if kind == "durationless_long":
        webm = sample.with_suffix(".webm")
        app.state.service.audio.run(
            [
                settings.ffmpeg_bin,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(sample),
                "-c:a",
                "libopus",
                "-f",
                "webm",
                "-live",
                "1",
                str(webm),
            ]
        )
        filename, payload = "recording.webm", webm.read_bytes()
    with TestClient(app) as client:
        response = client.post("/v1/voices/references", headers=HEADERS, files={"file": (filename, payload)})
        assert response.status_code in {400, 422}, response.text
        assert not list(app.state.service.references.root.iterdir())
        assert app.state.service.voices.list() == []


def test_reference_expiry_tampering_mode_and_source_guards(settings, sample):
    app = quick_app(settings)
    with TestClient(app) as client:
        draft = stage(client, sample).json()
        data = {"draft_id": draft["draft_id"], "name": "test", "ref_text": ""}
        assert client.post("/v1/voices", headers=HEADERS, data=data).status_code == 422
        assert (
            client.post(
                "/v1/voices", headers=HEADERS, data={**data, "ref_text": "test", "mode": "x_vector_only"}
            ).status_code
            == 422
        )
        assert client.post("/v1/voices", headers=HEADERS, data={"name": "test"}).status_code == 422
        assert (
            client.post(
                "/v1/voices", headers=HEADERS, data=data, files={"file": ("test.wav", sample.read_bytes())}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/voices", headers=HEADERS, data={**data, "draft_id": "../../outside", "ref_text": "test"}
            ).status_code
            == 404
        )
        path = app.state.service.references.sample(draft["draft_id"])
        original = path.read_bytes()
        path.write_bytes(b"modified")
        assert client.post("/v1/voices", headers=HEADERS, data={**data, "ref_text": "test"}).status_code == 422
        assert client.post(f"/v1/voices/references/{draft['draft_id']}/transcribe", headers=HEADERS).status_code == 422
        path.write_bytes(original)
        metadata = path.parent / "draft.json"
        doc = json.loads(metadata.read_text())
        doc["expires_at"] = 0
        write_json(metadata, doc)
        response = client.post("/v1/voices", headers=HEADERS, data={**data, "ref_text": "test"})
        assert response.status_code == 410 and not path.parent.exists()
        another = stage(client, sample).json()
        path = app.state.service.references.sample(another["draft_id"])
        doc = json.loads((path.parent / "draft.json").read_text())
        doc["expires_at"] = 0
        write_json(path.parent / "draft.json", doc)
        app.state.service.references.prune()
        assert not path.parent.exists() and app.state.service.voices.list() == []


def test_invalid_language_and_file_limit_rejected_before_asr(settings, sample):
    app = quick_app(settings)
    with TestClient(app) as client:
        assert stage(client, sample, language="unsupported").status_code == 422
        assert not list(app.state.service.references.root.iterdir())
    small = quick_app(settings.model_copy(update={"max_upload_bytes": 1024, "max_request_bytes": 2048}))
    with TestClient(small) as client:
        assert stage(client, sample).status_code == 413
        assert not list(small.state.service.references.root.iterdir())


def test_asr_concurrency_is_bounded_without_occupying_gpu_queue(settings, sample):
    entered, release = threading.Event(), threading.Event()

    def handler(request):
        entered.set()
        release.wait(5)
        return httpx.Response(200, json={"text": "测试。"})

    asr = AsrClient(settings.model_copy(update={"asr_base_url": "http://asr.test"}), httpx.MockTransport(handler))
    thread = threading.Thread(target=asr.transcribe, args=(sample,))
    thread.start()
    try:
        assert entered.wait(3)
        with pytest.raises(ServiceError) as caught:
            asr.transcribe(sample)
        assert caught.value.code == "asr_busy"
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()


def test_asr_root_normalization_and_tls_pair_configuration():
    assert Settings(asr_base_url=" http://asr.test:8001/ ").asr_base_url == "http://asr.test:8001"
    assert Settings(asr_base_url="   ").asr_base_url == ""
    assert Settings(tls_certfile="cert.pem", tls_keyfile="key.pem").tls_certfile == "cert.pem"
    for value in [
        "ftp://asr.test",
        "http://asr.test/v1",
        "http://user:password@asr.test",
        "http://asr.test?secret=1",
        "http://asr.test:bad",
    ]:
        with pytest.raises(ValidationError):
            Settings(asr_base_url=value)
    with pytest.raises(ValidationError):
        Settings(tls_certfile="cert.pem")
    with pytest.raises(ValidationError):
        Settings(tls_keyfile="key.pem")


def test_https_entrance_rejects_unsafe_or_non_root_links():
    assert Settings(https_public_url=" https://voice.example.test:8443/ ").https_public_url == "https://voice.example.test:8443"
    for value in ["http://voice.example.test", "https://user:secret@host", "https://host/path",
                  "https://host?token=secret", "https://host/#quick-clone", "https://host:bad"]:
        with pytest.raises(ValidationError):
            Settings(https_public_url=value)


def test_full_gpu_queue_keeps_one_registered_voice_and_a_retry_receipt(settings, sample):
    gate = threading.Event()
    configured = settings.model_copy(
        update={"asr_base_url": "http://asr.test", "queue_limit": 1, "preload_model": True}
    )
    app = create_app(configured, ControlledBackend(preload_gate=gate))
    app.state.service.asr = AsrClient(
        configured,
        httpx.MockTransport(lambda request: httpx.Response(200, json={"backend_ready": True, "text": "测试文字。"})),
    )
    with TestClient(app) as client:
        app.state.service.jobs.submit("narration", {})
        draft = stage(client, sample).json()
        data = {"draft_id": draft["draft_id"], "name": "队列满时的音色", "ref_text": "确认的文字。"}
        first = client.post("/v1/voices", data=data).json()
        assert first["conditioning_state"] == "failed" and first["error"]["type"] == "queue_full"
        assert first["preparation_job_id"] is None
        second = client.post("/v1/voices", data=data).json()
        assert second["voice_id"] == first["voice_id"]
        assert len(app.state.service.voices.list()) == 1


def test_manual_reference_skips_busy_asr_and_can_still_prepare_icl(settings, sample):
    def handler(request):
        assert request.url.path == "/health", "Manual mode must not submit an ASR request"
        return httpx.Response(200, json={"backend_ready": True})

    app = quick_app(settings, handler)
    with TestClient(app) as client:
        draft = stage(client, sample, transcribe="false").json()
        assert draft["transcription_state"] == "pending" and draft["asr_error"] is None
        voice = client.post(
            "/v1/voices",
            headers=HEADERS,
            data={"draft_id": draft["draft_id"], "name": "手动文字", "ref_text": "录音对应文字。"},
        ).json()
        assert voice["mode"] == "icl"
        assert wait_done(app.state.service, voice["preparation_job_id"])["state"] == "succeeded"

import hashlib
import io
import json
import stat
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qwen3_tts_openai.app import create_app
from qwen3_tts_openai.contracts import NarrationRequest
from qwen3_tts_openai.errors import ServiceError
from qwen3_tts_openai.storage import file_hash
from qwen3_tts_openai.synthesis_service import SynthesisService
from tests.conftest import ControlledBackend, wait_done


def package(audio, *, entries=None, extra=None, compression=zipfile.ZIP_DEFLATED):
    """Construct the interchange contract independently of the production writer."""
    entries = entries or [{"voice_id": "narrator", "name": "讲解音色", "ref_text": "参考录音对应文字。",
                           "mode": "icl", "audio": "voices/0000.wav", "sha256": hashlib.sha256(audio).hexdigest()}]
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", compression=compression) as archive:
        archive.writestr("manifest.json", json.dumps({"format": "qwen3-tts-voices", "version": 1, "voices": entries}))
        for path in dict.fromkeys(entry["audio"] for entry in entries):
            archive.writestr(path, audio)
        for path, data in (extra or {}).items():
            archive.writestr(path, data)
    return content.getvalue()


def two_entries(audio):
    first = {"voice_id": "narrator", "name": "讲解音色", "ref_text": "参考文字。", "mode": "icl",
             "audio": "voices/0000.wav", "sha256": hashlib.sha256(audio).hexdigest()}
    second = {**first, "voice_id": "温柔女声", "name": "温柔女声", "ref_text": "", "mode": "x_vector_only", "audio": "voices/0001.wav"}
    return [first, second]


def post_package(client, payload):
    return client.post("/v1/voices/import", files={"file": ("voices.zip", payload, "application/zip")})


def idle_voice(service, sample, ident):
    voice = service.register_voice(sample, ident, "参考文字。", voice_id=ident)
    service.jobs.cancel(voice["preparation_job_id"])
    return service.voices.get(ident)


def test_export_delete_import_roundtrip_preserves_audio_metadata_and_completed_artifacts(settings, sample):
    app = create_app(settings, ControlledBackend())
    with TestClient(app) as client:
        service = app.state.service
        originals = []
        for entry in two_entries(sample.read_bytes()):
            voice = service.register_voice(sample, entry["name"], entry["ref_text"], entry["mode"], entry["voice_id"])
            assert wait_done(service, voice["preparation_job_id"])["state"] == "succeeded"
            originals.append(service.voices.get(voice["voice_id"]))
        generated = service.submit(NarrationRequest(text="删除后仍保留已完成的配音。", voice_id="narrator", compatibility="generic"))
        assert wait_done(service, generated["job_id"])["state"] == "succeeded"
        artifact, _ = service.artifact(generated["job_id"], "full-wav")
        artifact_hash = file_hash(artifact)

        exported = client.post("/v1/voices/export", json={"voice_ids": ["narrator", "温柔女声", "narrator"]})
        assert exported.status_code == 200, exported.text
        assert exported.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["format"] == "qwen3-tts-voices" and manifest["version"] == 1
            assert len(manifest["voices"]) == 2
            for entry, original in zip(manifest["voices"], originals):
                assert set(entry) == {"voice_id", "name", "ref_text", "mode", "audio", "sha256"}
                assert {k: entry[k] for k in ("voice_id", "name", "ref_text", "mode")} == {k: original[k] for k in ("voice_id", "name", "ref_text", "mode")}
                assert archive.read(entry["audio"]) == sample.read_bytes()
        assert not list(settings.data_dir.glob(".voice-export-*"))

        removed = client.post("/v1/voices/delete", json={"voice_ids": [v["voice_id"] for v in originals]})
        assert removed.status_code == 200 and len(removed.json()["deleted"]) == 2
        assert service.voices.list() == [] and list(service.voices.root.iterdir()) == []
        assert client.get("/v1/voices/narrator/sample").status_code == 404
        assert file_hash(service.artifact(generated["job_id"], "full-wav")[0]) == artifact_hash

        imported = post_package(client, exported.content)
        assert imported.status_code == 201, imported.text
        for restored, original in zip(imported.json()["voices"], originals):
            assert restored["source_voice_id"] == restored["voice_id"] == original["voice_id"]
            assert restored["created_at"] > original["created_at"]
            assert wait_done(service, restored["preparation_job_id"])["state"] == "succeeded"
            assert client.get(f"/v1/voices/{restored['voice_id']}/sample").content == sample.read_bytes()
            plan = service.compile(NarrationRequest(text="恢复后可继续配音。", voice_id=restored["voice_id"]))
            assert plan["valid"] and plan["units"][0]["voice_snapshot"]["mode"] == original["mode"]
        assert not list(settings.data_dir.glob(".voice-import-*"))
        assert not list(settings.data_dir.glob(".voice-upload-*"))
    with TestClient(create_app(settings, ControlledBackend())) as restarted:
        assert {v["voice_id"] for v in restarted.get("/v1/voices").json()["voices"]} == {"narrator", "温柔女声"}
        assert restarted.get("/v1/voices/narrator/sample").content == sample.read_bytes()


def test_import_collision_renames_without_overwriting_existing_voice(settings, sample):
    app = create_app(settings, ControlledBackend())
    service = app.state.service
    with TestClient(app) as client:
        voice = service.register_voice(sample, "原始音色", "原始参考文字。", voice_id="narrator")
        assert wait_done(service, voice["preparation_job_id"])["state"] == "succeeded"
        original = service.voices.get("narrator")
        response = post_package(client, package(sample.read_bytes()))
        assert response.status_code == 201, response.text
        imported = response.json()["voices"][0]
        assert imported["source_voice_id"] == "narrator" and imported["voice_id"] == "narrator-2"
        assert service.voices.get("narrator") == original
        assert client.get("/v1/voices/narrator/sample").content == sample.read_bytes()
        assert wait_done(service, imported["preparation_job_id"])["state"] == "succeeded"


def test_invalid_second_recording_rolls_back_entire_import(settings, sample):
    entries = two_entries(sample.read_bytes())
    broken = b"not a WAV recording"
    entries[1]["sha256"] = hashlib.sha256(broken).hexdigest()
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format": "qwen3-tts-voices", "version": 1, "voices": entries}))
        archive.writestr(entries[0]["audio"], sample.read_bytes())
        archive.writestr(entries[1]["audio"], broken)
    app = create_app(settings, ControlledBackend())
    response = post_package(TestClient(app), content.getvalue())
    assert response.status_code == 400 and response.json()["error"]["type"] == "invalid_audio"
    assert app.state.service.voices.list() == [] and list(app.state.service.voices.root.iterdir()) == []
    assert app.state.service.jobs.list() == []


@pytest.mark.parametrize("fault", ["checksum", "audio-path", "voice-id", "blank-name", "blank-transcript", "duplicate-id", "shared-audio", "extra-path", "symlink", "duplicate-member", "version", "truncated", "non-zip"])
def test_invalid_archives_never_create_voices_or_escape_workspace(settings, sample, fault):
    entries, extra = two_entries(sample.read_bytes()), {}
    if fault == "checksum":
        entries[0]["sha256"] = "0" * 64
    elif fault == "audio-path":
        entries[0]["audio"] = "../../outside.wav"
    elif fault == "voice-id":
        entries[0]["voice_id"] = "../outside"
    elif fault == "blank-name":
        entries[0]["name"] = " "
    elif fault == "blank-transcript":
        entries[0]["ref_text"] = " "
    elif fault == "duplicate-id":
        entries[1]["voice_id"] = entries[0]["voice_id"]
    elif fault == "shared-audio":
        entries[1]["audio"] = entries[0]["audio"]
    elif fault == "extra-path":
        extra["../../outside.wav"] = b"unexpected"
    payload = package(sample.read_bytes(), entries=entries, extra=extra)
    if fault == "version":
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"format": "qwen3-tts-voices", "version": 99, "voices": entries}))
            for entry in entries:
                archive.writestr(entry["audio"], sample.read_bytes())
        payload = buffer.getvalue()
    elif fault in {"symlink", "duplicate-member"}:
        buffer = io.BytesIO(payload)
        with zipfile.ZipFile(buffer, "a") as archive:
            if fault == "symlink":
                member = zipfile.ZipInfo("outside-link")
                member.create_system = 3
                member.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(member, "../../outside.wav")
            else:
                with pytest.warns(UserWarning, match="Duplicate name"):
                    archive.writestr("manifest.json", json.dumps({"format": "qwen3-tts-voices", "version": 99, "voices": entries}))
        payload = buffer.getvalue()
    elif fault == "truncated":
        payload = payload[:len(payload) // 2]
    elif fault == "non-zip":
        payload = b"not a zip file"
    app = create_app(settings, ControlledBackend())
    response = post_package(TestClient(app), payload)
    assert response.status_code == 422, response.text
    assert app.state.service.voices.list() == []
    assert list(app.state.service.voices.root.iterdir()) == []
    assert not (settings.data_dir.parent / "outside.wav").exists()


def test_import_keeps_all_voices_when_preparation_queue_is_full(settings, sample):
    app = create_app(settings.model_copy(update={"queue_limit": 1}), ControlledBackend())
    service = app.state.service
    service.jobs.submit("model_preload", {})
    response = post_package(TestClient(app), package(sample.read_bytes(), entries=two_entries(sample.read_bytes())))
    assert response.status_code == 201, response.text
    voices = response.json()["voices"]
    assert len(voices) == 2 and len(service.voices.list()) == 2
    assert all(v["preparation_job_id"] is None and v["conditioning_state"] == "failed" and v["error"]["type"] == "queue_full" for v in voices)
    assert all(service.voices.path(v["voice_id"]).is_file() for v in voices)


def test_archive_limits_cover_compressed_and_uncompressed_uploads(settings, sample):
    settings = settings.model_copy(update={"max_voice_archive_bytes": 150000, "max_request_bytes": 96500, "max_upload_bytes": 96100})
    app = create_app(settings, ControlledBackend())
    client = TestClient(app)
    audio = sample.read_bytes()
    # A valid stored ZIP can exceed the ordinary request cap while fitting the archive cap.
    assert len(package(audio, compression=zipfile.ZIP_STORED)) < settings.max_voice_archive_bytes
    assert len(package(audio, compression=zipfile.ZIP_STORED)) > settings.max_request_bytes
    response = post_package(client, package(audio, compression=zipfile.ZIP_STORED))
    assert response.status_code == 201, response.text
    # The ZIP compresses well, but its two WAV members exceed the expanded cap.
    response = post_package(client, package(audio, entries=two_entries(audio)))
    assert response.status_code == 413 and response.json()["error"]["type"] == "voice_archive_too_large"
    assert len(app.state.service.voices.list()) == 1
    oversized = post_package(client, b"x" * (settings.max_voice_archive_bytes + 1))
    assert oversized.status_code == 413
    exported = client.post("/v1/voices/export", json={"voice_ids": ["narrator"]})
    assert exported.status_code == 200
    app.state.service.voices.audio.settings = settings.model_copy(update={"max_upload_bytes": 1024})
    too_big_reference = post_package(client, package(audio))
    assert too_big_reference.status_code == 413


@pytest.mark.parametrize("kind", ["voice_prepare", "narration"])
@pytest.mark.parametrize("state", ["queued", "running"])
def test_delete_rejects_entire_selection_if_any_active_job_uses_a_voice(settings, sample, kind, state):
    service = SynthesisService(settings.model_copy(update={"queue_limit": 100}), ControlledBackend())
    used = idle_voice(service, sample, "used")
    idle_voice(service, sample, "idle")
    target = service.jobs.submit(kind, {"voice": used} if kind == "voice_prepare" else {},
                                 {"units": [{"voice_id": "used"}]} if kind == "narration" else None)
    if state == "running":
        assert service.jobs.claim()["id"] == target["id"]
    for _ in range(55):
        service.jobs.submit("model_preload", {})
    before = service.voices.list()
    with pytest.raises(ServiceError) as caught:
        service.delete_voices(["idle", "used"])
    assert caught.value.code == "voices_in_use" and caught.value.status == 409
    assert caught.value.details == {"voice_ids": ["used"], "job_ids": [target["id"]]}
    assert service.voices.list() == before
    assert all(service.voices.path(ident).is_file() for ident in ("idle", "used"))
    service.jobs.cancel(target["id"])
    if state == "running":
        with pytest.raises(ServiceError, match="任务"):
            service.delete_voices(["used"])
        service.jobs.finish(target["id"], "cancelled")
    assert service.delete_voices(["idle", "used", "idle"])["deleted"] == ["idle", "used"]
    assert service.voices.list() == [] and list(service.voices.root.iterdir()) == []


def test_missing_voice_and_invalid_selections_have_no_side_effects(settings, sample):
    app = create_app(settings, ControlledBackend())
    original = idle_voice(app.state.service, sample, "narrator")
    client = TestClient(app)
    for endpoint in ("delete", "export"):
        for ids, status in (([], 422), (["narrator", "missing"], 404), (["narrator"] * 101, 422)):
            response = client.post("/v1/voices/" + endpoint, json={"voice_ids": ids})
            assert response.status_code == status
            assert app.state.service.voices.list() == [original]
    assert client.delete("/v1/voices/narrator").json() == {"deleted": ["narrator"]}
    assert client.delete("/v1/voices/narrator").status_code == 404
    assert not list(settings.data_dir.glob(".voice-export-*"))


def test_file_operations_restore_database_and_files_after_a_rename_failure(settings, sample, monkeypatch, tmp_path):
    service = SynthesisService(settings, ControlledBackend())
    source = tmp_path / "voices.zip"
    source.write_bytes(package(sample.read_bytes(), entries=two_entries(sample.read_bytes())))
    rename = Path.rename

    def fail_import(path, target):
        if Path(target).name == "温柔女声":
            raise OSError("controlled rename failure")
        return rename(path, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", fail_import)
        with pytest.raises(OSError, match="controlled"):
            service.import_voices(source)
    assert service.voices.list() == [] and list(service.voices.root.iterdir()) == []
    idle_voice(service, sample, "first")
    idle_voice(service, sample, "second")
    before = service.voices.list()

    def fail_delete(path, target):
        if path.name == "second":
            raise OSError("controlled rename failure")
        return rename(path, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", fail_delete)
        with pytest.raises(OSError, match="controlled"):
            service.delete_voices(["first", "second"])
    assert service.voices.list() == before
    assert all(service.voices.path(ident).is_file() for ident in ("first", "second"))


def test_submission_and_deletion_cannot_leave_a_queued_job_without_its_voice(settings, sample, monkeypatch):
    service = SynthesisService(settings, ControlledBackend())
    idle_voice(service, sample, "narrator")
    compiling, release, deleting = threading.Event(), threading.Event(), threading.Event()
    compile_request = service.compile

    def gated_compile(request):
        plan = compile_request(request)
        compiling.set()
        assert release.wait(5)
        return plan

    def delete():
        deleting.set()
        return service.delete_voices(["narrator"])

    monkeypatch.setattr(service, "compile", gated_compile)
    with ThreadPoolExecutor(max_workers=2) as pool:
        submitted = pool.submit(service.submit, NarrationRequest(text="并发提交测试。", voice_id="narrator"))
        try:
            assert compiling.wait(5)
            removed = pool.submit(delete)
            assert deleting.wait(5) and not removed.done()
        finally:
            release.set()
        assert submitted.result()["status"] == "queued"
        with pytest.raises(ServiceError) as caught:
            removed.result()
        assert caught.value.code == "voices_in_use"
    assert service.voices.path("narrator").is_file()


def test_old_preparation_result_cannot_change_a_reimported_voice_with_the_same_id(settings, sample):
    service = SynthesisService(settings, ControlledBackend())
    old = idle_voice(service, sample, "narrator")
    service.delete_voices(["narrator"])
    current = idle_voice(service, sample, "narrator")
    assert old["conditioning_key"] == current["conditioning_key"]
    service.voices.preparation_finished(old, {"type": "old_failure", "message": "historical job"})
    assert service.voices.get("narrator") == current


def test_file_endpoints_require_api_auth_and_presets_cannot_be_imported(settings, sample):
    app = create_app(settings.model_copy(update={"api_token": "test-only-token"}), ControlledBackend())
    client = TestClient(app)
    for path in ("/v1/voices/export", "/v1/voices/delete"):
        assert client.post(path, json={"voice_ids": ["narrator"]}).status_code == 401
    assert post_package(client, package(sample.read_bytes())).status_code == 401
    assert client.delete("/v1/voices/narrator").status_code == 401
    app = create_app(settings.model_copy(update={"backend": "qwen-custom"}), ControlledBackend())
    assert post_package(TestClient(app), package(sample.read_bytes())).status_code == 422
    assert app.state.service.voices.list() == []

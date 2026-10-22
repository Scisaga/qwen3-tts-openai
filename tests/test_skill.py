"""Portable Skill behavior against the real HTTP/MCP app with controlled test audio."""
import hashlib
import importlib.util
import io
import json
import os
import socket
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import soundfile as sf
import uvicorn

from qwen3_tts_openai.app import create_app
from tests.conftest import ControlledBackend

TEST_TOKEN = secrets.token_hex(16)

SKILL = Path(__file__).resolve().parents[1] / "skills" / "qwen3-tts-openai"
spec = importlib.util.spec_from_file_location("tts_skill", SKILL / "scripts" / "tts_skill.py")
skill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill)


def test_install_is_portable_idempotent_and_preserves_edited_copy(tmp_path):
    parent = tmp_path / "client skills"
    result = skill.install("codex", parent)
    target = parent / skill.SKILL_NAME
    assert result["skills"][0]["status"] == "installed"
    assert (target / "references" / "setup.md").is_file()
    assert (target / "references" / "usage.md").is_file()
    assert (target / "agents" / "openai.yaml").is_file()
    (target / "__pycache__").mkdir()
    (target / "__pycache__" / "ignored.pyc").write_bytes(b"cache")
    assert skill.install("codex", parent)["skills"][0]["status"] == "unchanged"
    copied = subprocess.run(
        [sys.executable, "-I", str(target / "scripts" / "tts_skill.py"), "install", "--client", "claude",
         "--skills-dir", str(tmp_path / "another client")],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert copied.returncode == 0, copied.stderr
    assert (tmp_path / "another client" / skill.SKILL_NAME / "SKILL.md").is_file()
    manifest = target / "SKILL.md"
    manifest.write_text("user's locally edited Skill")
    with pytest.raises(ValueError, match="differs"):
        skill.install("codex", parent)
    assert manifest.read_text() == "user's locally edited Skill"


def test_install_both_checks_conflicts_before_copying_either_client(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    existing = tmp_path / ".claude" / "skills" / skill.SKILL_NAME
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("existing Skill")
    with pytest.raises(ValueError, match="differs"):
        skill.install("both")
    assert not (tmp_path / ".agents" / "skills").exists()
    assert (existing / "SKILL.md").read_text() == "existing Skill"


@pytest.fixture
def skill_service(settings):
    settings = settings.model_copy(update={"api_token": TEST_TOKEN, "enable_mcp": True, "preload_model": True})
    backend = ControlledBackend()
    app = create_app(settings, backend)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", ws="none"))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started, "Isolated HTTP test service did not start"
        while not app.state.service.ready() and time.monotonic() < deadline:
            time.sleep(.01)
        assert app.state.service.ready(), app.state.service.health()
        yield url, app.state.service, backend
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        listener.close()


def test_installed_cli_real_http_mcp_and_verified_audio(tmp_path, sample, skill_service):
    url, service, backend = skill_service
    installed = tmp_path / "portable"
    skill.install("codex", installed)
    script = installed / skill.SKILL_NAME / "scripts" / "tts_skill.py"
    environment = {**os.environ, "QWEN_TTS_API_TOKEN": TEST_TOKEN}

    def cli(*args, expected=0):
        process = subprocess.run(
            [sys.executable, "-I", str(script), "--url", url, *map(str, args)],
            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30,
        )
        assert process.returncode == expected, process.stdout + process.stderr
        assert TEST_TOKEN not in process.stdout + process.stderr
        return json.loads(process.stdout)

    health = cli("doctor", "--mcp")
    assert health["inference_ready"] and health["api_accessible"]
    assert set(health["mcp_tools"]) == {"list_voices", "submit_narration", "get_job"}
    assert backend.calls == [] and backend.preparations == []

    with httpx.Client(base_url=url, headers={"Authorization": "Bearer " + TEST_TOKEN}, trust_env=False) as http:
        with sample.open("rb") as source:
            response = http.post("/v1/voices", files={"file": ("reference.wav", source, "audio/wav")},
                                 data={"name": "隔离测试音色", "ref_text": "参考音频的测试文字。", "mode": "icl"})
        assert response.status_code == 202
        voice = response.json()
    assert cli("job", voice["preparation_job_id"], "--wait", "--poll-interval", ".01")["status"] == "succeeded"
    assert cli("voices")["voices"][0]["conditioning_state"] == "ready"

    request = {"text": "保留  空格与换行。\n第二行原文。", "voice_id": voice["voice_id"], "compatibility": "generic",
               "language": "Chinese", "generation": {"seed": 11}, "speed": .95}
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    assert cli("compile", "--request", request_path)["valid"]
    assert backend.calls == []  # Preflight and MCP discovery must not generate audio.
    receipt = cli("submit", "--request", request_path)
    ident = receipt["job_id"]
    result = cli("job", ident, "--wait", "--poll-interval", ".01")
    assert result["status"] == "succeeded"
    assert service.jobs.get(ident)["request"]["input"]["text"] == request["text"]
    for format in ("mp3", "wav"):
        output = tmp_path / ("narration." + format)
        downloaded = cli("download", ident, "--artifact", "full-" + format, "--output", output)
        assert downloaded["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
        assert downloaded["bytes"] == output.stat().st_size > 100
    waveform, rate = sf.read(tmp_path / "narration.wav")
    assert rate == 24000 and len(waveform) > 0

    original = (tmp_path / "narration.mp3").read_bytes()
    with pytest.raises(FileExistsError):
        skill.Client(url, TEST_TOKEN).download(ident, "full-mp3", tmp_path / "narration.mp3")
    assert (tmp_path / "narration.mp3").read_bytes() == original

    before = len(service.jobs.list())
    request["voice_profile"] = {"instructions": "请用开心的语气朗读。"}
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    rejected = cli("submit", "--request", request_path, expected=2)
    assert not rejected["valid"]
    assert len(service.jobs.list()) == before


def test_auth_errors_redact_token_and_doctor_distinguishes_unready(skill_service, monkeypatch, capsys):
    url, service, backend = skill_service
    with pytest.raises(ValueError, match="HTTP 401"):
        skill.Client(url, "wrong-token").doctor()
    backend.health["state"] = "unloaded"
    client = skill.Client(url, TEST_TOKEN)
    assert client.doctor()["inference_ready"] is False
    assert not service.ready()
    monkeypatch.setenv("QWEN_TTS_API_TOKEN", "do-not-print")
    monkeypatch.setattr(sys, "argv", ["tts_skill.py", "voices"])
    def failure(args):
        raise ValueError("a response containing do-not-print")
    monkeypatch.setattr(skill, "run", failure)
    assert skill.main() == 1
    assert "do-not-print" not in capsys.readouterr().err


def test_wait_timeout_keeps_existing_job_and_its_id(monkeypatch):
    client = skill.Client("http://127.0.0.1:8000")
    monkeypatch.setattr(client, "job", lambda ident: {"job_id": ident, "status": "running", "progress": .5})
    result, code = client.wait_job("job-test", .001, .001)
    assert code == 3 and result["wait_timed_out"]
    assert result["job_id"] == "job-test" and result["status"] == "running"


def test_download_rejects_changed_url_and_removes_corrupt_partial(tmp_path, monkeypatch):
    client = skill.Client("http://127.0.0.1:8000", "private-test-token")
    artifact = {"artifact_id": "full-mp3", "url": "https://other.example.test/audio.mp3",
                "bytes": 3, "sha256": hashlib.sha256(b"abc").hexdigest(), "media_type": "audio/mpeg"}
    monkeypatch.setattr(client, "job", lambda ident: {"artifacts": [artifact]})
    output = tmp_path / "download.mp3"
    with pytest.raises(ValueError, match="URL"):
        client.download("job-test", "full-mp3", output)
    assert not output.exists()
    artifact["url"] = "/v1/jobs/job-test/artifacts/full-mp3"
    monkeypatch.setattr(client, "open", lambda *args, **kwargs: io.BytesIO(b"bad"))
    with pytest.raises(ValueError, match="verification failed"):
        client.download("job-test", "full-mp3", output)
    assert not output.exists()


def test_http_redirect_is_not_followed_with_credentials():
    seen = []
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/another-location")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        client = skill.Client(f"http://127.0.0.1:{server.server_port}", "private-test-token")
        with pytest.raises(ValueError, match="HTTP 302"):
            client.json("/artifact")
        assert seen == ["/artifact"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)

"""Exercise the production module entry point without loading a GPU model."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx


def test_module_entrypoint_serves_api_pages_and_repository_assets(tmp_path):
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {
        **os.environ, "HOST": "127.0.0.1", "PORT": str(port), "PRELOAD_MODEL": "0",
        "BACKEND": "qwen-base", "DATA_DIR": str(tmp_path / "data"),
        "HF_HOME": str(tmp_path / "models"), "HF_HUB_OFFLINE": "1",
        "API_TOKEN": "", "ADMIN_TOKEN": "", "ASR_BASE_URL": "",
        "GPU_DEVICE": "", "GPU_HOST_DEVICE": "", "CUDA_VISIBLE_DEVICES": "",
        "TLS_CERTFILE": "", "TLS_KEYFILE": "", "MIN_FREE_DISK_BYTES": "0",
    }
    with (tmp_path / "server.log").open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "qwen3_tts_openai"], cwd=root, env=environment,
            stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=2) as client:
                deadline = time.monotonic() + 15
                while True:
                    assert process.poll() is None, "Module entry point exited during startup"
                    try:
                        response = client.get("/health")
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    assert time.monotonic() < deadline, "Module entry point did not start"
                    time.sleep(.05)
                assert response.json()["alive"] is True
                assert response.json()["model"]["state"] == "unloaded"
                assert client.get("/ready").status_code == 503
                assert client.get("/v1/voices").json() == {"voices": []}
                for route in ("/", "/studio", "/voices", "/jobs", "/integrations/mcp", "/runtime"):
                    page = client.get(route)
                    assert page.status_code == 200 and 'id="voice-select"' in page.text
                for asset in ("app.js", "app.css", "quick-clone.js", "mcp.js"):
                    assert client.get("/static/" + asset).status_code == 200
                assert client.get("/examples/narration_director.json").json()["schema_version"] == 2
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

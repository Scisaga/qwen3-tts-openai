"""Explicit, isolated browser test server with controlled tones; not a production entry point."""
import os
from pathlib import Path

import uvicorn
import httpx

from qwen3_tts_openai.app import create_app
from qwen3_tts_openai.asr_client import AsrClient
from qwen3_tts_openai.config import Settings
from tests.conftest import ControlledBackend


if __name__ == "__main__":
    quick = os.getenv("UI_TEST_QUICK_CLONE") == "1"
    settings=Settings.from_env().model_copy(update={"data_dir":Path(os.environ["UI_TEST_DATA_DIR"]),"preload_model":False,"min_free_disk_bytes":0,"api_token":"ui-test-token", "asr_base_url":"http://asr.test" if quick else ""})
    if os.getenv("UI_TEST_PROGRESS") == "1":
        from tests.test_progress import ProgressBackend
        backend = ProgressBackend()
    else:
        backend = ControlledBackend(delay=.15)
    app = create_app(settings, backend)
    if quick:
        calls = 0
        def controlled_asr(request):
            global calls
            if request.url.path == "/health":
                return httpx.Response(200, json={"backend_ready": True})
            calls += 1
            if calls == 2:
                raise httpx.ReadTimeout("controlled browser-test timeout", request=request)
            return httpx.Response(200, json={"text": "这是一段浏览器测试录音。", "language": "Chinese"})
        app.state.service.asr = AsrClient(settings, httpx.MockTransport(controlled_asr))
    uvicorn.run(app,host="127.0.0.1",port=int(os.getenv("UI_TEST_PORT","12308")),
                ssl_certfile=settings.tls_certfile or None, ssl_keyfile=settings.tls_keyfile or None)

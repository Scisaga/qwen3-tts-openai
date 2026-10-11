from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx

from config import Settings
from errors import ServiceError

ASR_LANGUAGES = {"zh", "en", "ja", "ko", "de", "fr", "ru", "pt", "es", "it"}


class AsrClient:
    """Optional, bounded HTTP integration; never loads another model in the TTS process."""

    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self._transport = transport
        self._lock = threading.Lock()
        self._slot = threading.BoundedSemaphore(1)
        self._health = {
            "status": "unchecked" if settings.asr_base_url else "disabled",
            "available": False,
            "checked_at": None,
            "error": None,
        }

    def snapshot(self):
        with self._lock:
            return dict(self._health)

    def _set_health(self, available, error=None):
        with self._lock:
            self._health = {
                "status": "ready" if available else "unavailable",
                "available": available,
                "checked_at": time.time(),
                "error": error,
            }

    def probe(self):
        if not self.settings.asr_base_url:
            return self.snapshot()
        try:
            with httpx.Client(trust_env=False, timeout=3, follow_redirects=False, transport=self._transport) as client:
                response = client.get(self.settings.asr_base_url + "/health")
                response.raise_for_status()
                data = response.json()
            if not isinstance(data, dict) or data.get("backend_ready") is not True:
                raise ValueError("ASR backend is not ready")
            self._set_health(True)
        except (httpx.HTTPError, ValueError):
            self._set_health(
                False, {"type": "asr_unavailable", "message": "自动转写服务暂不可用，可重试或手动填写录音对应文字。"}
            )
        return self.snapshot()

    def transcribe(self, path: Path, language: str | None = None):
        if not self.settings.asr_base_url:
            raise ServiceError("quick_clone_disabled", "未配置 ASR_BASE_URL，快速复刻功能未启用。", 403)
        if language and language not in ASR_LANGUAGES:
            raise ServiceError("invalid_language", "请选择支持的录音语言，或使用自动识别。", 422)
        if not self._slot.acquire(blocking=False):
            raise ServiceError("asr_busy", "已有录音正在识别，请稍后重试或手动填写对应文字。", 429)
        try:
            data = {"response_format": "json"}
            if language:
                data["language"] = language
            timeout = httpx.Timeout(self.settings.asr_timeout_seconds, connect=5)
            with httpx.Client(
                trust_env=False, timeout=timeout, follow_redirects=False, transport=self._transport
            ) as client:
                with path.open("rb") as audio:
                    response = client.post(
                        self.settings.asr_base_url + "/v1/audio/transcriptions",
                        files={"file": ("reference.wav", audio, "audio/wav")},
                        data=data,
                    )
                if response.status_code != 200:
                    raise ServiceError("asr_unavailable", "自动转写服务暂不可用，可重试或手动填写录音对应文字。", 503)
                if len(response.content) > 64 * 1024:
                    raise ValueError("ASR response is too large")
                result = response.json()
            text = result.get("text") if isinstance(result, dict) else None
            if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                raise ServiceError("asr_invalid_response", "未识别到有效文字，请回听录音后重试或手动填写。", 502)
            self._set_health(True)
            detected = result.get("language")
            return {"ref_text": text.strip(), "language": detected if isinstance(detected, str) else None}
        except httpx.TimeoutException as exc:
            error = ServiceError("asr_timeout", "自动转写超时，录音已保留，可重试或手动填写对应文字。", 504)
            self._set_health(False, error.payload()["error"])
            raise error from exc
        except (httpx.HTTPError, ValueError) as exc:
            error = ServiceError("asr_unavailable", "无法读取自动转写结果，录音已保留，可重试或手动填写。", 503)
            self._set_health(False, error.payload()["error"])
            raise error from exc
        except ServiceError as exc:
            self._set_health(
                exc.code == "asr_invalid_response",
                None if exc.code == "asr_invalid_response" else exc.payload()["error"],
            )
            raise
        finally:
            self._slot.release()

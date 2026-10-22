from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from qwen3_tts_openai.audio_processing import AudioProcessor
from qwen3_tts_openai.errors import ServiceError
from qwen3_tts_openai.storage import digest, file_hash, write_json

REFERENCE_ID = re.compile(r"reference_[0-9a-f]{32}\Z")


class ReferenceStore:
    """Short-lived WAV drafts bind the reviewed transcript to exactly one recording."""

    def __init__(self, root: Path, audio: AudioProcessor, ttl_seconds: int):
        self.root, self.audio, self.ttl = root, audio, ttl_seconds
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._busy = set()

    def _directory(self, ident):
        if not REFERENCE_ID.fullmatch(ident):
            raise ServiceError("reference_not_found", "录音草稿不存在，请重新录制或上传。", 404)
        return self.root / ident

    def _read(self, ident):
        directory = self._directory(ident)
        try:
            doc = json.loads((directory / "draft.json").read_text())
        except FileNotFoundError as exc:
            raise ServiceError("reference_not_found", "录音草稿不存在或已过期，请重新录制或上传。", 404) from exc
        if doc["expires_at"] <= time.time() and ident not in self._busy:
            shutil.rmtree(directory)
            raise ServiceError("reference_expired", "录音草稿已过期，请重新录制或上传。", 410)
        return doc

    def create(self, source):
        with self._lock:
            self.prune()
            ident = "reference_" + uuid.uuid4().hex
            directory = self._directory(ident)
            directory.mkdir()
            try:
                metadata = self.audio.convert_reference(source, directory / "reference.wav")
                now = time.time()
                doc = {
                    "draft_id": ident,
                    "created_at": now,
                    "expires_at": now + self.ttl,
                    "reference": metadata,
                    "ref_text": "",
                    "language": None,
                    "transcription_state": "pending",
                    "asr_error": None,
                }
                write_json(directory / "draft.json", doc)
                return self.public(doc)
            except Exception:
                shutil.rmtree(directory)
                raise

    @staticmethod
    def public(doc):
        return {
            key: doc.get(key)
            for key in (
                "draft_id",
                "created_at",
                "expires_at",
                "reference",
                "ref_text",
                "language",
                "transcription_state",
                "asr_error",
            )
        } | {"sample_url": f"/v1/voices/references/{doc['draft_id']}/sample"}

    def transcribe(self, ident, asr, language=None):
        with self._lock:
            doc = self._read(ident)
            if ident in self._busy:
                raise ServiceError("reference_busy", "这段录音正在识别，请稍后重试。", 409)
            if doc.get("registration"):
                raise ServiceError("reference_consumed", "这段录音已保存为音色。", 409)
            self._busy.add(ident)
            path = self._directory(ident) / "reference.wav"
            if not path.is_file() or file_hash(path) != doc["reference"]["processed_sha256"]:
                self._busy.discard(ident)
                raise ServiceError("reference_corrupt", "录音草稿已损坏，请重新录制或上传。", 422)
        try:
            try:
                result = asr.transcribe(path, language)
                doc.update(result, transcription_state="succeeded", asr_error=None)
            except ServiceError as exc:
                if exc.code == "invalid_language":
                    raise
                # Keep the valid recording usable even when the optional upstream is down.
                doc.update(transcription_state="failed", asr_error=exc.payload()["error"])
            with self._lock:
                doc["expires_at"] = time.time() + self.ttl
                write_json(self._directory(ident) / "draft.json", doc)
                return self.public(doc)
        finally:
            with self._lock:
                self._busy.discard(ident)

    def register(self, ident, name, ref_text, register, voice_id=None):
        if not ref_text.strip():
            raise ServiceError(
                "invalid_reference_mode", "快速复刻需要与录音一致的文字，请确认识别结果或手动填写。", 422
            )
        voice_id = (voice_id or "").strip() or None
        key = digest({"name": name, "ref_text": ref_text, "mode": "icl", "voice_id": voice_id})
        with self._lock:
            doc = self._read(ident)
            if ident in self._busy:
                raise ServiceError("reference_busy", "这段录音正在识别，请等待识别完成。", 409)
            if receipt := doc.get("registration"):
                if receipt["key"] != key:
                    raise ServiceError("reference_consumed", "这段录音已保存为音色，请编辑已有音色或重新录制。", 409)
                return receipt["voice"]
            path = self._directory(ident) / "reference.wav"
            if not path.is_file() or file_hash(path) != doc["reference"]["processed_sha256"]:
                raise ServiceError("reference_corrupt", "录音草稿已损坏，请重新录制或上传。", 422)
            try:
                voice = register(path, name, ref_text, "icl", voice_id)
            except ServiceError as exc:
                voice = (exc.details or {}).get("voice")
                if not voice:
                    raise
            doc["registration"] = {"key": key, "voice": voice}
            write_json(self._directory(ident) / "draft.json", doc)
            return voice

    def sample(self, ident):
        with self._lock:
            self._read(ident)
            path = self._directory(ident) / "reference.wav"
            if not path.is_file():
                raise ServiceError("reference_not_found", "录音草稿不存在，请重新录制或上传。", 404)
            return path

    def delete(self, ident):
        with self._lock:
            self._read(ident)
            if ident in self._busy:
                raise ServiceError("reference_busy", "这段录音正在识别，请稍后清除。", 409)
            shutil.rmtree(self._directory(ident))

    def prune(self):
        with self._lock:
            for directory in self.root.iterdir():
                if not directory.is_dir() or not REFERENCE_ID.fullmatch(directory.name) or directory.name in self._busy:
                    continue
                try:
                    doc = json.loads((directory / "draft.json").read_text())
                    expired = doc["expires_at"] <= time.time()
                except (OSError, ValueError, KeyError):
                    expired = directory.stat().st_mtime + self.ttl <= time.time()
                if expired:
                    shutil.rmtree(directory)

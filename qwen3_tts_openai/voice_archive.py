"""Portable voice packages contain JSON and WAV only; conditioning is rebuilt locally."""
from __future__ import annotations

import shutil
import stat
import zipfile
import zlib
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from qwen3_tts_openai.contracts import StrictModel
from qwen3_tts_openai.errors import ServiceError
from qwen3_tts_openai.storage import file_hash
from qwen3_tts_openai.voice_store import validate_voice_id


class ArchivedVoice(StrictModel):
    voice_id: str
    name: str = Field(min_length=1, max_length=100)
    ref_text: str = Field(max_length=4000)
    mode: Literal["icl", "x_vector_only"]
    audio: str = Field(pattern=r"^voices/[0-9]{4}\.wav$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("voice_id")
    @classmethod
    def valid_id(cls, value):
        return validate_voice_id(value)

    @model_validator(mode="after")
    def metadata(self):
        if not self.name.strip() or self.mode == "icl" and not self.ref_text.strip():
            raise ValueError("Voice name and ICL reference text must not be blank")
        return self


class VoiceManifest(StrictModel):
    format: Literal["qwen3-tts-voices"]
    version: Literal[1]
    voices: list[ArchivedVoice] = Field(min_length=1, max_length=100)


def export_archive(store, identifiers, target: Path, limit: int):
    documents = [store.get(ident) for ident in dict.fromkeys(identifiers)]
    entries, total = [], 0
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, doc in enumerate(documents):
            path = store.path(doc["voice_id"], original=True)
            if not path.is_file() or file_hash(path) != doc["reference"]["original_sha256"]:
                raise ServiceError("reference_corrupt", f"参考录音缺失或已变更：{doc['voice_id']}，请检查后再导出。", 409)
            total += path.stat().st_size
            if total > limit:
                raise ServiceError("voice_archive_too_large", "所选录音超过音色包大小限制，请分批导出。", 413)
            entry = ArchivedVoice(**{k: doc[k] for k in ("voice_id", "name", "ref_text", "mode")},
                                  audio=f"voices/{index:04d}.wav", sha256=doc["reference"]["original_sha256"])
            archive.write(path, entry.audio)
            entries.append(entry)
        manifest = VoiceManifest(format="qwen3-tts-voices", version=1, voices=entries)
        manifest_bytes = manifest.model_dump_json(indent=2).encode("utf-8")
        if total + len(manifest_bytes) > limit:
            raise ServiceError("voice_archive_too_large", "所选录音超过音色包大小限制，请分批导出。", 413)
        archive.writestr("manifest.json", manifest_bytes)
    if target.stat().st_size > limit:
        raise ServiceError("voice_archive_too_large", "音色包超过大小限制，请分批导出。", 413)


def read_archive(source: Path, workspace: Path, settings):
    try:
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if len(members) > 101 or len(set(names)) != len(names) or "manifest.json" not in names:
                raise ValueError("Missing manifest, duplicate entries, or too many files")
            if any(member.is_dir() or stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1
                   or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED} for member in members):
                raise ValueError("Only regular, unencrypted WAV/JSON entries are accepted")
            if sum(member.file_size for member in members) > settings.max_voice_archive_bytes:
                raise ServiceError("voice_archive_too_large", "音色包解压后超过大小限制，请分批导入。", 413)
            manifest_info = archive.getinfo("manifest.json")
            if manifest_info.file_size > 2 * 1024 * 1024:
                raise ValueError("Manifest too large")
            manifest = VoiceManifest.model_validate_json(archive.read(manifest_info))
            expected = {"manifest.json", *(entry.audio for entry in manifest.voices)}
            if set(names) != expected or len(expected) != len(manifest.voices) + 1:
                raise ValueError("Unexpected files or shared audio paths")
            if len({entry.voice_id for entry in manifest.voices}) != len(manifest.voices):
                raise ValueError("Duplicate voice IDs")
            entries = []
            for index, entry in enumerate(manifest.voices):
                member = archive.getinfo(entry.audio)
                if member.file_size > settings.max_upload_bytes:
                    raise ServiceError("upload_too_large", "包内单条参考录音超过 MAX_UPLOAD_BYTES。", 413)
                # Use our own filenames, never archive paths, when writing to disk.
                target = workspace / f"{index}.wav"
                with archive.open(member) as incoming, target.open("wb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, 256 * 1024)
                if file_hash(target) != entry.sha256:
                    raise ValueError("Reference audio checksum mismatch")
                entries.append({**entry.model_dump(exclude={"audio", "sha256"}), "source": target})
            return entries
    except (zipfile.BadZipFile, ValueError, ValidationError, RuntimeError, NotImplementedError, zlib.error) as exc:
        raise ServiceError("invalid_voice_archive", "音色包无效、文件不完整或校验失败，请选择本服务导出的 ZIP 音色包。", 422) from exc

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unicodedata
from pathlib import Path

from qwen3_tts_openai.audio_processing import AudioProcessor
from qwen3_tts_openai.errors import ServiceError
from qwen3_tts_openai.storage import Database, digest


MAX_VOICE_ID_LENGTH = 40


def voice_id_from_name(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).casefold()
    # Collapse punctuation and spaces without discarding meaningful Chinese names.
    words = "".join(c if c.isalnum() else " " for c in text).split()
    slug = "-".join(words)[:MAX_VOICE_ID_LENGTH].rstrip("-")
    return "references-voice" if slug == "references" else slug or "voice"


def validate_voice_id(ident: str) -> str:
    if (not 1 <= len(ident) <= MAX_VOICE_ID_LENGTH or ident == "references"
            or ident != unicodedata.normalize("NFKC", ident).casefold()
            or ident.startswith("-") or ident.endswith("-") or "--" in ident
            or any(not (c.isalnum() or c == "-") for c in ident)):
        raise ServiceError("invalid_voice_id", "音色 ID 请使用 1–40 个小写字母、汉字或数字，可用单个连字符分隔，例如 narrator 或 讲解女声。", 422)
    return ident


class VoiceStore:
    def __init__(self, database: Database, root: Path, audio: AudioProcessor):
        self.database, self.root, self.audio = database, root, audio
        root.mkdir(parents=True, exist_ok=True)

    def register(self, source: Path, name: str, ref_text: str, mode: str | None, model: dict, voice_id: str | None = None):
        mode = mode or ("icl" if ref_text.strip() else "x_vector_only")
        if mode not in {"icl", "x_vector_only"} or mode == "icl" and not ref_text.strip():
            raise ServiceError("invalid_reference_mode", "ICL requires the matching ref_text; select x_vector_only explicitly without it")
        requested = validate_voice_id(voice_id.strip()) if voice_id and voice_id.strip() else None
        base = requested or voice_id_from_name(name)
        directory = None
        try:
            with tempfile.TemporaryDirectory(prefix=".upload-", dir=self.root) as temp:
                original, normalized = Path(temp) / "original.wav", Path(temp) / "reference.wav"
                shutil.copyfile(source, original)
                metadata = self.audio.validate_reference(original, normalized)
                with self.database.connect() as db:
                    # Allocate under the write lock so simultaneous same-name uploads stay distinct.
                    db.execute("BEGIN IMMEDIATE")
                    ident, number = base, 1
                    while db.execute("SELECT 1 FROM voices WHERE id=?", (ident,)).fetchone() or (self.root / ident).exists():
                        if requested:
                            raise ServiceError("voice_id_exists", "该音色 ID 已存在，请换一个名称。", 409)
                        number += 1
                        suffix = f"-{number}"
                        ident = base[:MAX_VOICE_ID_LENGTH - len(suffix)].rstrip("-") + suffix
                    doc = {"voice_id": ident, "name": name, "ref_text": ref_text, "mode": mode, "created_at": time.time(), "conditioning_state": "pending", "model": model, "reference": metadata, "warnings": (["x_vector_only uses speaker embedding without transcript/context; cloning quality needs listening review"] if mode == "x_vector_only" else [])}
                    doc["conditioning_key"] = self.conditioning_key(doc)
                    target = self.root / ident
                    Path(temp).rename(target)
                    directory = target
                    db.execute("INSERT INTO voices(id,document) VALUES(?,?)", (ident, json.dumps(doc, ensure_ascii=False)))
            return doc
        except Exception:
            if directory is not None:
                shutil.rmtree(directory)
            raise

    @staticmethod
    def conditioning_key(doc):
        return digest({"reference": doc["reference"], "ref_text": doc["ref_text"], "mode": doc["mode"], "model": doc["model"]})

    def get(self, ident):
        with self.database.connect() as db:
            row = db.execute("SELECT document FROM voices WHERE id=?", (ident,)).fetchone()
        if not row:
            raise ServiceError("voice_not_found", "Unknown registered voice_id; upload a WAV or supply an explicit voice_mapping", 404)
        return json.loads(row[0])

    def list(self):
        with self.database.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT document FROM voices ORDER BY rowid DESC")]

    def edit(self, ident, changes: dict):
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT document FROM voices WHERE id=?", (ident,)).fetchone()
            if not row:
                raise ServiceError("voice_not_found", "Unknown registered voice_id", 404)
            doc = json.loads(row[0])
            old_key = doc["conditioning_key"]
            doc.update({k: v for k, v in changes.items() if v is not None})
            if not doc["ref_text"].strip() and doc["mode"] == "icl":
                raise ServiceError("invalid_reference_mode", "Clearing ref_text requires mode=x_vector_only")
            doc["conditioning_key"] = self.conditioning_key(doc)
            if old_key != doc["conditioning_key"]:
                doc["conditioning_state"] = "pending"
                doc.pop("error", None)
            db.execute("UPDATE voices SET document=? WHERE id=?", (json.dumps(doc, ensure_ascii=False), ident))
        return doc, old_key != doc["conditioning_key"]

    def preparation_finished(self, snapshot, error=None, details=None):
        # A queued preparation of an old reference-text version must not overwrite a later edit.
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT document FROM voices WHERE id=?", (snapshot["voice_id"],)).fetchone()
            if not row:
                return
            doc = json.loads(row[0])
            if doc["conditioning_key"] != snapshot["conditioning_key"]:
                return
            doc["conditioning_state"] = "failed" if error else "ready"
            doc["error"] = error
            if details is not None:
                doc["conditioning_details"] = details
            db.execute("UPDATE voices SET document=? WHERE id=?", (json.dumps(doc, ensure_ascii=False), doc["voice_id"]))

    def path(self, ident, original=False):
        self.get(ident)
        return self.root / ident / ("original.wav" if original else "reference.wav")

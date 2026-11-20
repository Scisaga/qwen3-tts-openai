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
        return self.register_many([{"source": source, "name": name, "ref_text": ref_text,
                                    "mode": mode, "voice_id": voice_id}], model)[0]

    def register_many(self, entries, model, *, rename_conflicts=False):
        directories, documents = [], []
        try:
            with tempfile.TemporaryDirectory(prefix=".upload-", dir=self.root) as temp:
                prepared = []
                # Validate every recording before adding any voice to the library.
                for index, entry in enumerate(entries):
                    name, ref_text = entry["name"], entry["ref_text"]
                    mode = entry.get("mode") or ("icl" if ref_text.strip() else "x_vector_only")
                    if mode not in {"icl", "x_vector_only"} or mode == "icl" and not ref_text.strip():
                        raise ServiceError("invalid_reference_mode", "ICL requires matching ref_text")
                    requested = entry.get("voice_id")
                    requested = validate_voice_id(requested.strip()) if requested and requested.strip() else None
                    folder = Path(temp) / str(index)
                    folder.mkdir()
                    original, normalized = folder / "original.wav", folder / "reference.wav"
                    shutil.copyfile(entry["source"], original)
                    metadata = self.audio.validate_reference(original, normalized)
                    prepared.append((folder, requested, name, ref_text, mode, metadata))
                with self.database.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    for folder, requested, name, ref_text, mode, metadata in prepared:
                        base = requested or voice_id_from_name(name)
                        ident, number = base, 1
                        while db.execute("SELECT 1 FROM voices WHERE id=?", (ident,)).fetchone() or (self.root / ident).exists():
                            if requested and not rename_conflicts:
                                raise ServiceError("voice_id_exists", "该音色 ID 已存在，请换一个名称。", 409)
                            number += 1
                            suffix = f"-{number}"
                            ident = base[:MAX_VOICE_ID_LENGTH - len(suffix)].rstrip("-") + suffix
                        doc = {"voice_id": ident, "name": name, "ref_text": ref_text, "mode": mode, "created_at": time.time(), "conditioning_state": "pending", "model": model, "reference": metadata, "warnings": (["x_vector_only uses speaker embedding without transcript/context; cloning quality needs listening review"] if mode == "x_vector_only" else [])}
                        doc["conditioning_key"] = self.conditioning_key(doc)
                        target = self.root / ident
                        folder.rename(target)
                        directories.append(target)
                        db.execute("INSERT INTO voices(id,document) VALUES(?,?)", (ident, json.dumps(doc, ensure_ascii=False)))
                        documents.append(doc)
            return documents
        except Exception:
            for directory in directories:
                shutil.rmtree(directory)
            raise

    def delete_many(self, identifiers):
        identifiers = list(dict.fromkeys(identifiers))
        moved = []
        with tempfile.TemporaryDirectory(prefix=".delete-", dir=self.root) as temp:
            try:
                with self.database.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    for ident in identifiers:
                        if not db.execute("SELECT 1 FROM voices WHERE id=?", (ident,)).fetchone():
                            raise ServiceError("voice_not_found", f"音色不存在：{ident}", 404)
                    # Check the complete active queue, including jobs beyond the UI's latest 50.
                    busy_voices, busy_jobs = set(), []
                    for row in db.execute("SELECT id,kind,request,plan FROM jobs WHERE state IN ('queued','running')"):
                        if row["kind"] == "voice_prepare":
                            used = {json.loads(row["request"])["voice"]["voice_id"]}
                        else:
                            used = {unit["voice_id"] for unit in (json.loads(row["plan"]) if row["plan"] else {}).get("units", [])}
                        overlap = used.intersection(identifiers)
                        if overlap:
                            busy_voices.update(overlap)
                            busy_jobs.append(row["id"])
                    if busy_voices:
                        raise ServiceError("voices_in_use", "所选音色有任务正在使用，请等待任务完成或取消后重试。本次未删除任何音色。", 409,
                                           {"voice_ids": sorted(busy_voices), "job_ids": busy_jobs})
                    for index, ident in enumerate(identifiers):
                        source, target = self.root / ident, Path(temp) / str(index)
                        if source.exists():
                            source.rename(target)
                            moved.append((source, target))
                        db.execute("DELETE FROM voices WHERE id=?", (ident,))
            except Exception:
                for source, target in reversed(moved):
                    target.rename(source)
                raise
        return identifiers

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
            if (doc["conditioning_key"] != snapshot["conditioning_key"]
                    or doc["created_at"] != snapshot["created_at"]):
                return
            doc["conditioning_state"] = "failed" if error else "ready"
            doc["error"] = error
            if details is not None:
                doc["conditioning_details"] = details
            db.execute("UPDATE voices SET document=? WHERE id=?", (json.dumps(doc, ensure_ascii=False), doc["voice_id"]))

    def path(self, ident, original=False):
        self.get(ident)
        return self.root / ident / ("original.wav" if original else "reference.wav")

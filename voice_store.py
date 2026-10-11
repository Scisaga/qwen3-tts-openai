from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

from audio_processing import AudioProcessor
from errors import ServiceError
from storage import Database, digest


class VoiceStore:
    def __init__(self, database: Database, root: Path, audio: AudioProcessor):
        self.database, self.root, self.audio = database, root, audio
        root.mkdir(parents=True, exist_ok=True)

    def register(self, source: Path, name: str, ref_text: str, mode: str | None, model: dict):
        mode = mode or ("icl" if ref_text.strip() else "x_vector_only")
        if mode not in {"icl", "x_vector_only"} or mode == "icl" and not ref_text.strip():
            raise ServiceError("invalid_reference_mode", "ICL requires the matching ref_text; select x_vector_only explicitly without it")
        ident = "voice_" + uuid.uuid4().hex
        directory = self.root / ident
        directory.mkdir()
        try:
            original, normalized = directory / "original.wav", directory / "reference.wav"
            shutil.copyfile(source, original)
            metadata = self.audio.validate_reference(original, normalized)
            doc = {"voice_id": ident, "name": name, "ref_text": ref_text, "mode": mode, "created_at": time.time(), "conditioning_state": "pending", "model": model, "reference": metadata, "warnings": (["x_vector_only uses speaker embedding without transcript/context; cloning quality needs listening review"] if mode == "x_vector_only" else [])}
            doc["conditioning_key"] = self.conditioning_key(doc)
            self.save(doc)
            return doc
        except Exception:
            shutil.rmtree(directory)
            raise

    @staticmethod
    def conditioning_key(doc):
        return digest({"reference": doc["reference"], "ref_text": doc["ref_text"], "mode": doc["mode"], "model": doc["model"]})

    def save(self, doc):
        with self.database.connect() as db:
            db.execute("INSERT INTO voices(id,document) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document", (doc["voice_id"], json.dumps(doc, ensure_ascii=False)))

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

from __future__ import annotations

import json
import time
import uuid

from errors import ServiceError
from storage import Database

TERMINAL = {"succeeded", "failed", "cancelled"}


class JobStore:
    def __init__(self, database: Database, queue_limit: int):
        self.database, self.queue_limit = database, queue_limit

    def recover(self):
        with self.database.connect() as db:
            db.execute("UPDATE jobs SET state=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'failed' END, error=?, updated=? WHERE state='running'", (json.dumps({"type": "service_restarted", "message": "Service stopped during execution; resubmit to reuse verified caches"}), time.time()))

    def submit(self, kind, request, plan=None):
        ident, now = "job_" + uuid.uuid4().hex, time.time()
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT count(*) FROM jobs WHERE state='queued'").fetchone()[0]
            if count >= self.queue_limit:
                raise ServiceError("queue_full", "Queue is full; wait for another job to finish", 429)
            db.execute("INSERT INTO jobs(id,kind,state,request,plan,created,updated) VALUES(?,?,'queued',?,?,?,?)", (ident, kind, json.dumps(request, ensure_ascii=False), json.dumps(plan, ensure_ascii=False) if plan else None, now, now))
        return self.get(ident)

    def claim(self):
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY created,id LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET state='running',updated=? WHERE id=?", (time.time(), row[0]))
        return self.get(row[0])

    def get(self, ident):
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise ServiceError("job_not_found", "Unknown job_id", 404)
        result = dict(row)
        for key in ("request", "plan", "result", "error"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def list(self, limit=50):
        with self.database.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM jobs ORDER BY created DESC LIMIT ?", (limit,))]
        return [self.get(ident) for ident in ids]

    def cancelled(self, ident):
        with self.database.connect() as db:
            row = db.execute("SELECT cancel_requested FROM jobs WHERE id=?", (ident,)).fetchone()
        return bool(row and row[0])

    def cancel(self, ident):
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM jobs WHERE id=?", (ident,)).fetchone()
            if not row:
                raise ServiceError("job_not_found", "Unknown job_id", 404)
            if row[0] not in TERMINAL:
                db.execute("UPDATE jobs SET cancel_requested=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,updated=? WHERE id=?", (time.time(), ident))
        return self.get(ident)

    def progress(self, ident, value):
        with self.database.connect() as db:
            db.execute("UPDATE jobs SET progress=?,updated=? WHERE id=? AND state='running'", (min(0.99, value), time.time(), ident))

    def finish(self, ident, state, result=None, error=None):
        assert state in TERMINAL
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT cancel_requested FROM jobs WHERE id=?", (ident,)).fetchone()
            if row and row[0] and state == "succeeded":
                state, result = "cancelled", None
            db.execute("UPDATE jobs SET state=?,progress=?,result=?,error=?,updated=? WHERE id=?", (state, 1 if state == "succeeded" else 0, json.dumps(result, ensure_ascii=False) if result else None, json.dumps(error, ensure_ascii=False) if error else None, time.time(), ident))

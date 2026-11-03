"""Durable start receipts. An interrupted PENDING command is never replayed."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import sqlite3
from pathlib import Path

class StartReceipts:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS start_receipt (command_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def digest(payload):
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()

    def get(self, command_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM start_receipt WHERE command_id=?", (command_id,)).fetchone()
        if row is None:
            return {"command_id": command_id, "status": "not_found"}
        return {"command_id": command_id, "status": row["status"], "payload_hash": row["payload_hash"],
                "result": json.loads(row["result"]) if row["result"] else None, "error": row["error"]}

    def begin(self, command_id, payload_hash):
        with self.connect() as db:
            db.execute("INSERT INTO start_receipt(command_id,payload_hash,status) VALUES(?,?,'pending')", (command_id, payload_hash))

    def finish(self, command_id, status, result=None, error=None):
        with self.connect() as db:
            db.execute("UPDATE start_receipt SET status=?,result=?,error=? WHERE command_id=?",
                       (status, json.dumps(result, ensure_ascii=False, allow_nan=False) if result is not None else None, error, command_id))

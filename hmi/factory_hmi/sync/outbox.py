"""Crash-safe local upload outbox for execution bundles."""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


OUTBOX_STATES = {
    "local_completed",
    "uploading",
    "validating",
    "pending_review",
    "approved",
    "uploaded",
    "rejected",
    "sync_failed",
    "archived", "awaiting_classification", "needs_verification", "processing_failed",
}
TERMINAL_STATES = {"approved", "uploaded", "rejected", "archived", "awaiting_classification", "needs_verification", "processing_failed"}


@dataclass(frozen=True)
class OutboxItem:
    execution_uuid: str
    test_id: str
    bundle_path: str
    manifest: dict[str, Any]
    state: str
    attempts: int
    next_attempt_at: float
    last_error: str
    submission_id: str
    upload_id: str
    next_chunk_index: int
    review_reason: str
    remote_state: str
    created_at: float
    updated_at: float
    execution_code: str = ""
    submission_owner: str = ""
    submission_revision: int = 0
    submission_payload: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Outbox:
    """SQLite-backed queue that survives gateway and workstation restarts."""

    def __init__(
        self,
        path: Path | str,
        *,
        recover_interrupted: bool = True,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize(recover_interrupted=recover_interrupted)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self, *, recover_interrupted: bool) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS execution_outbox (
                    execution_uuid TEXT PRIMARY KEY,
                    test_id TEXT NOT NULL,
                    bundle_path TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    submission_id TEXT NOT NULL DEFAULT '',
                    upload_id TEXT NOT NULL DEFAULT '',
                    next_chunk_index INTEGER NOT NULL DEFAULT 0,
                    review_reason TEXT NOT NULL DEFAULT '',
                    remote_state TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS execution_outbox_due
                ON execution_outbox(state, next_attempt_at);
                CREATE TABLE IF NOT EXISTS legacy_summary_recovery (
                    execution_uuid TEXT NOT NULL REFERENCES execution_outbox(execution_uuid),
                    payload_sha256 TEXT NOT NULL,
                    frozen_payload TEXT NOT NULL,
                    submission_owner TEXT NOT NULL,
                    submission_revision INTEGER NOT NULL,
                    rejection_status INTEGER NOT NULL CHECK(rejection_status = 422),
                    rejection_error TEXT NOT NULL,
                    reconciled_at REAL NOT NULL,
                    PRIMARY KEY(execution_uuid, payload_sha256)
                );
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(execution_outbox)")}
            if "execution_code" not in columns:
                connection.execute("ALTER TABLE execution_outbox ADD COLUMN execution_code TEXT NOT NULL DEFAULT ''")
            if "submission_owner" not in columns:
                connection.execute("ALTER TABLE execution_outbox ADD COLUMN submission_owner TEXT NOT NULL DEFAULT ''")
            if "submission_revision" not in columns:
                connection.execute("ALTER TABLE execution_outbox ADD COLUMN submission_revision INTEGER NOT NULL DEFAULT 0")
            if "submission_payload" not in columns:
                connection.execute("ALTER TABLE execution_outbox ADD COLUMN submission_payload TEXT NOT NULL DEFAULT ''")
            if recover_interrupted:
                self._recover_interrupted(connection)

    @staticmethod
    def _recover_interrupted(connection: sqlite3.Connection) -> int:
        # An interrupted upload remains resumable from its recorded chunk.
        cursor = connection.execute(
            """
            UPDATE execution_outbox
            SET state = 'sync_failed',
                next_attempt_at = 0,
                last_error = CASE
                    WHEN last_error = '' THEN 'uploader restarted'
                    ELSE last_error
                END
            WHERE state IN ('uploading', 'validating')
            """
        )
        return max(0, cursor.rowcount)

    def recover_interrupted(self) -> int:
        """Make work left by a stopped uploader eligible for resumption."""
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._recover_interrupted(connection)

    def import_from(self, source_path: Path | str) -> int:
        """Copy legacy outbox rows without overwriting existing global rows."""
        source = Path(source_path).expanduser().resolve()
        if source == self.path or not source.is_file():
            return 0
        columns = (
            "execution_uuid",
            "test_id",
            "bundle_path",
            "manifest_json",
            "state",
            "attempts",
            "next_attempt_at",
            "last_error",
            "submission_id",
            "upload_id",
            "next_chunk_index",
            "review_reason",
            "remote_state",
            "created_at",
            "updated_at",
            "execution_code",
        )
        column_list = ", ".join(columns)
        with self._lock:
            connection = self._connect()
            attached = False
            try:
                connection.execute(
                    "ATTACH DATABASE ? AS legacy_outbox",
                    (str(source),),
                )
                attached = True
                available = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA legacy_outbox.table_info(execution_outbox)"
                    )
                }
                missing = set(columns) - available - {"execution_code"}
                if missing:
                    raise ValueError(
                        f"legacy outbox is missing columns: {sorted(missing)}"
                    )
                connection.execute("BEGIN IMMEDIATE")
                source_columns = ", ".join(
                    name if name in available else "'' AS execution_code"
                    for name in columns
                )
                cursor = connection.execute(
                    f"""
                    INSERT OR IGNORE INTO execution_outbox ({column_list})
                    SELECT {source_columns}
                    FROM legacy_outbox.execution_outbox
                    """
                )
                connection.commit()
                return max(0, cursor.rowcount)
            finally:
                if connection.in_transaction:
                    connection.rollback()
                if attached:
                    connection.execute("DETACH DATABASE legacy_outbox")
                connection.close()

    @staticmethod
    def _item(row: sqlite3.Row) -> OutboxItem:
        return OutboxItem(
            execution_uuid=str(row["execution_uuid"]),
            test_id=str(row["test_id"]),
            bundle_path=str(row["bundle_path"]),
            manifest=json.loads(str(row["manifest_json"])),
            state=str(row["state"]),
            attempts=int(row["attempts"]),
            next_attempt_at=float(row["next_attempt_at"]),
            last_error=str(row["last_error"]),
            submission_id=str(row["submission_id"]),
            upload_id=str(row["upload_id"]),
            next_chunk_index=int(row["next_chunk_index"]),
            review_reason=str(row["review_reason"]),
            remote_state=str(row["remote_state"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            execution_code=str(row["execution_code"]),
            submission_owner=str(row["submission_owner"]),
            submission_revision=int(row["submission_revision"]),
            submission_payload=str(row["submission_payload"]),
        )

    def enqueue(
        self,
        manifest: Mapping[str, Any],
        bundle_path: Path | str,
    ) -> OutboxItem:
        if manifest.get("recording_scope") == "local" and manifest.get("archive_protocol") != "reliability_v1_csv":
            raise ValueError("仅本地记录没有平台测试上下文，不能加入上传或审批队列。")
        execution_uuid = str(manifest.get("execution_uuid") or "").strip()
        test_id = str(manifest.get("test_id") or "").strip()
        path = Path(bundle_path).expanduser().resolve()
        if not execution_uuid or not test_id:
            raise ValueError("manifest execution_uuid and test_id are required")
        if not path.is_file():
            raise FileNotFoundError(path)
        now = time.time()
        payload = json.dumps(
            dict(manifest),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM execution_outbox WHERE execution_uuid=?", (execution_uuid,)).fetchone()
            if existing and (existing["state"] in {"approved", "uploaded", "pending_review", "archived", "awaiting_classification"} or existing["remote_state"] == "PENDING"):
                return self._item(existing)
            connection.execute(
                """
                INSERT INTO execution_outbox (
                    execution_uuid, test_id, bundle_path, manifest_json,
                    state, next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'local_completed', 0, ?, ?)
                ON CONFLICT(execution_uuid) DO UPDATE SET
                    test_id = excluded.test_id,
                    bundle_path = excluded.bundle_path,
                    manifest_json = excluded.manifest_json,
                    submission_payload = CASE WHEN execution_outbox.state IN ('rejected','needs_verification') THEN '' ELSE execution_outbox.submission_payload END,
                    state = CASE
                        WHEN execution_outbox.state IN ('approved', 'uploaded', 'pending_review')
                            THEN execution_outbox.state
                        ELSE 'local_completed'
                    END,
                    next_attempt_at = 0,
                    last_error = '',
                    updated_at = excluded.updated_at
                """,
                (execution_uuid, test_id, str(path), payload, now, now),
            )
        item = self.get(execution_uuid)
        assert item is not None
        return item

    def get(self, execution_uuid: str) -> OutboxItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM execution_outbox WHERE execution_uuid = ?",
                (execution_uuid,),
            ).fetchone()
        return self._item(row) if row is not None else None

    def get_by_test_id(self, test_id: str) -> OutboxItem | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM execution_outbox
                WHERE test_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (test_id,),
            ).fetchone()
        return self._item(row) if row is not None else None

    def record_central_analysis(self, execution_uuid: str, analysis: Mapping[str, Any]) -> OutboxItem:
        """Cache the authenticated server analysis without changing frozen requests."""
        encoded = json.dumps(dict(analysis), ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("中央分析超出受限元数据大小")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT manifest_json FROM execution_outbox WHERE execution_uuid=?", (execution_uuid,)).fetchone()
            if row is None:
                raise KeyError(execution_uuid)
            manifest = json.loads(row["manifest_json"])
            if manifest.get("central_analysis") != dict(analysis):
                manifest["central_analysis"] = dict(analysis)
                manifest["central_analysis_received_at"] = time.time()
                connection.execute("UPDATE execution_outbox SET manifest_json=?,updated_at=? WHERE execution_uuid=?",
                    (json.dumps(manifest, ensure_ascii=False, sort_keys=True), time.time(), execution_uuid))
        item = self.get(execution_uuid)
        assert item is not None
        return item

    def recover_rejected_legacy_summary(self, expected: OutboxItem, error: str) -> OutboxItem:
        """Keep the exact old request after a 422 and successful read-only receipt lookup.

        This only unlocks human verification. It never uploads a CSV or removes
        the old request; enqueue replaces the active payload after confirmation.
        """
        encoded = expected.submission_payload
        payload = json.loads(encoded or "{}")
        if (set(payload) != {"summary", "expected_revision", "reason"}
                or not isinstance(payload["summary"], dict)
                or payload["summary"].get("execution_uuid") != expected.execution_uuid):
            raise ValueError("旧摘要请求身份或格式不一致，不能恢复")
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM execution_outbox WHERE execution_uuid=?", (expected.execution_uuid,)).fetchone()
            if row is None:
                raise KeyError(expected.execution_uuid)
            if (row["submission_payload"] != encoded or row["submission_owner"] != expected.submission_owner
                    or row["submission_revision"] != expected.submission_revision
                    or row["state"] not in {"local_completed", "sync_failed"}
                    or row["submission_id"] or row["upload_id"] or row["remote_state"]
                    or json.loads(row["manifest_json"]).get("archive_protocol")):
                raise ValueError("旧请求状态已变化或已有平台回执，请刷新核对")
            connection.execute("""INSERT OR IGNORE INTO legacy_summary_recovery
                (execution_uuid,payload_sha256,frozen_payload,submission_owner,submission_revision,
                 rejection_status,rejection_error,reconciled_at) VALUES(?,?,?,?,?,422,?,?)""",
                (expected.execution_uuid,digest,encoded,expected.submission_owner,expected.submission_revision,str(error)[:4096],time.time()))
            connection.execute("""UPDATE execution_outbox SET state='needs_verification',next_attempt_at=0,
                last_error=?,review_reason=?,updated_at=? WHERE execution_uuid=?""",
                (str(error)[:4096],"旧摘要已拒收且未查到平台记录；请重新核实后归档原始 CSV",time.time(),expected.execution_uuid))
        item = self.get(expected.execution_uuid)
        assert item is not None
        return item

    def legacy_summary_recovery_verified(self, item: OutboxItem) -> bool:
        digest = hashlib.sha256(item.submission_payload.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            return connection.execute("""SELECT 1 FROM legacy_summary_recovery
                WHERE execution_uuid=? AND payload_sha256=? AND frozen_payload=?
                  AND submission_owner=? AND submission_revision=? AND rejection_status=422""",
                (item.execution_uuid,digest,item.submission_payload,item.submission_owner,item.submission_revision)).fetchone() is not None

    def list(self, *, limit: int = 100) -> list[OutboxItem]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM execution_outbox
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [self._item(row) for row in rows]

    def due(self, *, now: float | None = None, limit: int = 10, owner: str | None = None) -> list[OutboxItem]:
        current = time.time() if now is None else float(now)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM execution_outbox
                WHERE state IN ('local_completed', 'sync_failed', 'validating',
                                'pending_review', 'awaiting_classification')
                  AND next_attempt_at <= ?
                  AND (? IS NULL OR submission_owner = ?)
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (current, owner, owner, max(1, min(int(limit), 100))),
            ).fetchall()
        return [self._item(row) for row in rows]

    def update(self, execution_uuid: str, **changes: Any) -> OutboxItem:
        allowed = {
            "state",
            "attempts",
            "next_attempt_at",
            "last_error",
            "submission_id",
            "upload_id",
            "next_chunk_index",
            "review_reason",
            "remote_state",
            "execution_code",
            "submission_owner",
            "submission_revision",
            "submission_payload",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported outbox fields: {sorted(unknown)}")
        if "state" in changes and changes["state"] not in OUTBOX_STATES:
            raise ValueError(f"unsupported outbox state: {changes['state']}")
        if not changes:
            item = self.get(execution_uuid)
            if item is None:
                raise KeyError(execution_uuid)
            return item
        changes["updated_at"] = time.time()
        assignments = ", ".join(f"{name} = ?" for name in changes)
        values = [changes[name] for name in changes]
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"UPDATE execution_outbox SET {assignments} WHERE execution_uuid = ?",
                (*values, execution_uuid),
            )
            if cursor.rowcount != 1:
                raise KeyError(execution_uuid)
        item = self.get(execution_uuid)
        assert item is not None
        return item

    def fail(
        self,
        execution_uuid: str,
        error: str,
        *,
        base_delay_s: float = 5.0,
        max_delay_s: float = 900.0,
        random_source: random.Random | None = None,
    ) -> OutboxItem:
        item = self.get(execution_uuid)
        if item is None:
            raise KeyError(execution_uuid)
        attempts = item.attempts + 1
        delay = min(max_delay_s, base_delay_s * (2 ** min(attempts - 1, 10)))
        jitter = (random_source or random).uniform(0.5, 1.5)
        return self.update(
            execution_uuid,
            state="sync_failed",
            attempts=attempts,
            next_attempt_at=time.time() + delay * jitter,
            last_error=str(error)[:4096],
        )

    def retry(self, execution_uuid: str) -> OutboxItem:
        item = self.get(execution_uuid)
        if item is None:
            raise KeyError(execution_uuid)
        if item.state in {"approved", "uploaded"}:
            return item
        if item.state == "awaiting_classification":
            return self.update(execution_uuid, next_attempt_at=0, last_error="")
        if item.state in {"rejected", "needs_verification"} and item.manifest.get("archive_protocol") == "reliability_v1_csv":
            raise ValueError("驳回后请重新核实并提交")
        return self.update(
            execution_uuid,
            state="local_completed",
            submission_payload="" if item.state == "rejected" else item.submission_payload,
            next_attempt_at=0,
            last_error="",
        )


__all__ = [
    "OUTBOX_STATES",
    "TERMINAL_STATES",
    "Outbox",
    "OutboxItem",
]

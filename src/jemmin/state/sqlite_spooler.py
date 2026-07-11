from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import sqlite3
from pathlib import Path

from jemmin.models import ReviewRequest, ReviewState
from jemmin.state.statemachine import can_transition


@dataclass(slots=True)
class JobCreationOutcome:
    created: bool
    duplicate: bool
    existing_state: str | None = None


class SQLiteJobStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=3000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_requests (
                    request_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    project_profile TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    trigger_intent TEXT NOT NULL DEFAULT 'active_intent',
                    target_file TEXT NOT NULL,
                    git_revision TEXT NOT NULL,
                    base_file_hash TEXT NOT NULL,
                    diff_text TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_review_requests_idempotency_key ON review_requests(idempotency_key)"
            )
            # 마이그레이션: 기존 DB에 trigger_intent 컬럼이 없으면 추가
            existing_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(review_requests)").fetchall()
            }
            if "trigger_intent" not in existing_columns:
                connection.execute(
                    "ALTER TABLE review_requests ADD COLUMN trigger_intent TEXT NOT NULL DEFAULT 'active_intent'"
                )

    def create_request(self, request: ReviewRequest) -> JobCreationOutcome:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO review_requests (
                    request_id, idempotency_key, project_id, project_profile,
                    trigger, trigger_intent, target_file, git_revision, base_file_hash,
                    diff_text, metadata_json, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.idempotency_key,
                    request.project_id,
                    request.project_profile,
                    request.trigger,
                    request.trigger_intent,
                    request.target_file,
                    request.git_revision,
                    request.base_file_hash,
                    request.diff_text,
                    json.dumps(request.metadata, ensure_ascii=True),
                    ReviewState.QUEUED.value,
                ),
            )
            if cursor.rowcount == 0:
                existing = connection.execute(
                    "SELECT request_id, state FROM review_requests WHERE idempotency_key = ?",
                    (request.idempotency_key,),
                ).fetchone()
                existing_state = None if existing is None else str(existing["state"])
                if existing is not None:
                    self._append_event(
                        connection,
                        str(existing["request_id"]),
                        "review.duplicate_ignored",
                        {
                            "duplicate_request_id": request.request_id,
                            "idempotency_key": request.idempotency_key,
                            "existing_state": existing_state,
                        },
                    )
                return JobCreationOutcome(
                    created=False,
                    duplicate=True,
                    existing_state=existing_state,
                )
            self._append_event(connection, request.request_id, "review.request", {"state": ReviewState.QUEUED.value})
            return JobCreationOutcome(
                created=True,
                duplicate=False,
                existing_state=ReviewState.QUEUED.value,
            )

    def get_request(self, request_id: str) -> ReviewRequest | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM review_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        return ReviewRequest(
            request_id=row["request_id"],
            idempotency_key=row["idempotency_key"],
            project_id=row["project_id"],
            project_profile=row["project_profile"],
            trigger=row["trigger"],
            trigger_intent=row["trigger_intent"],
            target_file=row["target_file"],
            git_revision=row["git_revision"],
            base_file_hash=row["base_file_hash"],
            diff_text=row["diff_text"],
            metadata=json.loads(row["metadata_json"]),
        )

    def transition_state(
        self,
        request_id: str,
        from_state: ReviewState,
        to_state: ReviewState,
        reason: str | None = None,
        result_code: str | None = None,
    ) -> None:
        if not can_transition(from_state, to_state):
            raise ValueError(f"invalid state transition: {from_state.value} -> {to_state.value}")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE review_requests
                SET state = ?, updated_at = CURRENT_TIMESTAMP
                WHERE request_id = ? AND state = ?
                """,
                (to_state.value, request_id, from_state.value),
            )
            if cursor.rowcount == 0:
                current = connection.execute(
                    "SELECT state FROM review_requests WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if current is None:
                    raise KeyError(request_id)
                raise ValueError(f"current state mismatch: expected {from_state.value}, got {current['state']}")
            self._append_event(
                connection,
                request_id,
                "review.state_changed",
                {
                    "from": from_state.value,
                    "to": to_state.value,
                    "reason": reason,
                    "result_code": result_code,
                },
            )

    def append_event(self, request_id: str, event_type: str, payload: dict) -> None:
        with self._connection() as connection:
            self._append_event(connection, request_id, event_type, payload)

    def _append_event(
        self,
        connection: sqlite3.Connection,
        request_id: str,
        event_type: str,
        payload: dict,
    ) -> None:
        connection.execute(
            "INSERT INTO review_events (request_id, event_type, payload_json) VALUES (?, ?, ?)",
            (request_id, event_type, json.dumps(payload, ensure_ascii=True)),
        )

    def list_recoverable_jobs(self) -> list[ReviewRequest]:
        recoverable = {
            ReviewState.QUEUED.value,
            ReviewState.CONTEXT_READY.value,
            ReviewState.ANALYZING.value,
            ReviewState.PATCH_PROPOSED.value,
        }
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT request_id FROM review_requests WHERE state IN (?, ?, ?, ?)",
                tuple(recoverable),
            ).fetchall()
        return [self.get_request(row["request_id"]) for row in rows if self.get_request(row["request_id"]) is not None]

    def mark_terminal(
        self,
        request_id: str,
        final_state: ReviewState,
        reason: str | None = None,
        result_code: str | None = None,
    ) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE review_requests SET state = ?, updated_at = CURRENT_TIMESTAMP WHERE request_id = ?",
                (final_state.value, request_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(request_id)
            self._append_event(
                connection,
                request_id,
                "review.terminal",
                {"state": final_state.value, "reason": reason, "result_code": result_code},
            )

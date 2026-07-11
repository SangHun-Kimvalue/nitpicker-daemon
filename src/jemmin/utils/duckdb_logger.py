"""DuckDB 기반 분석 로거 — 비용·지연·품질 명세를 지속 저장합니다.

duckdb가 설치되지 않은 환경에서도 no-op으로 안전하게 동작하도록
_HAS_DUCKDB 플래그를 사용합니다.
"""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import duckdb
    _HAS_DUCKDB = True
except ImportError:  # pragma: no cover
    _HAS_DUCKDB = False

# 리뷰 이벤트 테이블 DDL
_REVIEW_DDL = """
CREATE TABLE IF NOT EXISTS review_events (
    id           BIGINT PRIMARY KEY,
    recorded_at  VARCHAR NOT NULL,
    request_id   VARCHAR NOT NULL,
    event_type   VARCHAR NOT NULL,
    status       VARCHAR,
    result_code  VARCHAR,
    confidence   DOUBLE,
    latency_ms   DOUBLE,
    token_input  INTEGER,
    token_output INTEGER,
    cost_usd     DOUBLE,
    agent_name   VARCHAR,
    findings_cnt INTEGER,
    payload_json VARCHAR
);
CREATE SEQUENCE IF NOT EXISTS review_events_seq START 1;
"""


class DuckDbLogger:
    """리뷰 이벤트를 DuckDB에 기록하는 로거.

    duckdb가 없으면 no-op으로 동작합니다.
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._path = str(db_path)
        self._lock = threading.Lock()
        self._conn: Any = None
        if _HAS_DUCKDB:
            self._conn = duckdb.connect(self._path)
            self._conn.execute(_REVIEW_DDL)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write(self, payload: dict[str, Any]) -> None:
        """JSON 페이로드를 review_events 테이블에 삽입합니다."""
        if not _HAS_DUCKDB or self._conn is None:
            return
        row = self._normalise(payload)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO review_events (
                    id, recorded_at, request_id, event_type,
                    status, result_code, confidence,
                    latency_ms, token_input, token_output, cost_usd,
                    agent_name, findings_cnt, payload_json
                ) VALUES (
                    nextval('review_events_seq'), ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?
                )
                """,
                [
                    row["recorded_at"],
                    row["request_id"],
                    row["event_type"],
                    row["status"],
                    row["result_code"],
                    row["confidence"],
                    row["latency_ms"],
                    row["token_input"],
                    row["token_output"],
                    row["cost_usd"],
                    row["agent_name"],
                    row["findings_cnt"],
                    json.dumps(payload, ensure_ascii=False, default=str),
                ],
            )

    def query(self, sql: str) -> list[dict[str, Any]]:
        """SQL 쿼리를 실행하고 dict 리스트를 반환합니다."""
        if not _HAS_DUCKDB or self._conn is None:
            return []
        with self._lock:
            rel = self._conn.execute(sql)
            cols = [d[0] for d in rel.description]
            return [dict(zip(cols, row)) for row in rel.fetchall()]

    def cost_summary(self) -> list[dict[str, Any]]:
        """event_type별 누적 비용 요약을 반환합니다."""
        return self.query(
            """
            SELECT event_type,
                   COUNT(*)          AS events,
                   SUM(token_input)  AS total_input_tokens,
                   SUM(token_output) AS total_output_tokens,
                   SUM(cost_usd)     AS total_cost_usd
            FROM review_events
            GROUP BY event_type
            ORDER BY total_cost_usd DESC
            """
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise(payload: dict[str, Any]) -> dict[str, Any]:
        """다양한 페이로드 구조를 테이블 컬럼으로 정규화합니다."""
        return {
            "recorded_at": datetime.now(UTC).isoformat(),
            "request_id": payload.get("request_id", ""),
            "event_type": payload.get("event_type", payload.get("type", "unknown")),
            "status": payload.get("status", ""),
            "result_code": payload.get("result_code", ""),
            "confidence": float(payload.get("confidence_score", 0.0)),
            "latency_ms": float(payload.get("latency_ms", 0.0)),
            "token_input": int(payload.get("token_input", 0)),
            "token_output": int(payload.get("token_output", 0)),
            "cost_usd": float(payload.get("cost_usd", 0.0)),
            "agent_name": payload.get("agent_name", ""),
            "findings_cnt": int(payload.get("findings_cnt", 0)),
        }

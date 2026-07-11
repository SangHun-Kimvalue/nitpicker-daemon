from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from jemmin.models import ReviewResult


class JsonlReviewLogger:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def log_event(self, request_id: str, event_type: str, payload: dict) -> None:
        self._append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "request_id": request_id,
                "event_type": event_type,
                "payload": payload,
            }
        )

    def log_result(self, result: ReviewResult) -> None:
        self._append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "request_id": result.request_id,
                "state": result.state.value,
                "status": result.status,
                "result_code": result.result_code,
                "summary": result.summary,
                "confidence_score": result.confidence_score,
                "patch_hash": result.patch_hash,
                "verification_result": result.verification_result,
            }
        )

    def _append(self, payload: dict) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True) + "\n")

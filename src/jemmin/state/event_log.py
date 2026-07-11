from __future__ import annotations

import json
from datetime import datetime, UTC
from pathlib import Path


class EventLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, request_id: str, event_type: str, payload: dict) -> None:
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "request_id": request_id,
            "event_type": event_type,
            "payload": payload,
        }
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")

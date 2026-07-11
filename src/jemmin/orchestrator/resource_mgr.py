from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock

from jemmin.models import ReviewRequest

# 인텐트별 debounce 설정 (seconds)
_DEBOUNCE_SECONDS: dict[str, float] = {
    "passive_save": 1.5,
    "active_intent": 0.0,  # 즉시 실행
}


@dataclass(slots=True)
class ResourceSnapshot:
    cpu_percent: float
    memory_percent: float
    inflight_jobs: int
    token_budget_remaining: int
    breaker_open: bool


class DefaultResourceManager:
    """인텐트 인지 Debounce + 토큰 예산 + Circuit Breaker.

    passive_save: 1.5초 debounce (Ctrl+S 코드 폭주 흡수)
    active_intent: 즉시 실행 (Git Add/Commit / 수동 Trigger)
    """

    def __init__(
        self,
        token_budget: int = 100_000,
        debounce: dict[str, float] | None = None,
    ) -> None:
        self._token_budget = token_budget
        self._breaker_open = False
        self._debounce = debounce if debounce is not None else dict(_DEBOUNCE_SECONDS)
        # 마지막 요청 시간 추적 (debounce 상태 확인용)
        self._last_request_at: dict[str, float] = {}  # key -> timestamp
        self._lock = Lock()

    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=0.0,
            memory_percent=0.0,
            inflight_jobs=0,
            token_budget_remaining=self._token_budget,
            breaker_open=self._breaker_open,
        )

    def allow_new_job(self, request: ReviewRequest) -> bool:
        if self._breaker_open:
            return False
        intent: str = request.trigger_intent
        wait = self._debounce.get(intent, 0.0)
        if wait > 0:
            key = f"{intent}:{request.target_file}"
            now = time.monotonic()
            with self._lock:
                last = self._last_request_at.get(key, 0.0)
                if now - last < wait:
                    return False  # debounce 창 내에 있음
                self._last_request_at[key] = now
        return True

    def reserve_tokens(self, request_id: str, amount: int) -> bool:
        with self._lock:
            if amount > self._token_budget:
                return False
            self._token_budget -= amount
            return True

    def release_tokens(self, request_id: str) -> None:
        return None

    def open_circuit(self, reason: str) -> None:
        self._breaker_open = True

    def close_circuit(self) -> None:
        self._breaker_open = False

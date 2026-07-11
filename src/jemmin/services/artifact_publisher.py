"""ArtifactPublisher — 리뷰 결과를 여러 출력 채널에 팬아웃하는 합성 레이어.

오케스트레이터(controller.py)가 feedback/logger/analytics/quick-fix를
각각 직접 호출하는 4갈래 결합을 해소합니다.

사용법:
    publisher = ArtifactPublisher(
        feedback_service=FileFeedbackService(...),
        review_logger=JsonlReviewLogger(...),
        analytics_logger=DuckDbLogger(...),
    )
    # 한 번의 호출로 모든 채널에 결과 발행
    publisher.publish_result(result, request_id, ...)
"""
from __future__ import annotations

import logging
from typing import Any, Protocol


# ---------------------------------------------------------------------------
# Protocol 정의 — duck typing으로 서비스 결합도 제거
# ---------------------------------------------------------------------------


class DiagnosticsPublisher(Protocol):
    def publish_diagnostics(self, result: Any, *, target_file: str = "") -> None: ...

    def publish_quick_fix(
        self,
        result: Any,
        target_file: str,
        findings: list[dict[str, Any]] | None = None,
        patch: Any | None = None,
    ) -> None: ...


class ResultLogger(Protocol):
    def log_result(self, result: Any) -> None: ...


class AnalyticsWriter(Protocol):
    def write(self, payload: dict[str, Any]) -> None: ...


# ---------------------------------------------------------------------------
# ArtifactPublisher
# ---------------------------------------------------------------------------


class ArtifactPublisher:
    """여러 출력 채널을 하나의 인터페이스로 통합합니다.

    각 채널은 독립적으로 실패할 수 있으며 (Fail-Safe),
    한 채널의 오류가 다른 채널의 발행을 차단하지 않습니다.
    """

    def __init__(
        self,
        *,
        feedback_service: DiagnosticsPublisher | None = None,
        review_logger: ResultLogger | None = None,
        analytics_logger: AnalyticsWriter | None = None,
        notifier: Any | None = None,
    ) -> None:
        self._feedback = feedback_service
        self._logger = review_logger
        self._analytics = analytics_logger
        self._notifier = notifier  # NotificationService (선택)

    # --- 단일 호출: 전체 발행 ---

    def publish_result(
        self,
        result: Any,
        *,
        target_file: str = "",
        findings: list[dict[str, Any]] | None = None,
        patch: Any | None = None,
    ) -> None:
        """diagnostics + quick-fix + result log를 한 번에 발행합니다."""
        self.publish_diagnostics(result, target_file=target_file)
        if target_file:
            self.publish_quick_fix(result, target_file, findings, patch)
        self.log_result(result)
        self._send_notification(result, target_file=target_file)

    # --- 개별 채널 (safe wrapper) ---

    def publish_diagnostics(self, result: Any, *, target_file: str = "") -> None:
        if self._feedback is None:
            return
        try:
            self._feedback.publish_diagnostics(result, target_file=target_file)
        except Exception as error:
            logging.error(
                "[ArtifactPublisher] diagnostics failed for %s: %s",
                getattr(result, "request_id", "?"),
                error,
            )

    def publish_quick_fix(
        self,
        result: Any,
        target_file: str,
        findings: list[dict[str, Any]] | None = None,
        patch: Any | None = None,
    ) -> None:
        if self._feedback is None:
            return
        try:
            self._feedback.publish_quick_fix(
                result, target_file=target_file, findings=findings, patch=patch,
            )
        except (OSError, ValueError, TypeError) as error:
            logging.error(
                "[ArtifactPublisher] quick-fix failed for %s: %s",
                getattr(result, "request_id", "?"),
                error,
            )

    def log_result(self, result: Any) -> None:
        if self._logger is None:
            return
        try:
            self._logger.log_result(result)
        except Exception as error:
            logging.error(
                "[ArtifactPublisher] result log failed for %s: %s",
                getattr(result, "request_id", "?"),
                error,
            )

    def log_analytics(
        self,
        request_id: str,
        *,
        event_type: str,
        status: str = "",
        agent_name: str = "",
        latency_ms: float = 0.0,
        findings_cnt: int = 0,
        result_code: str = "",
        confidence: float = 0.0,
    ) -> None:
        if self._analytics is None:
            return
        try:
            self._analytics.write({
                "request_id": request_id,
                "event_type": event_type,
                "status": status,
                "agent_name": agent_name,
                "latency_ms": latency_ms,
                "findings_cnt": findings_cnt,
                "result_code": result_code,
                "confidence_score": confidence,
            })
        except Exception as error:
            logging.warning(
                "[ArtifactPublisher] analytics write failed for %s: %s",
                request_id,
                error,
            )

    def _send_notification(self, result: Any, *, target_file: str = "") -> None:
        if self._notifier is None:
            return
        try:
            self._notifier.notify(result, target_file=target_file)
        except Exception as error:
            logging.debug(
                "[ArtifactPublisher] notification failed for %s: %s",
                getattr(result, "request_id", "?"),
                error,
            )

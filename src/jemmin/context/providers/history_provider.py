"""HistoryProvider — JSONL 리뷰 로그 기반 tier4 유사 리뷰 이력 수집기.

과거 리뷰 결과에서 동일 파일 또는 유사 패턴에 대한 이력을 검색하여
tier4 ContextFragment로 변환합니다. LLM이 과거 리뷰 결과를 참고하여
일관성 있는 리뷰를 생성할 수 있도록 합니다.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from jemmin.models import ReviewRequest

from .base import ContextFragment

_logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 5
_MAX_SCAN_LINES = 2000  # 성능을 위해 최근 N줄만 역순 스캔


class HistoryProvider:
    """JSONL 리뷰 로그에서 유사 과거 리뷰를 검색하여 tier4 context로 제공합니다."""

    name: str = "history"

    def __init__(
        self,
        *,
        review_log_path: str | Path | None = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> None:
        self._log_path: Path | None = Path(review_log_path) if review_log_path else None
        self._limit = limit

    def collect(self, request: ReviewRequest) -> ContextFragment:
        if not self._log_path or not self._log_path.exists():
            return ContextFragment(tier="tier4", entries=[], metadata={"source": "history", "reason": "no_log"})

        target = request.target_file or ""
        matches = self._search(target)

        if not matches:
            return ContextFragment(
                tier="tier4",
                entries=[],
                metadata={"source": "history", "target_file": target, "matches": 0},
            )

        entries = self._format_entries(matches, target)
        return ContextFragment(
            tier="tier4",
            entries=entries,
            metadata={
                "source": "history",
                "target_file": target,
                "matches": len(matches),
            },
        )

    def _search(self, target_file: str) -> list[dict[str, Any]]:
        """JSONL 로그를 역순 스캔하여 동일 파일에 대한 과거 리뷰를 검색합니다."""
        assert self._log_path is not None

        try:
            lines = self._log_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            _logger.warning("Failed to read review log %s: %s", self._log_path, exc)
            return []

        # 최근 N줄만 역순 스캔
        scan_lines = lines[-_MAX_SCAN_LINES:] if len(lines) > _MAX_SCAN_LINES else lines
        target_lower = target_file.lower().replace("\\", "/")
        results: list[dict[str, Any]] = []

        for line in reversed(scan_lines):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            # 파일명 매칭: request_id 또는 payload 내 target_file
            if not self._matches_target(record, target_lower):
                continue

            # 의미 있는 리뷰 결과만 수집 (pass/rejected/degraded)
            status = record.get("status", "")
            if status not in ("pass", "rejected", "degraded", "failed"):
                continue

            results.append(record)
            if len(results) >= self._limit:
                break

        return results

    @staticmethod
    def _matches_target(record: dict[str, Any], target_lower: str) -> bool:
        """레코드가 대상 파일과 관련있는지 판단합니다."""
        if not target_lower:
            return False

        # request_id에 파일명이 포함된 경우 (hash 기반 request_id는 무시)
        request_id = str(record.get("request_id", "")).lower().replace("\\", "/")
        if target_lower in request_id:
            return True

        # summary에 파일명이 포함된 경우
        summary = str(record.get("summary", "")).lower().replace("\\", "/")
        if target_lower in summary:
            return True

        # payload 내 target_file 필드
        payload = record.get("payload", {})
        if isinstance(payload, dict):
            payload_target = str(payload.get("target_file", "")).lower().replace("\\", "/")
            if payload_target and target_lower in payload_target:
                return True

        return False

    @staticmethod
    def _format_entries(matches: list[dict[str, Any]], target_file: str) -> list[str]:
        """과거 리뷰 결과를 LLM이 읽기 좋은 텍스트로 변환합니다."""
        parts: list[str] = [f"Past reviews for {target_file} ({len(matches)} found):"]

        for i, record in enumerate(matches, 1):
            status = record.get("status", "?")
            summary = record.get("summary", "(no summary)")
            result_code = record.get("result_code", "")
            confidence = record.get("confidence_score", 0)
            timestamp = record.get("timestamp", "")[:19]  # ISO 날짜 부분만

            line = f"  [{i}] {timestamp} | {status}"
            if result_code:
                line += f" ({result_code})"
            line += f" | confidence={confidence:.1f}"
            parts.append(line)

            # summary는 최대 200자로 제한
            truncated = summary[:200] + "..." if len(summary) > 200 else summary
            parts.append(f"      {truncated}")

        return parts

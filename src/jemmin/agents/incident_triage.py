"""IncidentTriageAgent — diff에서 장애 시그널 패턴을 탐지하여 우선순위를 부여한다."""
from __future__ import annotations

import re
from typing import Any

from jemmin.models import AgentDecision, ContextBundle, ReviewRequest

_ADDED_LINE_RE = re.compile(r"^\+(?!\+\+)(.*)$", re.MULTILINE)

# 장애 / 실수 유발 가능성이 높은 패턴
_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "INC001",
        "예외 없이 모든 예외 캐치 — 장애 원인이 숨겨질 수 있음; 예외 종류 명시하고 로깅",
        re.compile(r"except\s*Exception\s*:\s*\n\s*(pass|\.\.\.)\s*$", re.MULTILINE),
    ),
    (
        "INC002",
        "None 반환값을 검사 없이 사용 — is None / is not None 체크 필요",
        re.compile(
            r"=\s*\w+\.\w+\([^)]*\)\s*\n\s*\w+\.\w+",
            re.MULTILINE,
        ),
    ),
    (
        "INC003",
        "'# noqa' / '# type: ignore' 로 정적 검사 억제 — 실제 문제 해결 권장",
        re.compile(r"#\s*(noqa|type:\s*ignore)"),
    ),
    (
        "INC004",
        "logging.exception() / log.exception() 없이 예외 재발생 — 스택 트레이스 누락 위험",
        re.compile(r"except\s+[\w,(\s)]*:[\s\S]*?\n\s*(raise|return|pass)"),
    ),
    (
        "INC005",
        "NotImplemented 반환 (NotImplementedError 아님) — 비교 매직 목적으로만 사용 가능",
        re.compile(r"\breturn\s+NotImplemented\b"),
    ),
    (
        "INC006",
        "하드코딩된 IP/URL — 아이피 주소나 URL은 환경 변수/설정파일로 분리",
        re.compile(
            r"['\"]("
            r"https?://[\w./%-]{6,}"
            r"|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"
            r")['\"]\s*"
        ),
    ),
]


class IncidentTriageAgent:
    """장애 신호 탐지: 정적 분석만으로 실파/사고 숨김 패턴을 미리 차단한다."""

    name = "incident_triage"

    def __init__(self, provider: Any = None) -> None:
        self._provider = provider

    def run(self, request: ReviewRequest, context: ContextBundle) -> AgentDecision:
        diff = request.diff_text
        added = "\n".join(m.group(1) for m in _ADDED_LINE_RE.finditer(diff))
        findings: list[dict[str, Any]] = [
            {"code": code, "message": msg}
            for code, msg, pat in _RULES
            if pat.search(added)
        ]
        if findings:
            return AgentDecision(
                agent_name=self.name,
                status="warn",
                confidence_score=0.75,
                findings=findings,
                suggested_actions=[f"{f['code']}: {f['message']}" for f in findings],
            )
        return AgentDecision(
            agent_name=self.name,
            status="pass",
            confidence_score=0.9,
            findings=[],
            suggested_actions=[],
        )

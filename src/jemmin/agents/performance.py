"""PerformanceAgent — diff added-lines 기반 성능 안티패턴 탐지."""
from __future__ import annotations

import re
from typing import Any

from jemmin.models import AgentDecision, ContextBundle, ReviewRequest

_ADDED_LINE_RE = re.compile(r"^\+(?!\+\+)(.*)$", re.MULTILINE)

# (rule_code, message, pattern)
_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "PERF001",
        "루프 내 문자열 += 연산 — 리스트 누적 후 ''.join() 사용 권장 (O(n²) 불변 str 복사)",
        re.compile(r"(for|while)\s+.+:[\s\S]*?\n\s+\w+\s*\+=[\s]*['\"]?", re.MULTILINE),
    ),
    (
        "PERF002",
        "중첩 루프 내 리스트 탐색 — in list[] 는 O(n); set/dict로 교체 권장",
        re.compile(
            r"(for|while)\s+.+:[\s\S]*?\n\s+(for|while)\s+.+:[\s\S]*?\bin\s+\w*[Ll]ist",
            re.MULTILINE,
        ),
    ),
    (
        "PERF003",
        "copy.deepcopy() 과도한 사용 — 슬라이스/dataclasses.replace 등 경량 복사 고려",
        re.compile(r"\bcopy\.deepcopy\s*\("),
    ),
    (
        "PERF004",
        "글로벌 정규식을 루프 내부에서 re.compile — 루프 밖에서 한 번만 컴파일",
        re.compile(r"(for|while)\s+.+:[\s\S]*?\n\s+.*re\.compile\s*\(", re.MULTILINE),
    ),
    (
        "PERF005",
        "sorted() / sort() 를 루프 안에서 반복 호출 — 루프 밖으로 이동 권장",
        re.compile(r"(for|while)\s+.+:[\s\S]*?\n\s+.*\bsorted?\s*\(", re.MULTILINE),
    ),
    (
        "PERF006",
        "루프 내 len() 반복 호출 — 변수에 캐시 후 사용",
        re.compile(r"(for|while)\s+.+:[\s\S]*?\n\s+.*\blen\s*\(\w+\)", re.MULTILINE),
    ),
    (
        "PERF007",
        "리스트 컴프리헨션 중첩 3단계 이상 — 가독성/성능 모두 저하; 분리 고려",
        re.compile(r"\[[^\[\]]*for[^\[\]]*for[^\[\]]*for[^\[\]]*\]"),
    ),
    (
        "PERF008",
        "time.sleep() 동기 호출 — asyncio.sleep() 또는 이벤트 기반 대기 권장",
        re.compile(r"\btime\.sleep\s*\("),
    ),
]


def _added_block(diff_text: str) -> str:
    return "\n".join(m.group(1) for m in _ADDED_LINE_RE.finditer(diff_text))


def _scan_diff(diff_text: str) -> list[dict[str, Any]]:
    added = _added_block(diff_text)
    return [
        {"code": code, "message": msg}
        for code, msg, pat in _RULES
        if pat.search(added)
    ]


class PerformanceAgent:
    """정적 성능 안티패턴 탐지 에이전트 (diff added-lines 기반)."""

    name = "performance"

    def __init__(self, provider: Any = None) -> None:
        self._provider = provider

    def run(self, request: ReviewRequest, context: ContextBundle) -> AgentDecision:
        findings = _scan_diff(request.diff_text)
        if findings:
            return AgentDecision(
                agent_name=self.name,
                status="warn",
                confidence_score=0.7,
                findings=findings,
                suggested_actions=[f"{f['code']}: {f['message']}" for f in findings],
            )
        return AgentDecision(
            agent_name=self.name,
            status="pass",
            confidence_score=0.85,
            findings=[],
            suggested_actions=[],
        )

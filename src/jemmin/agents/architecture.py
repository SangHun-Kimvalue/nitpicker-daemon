from __future__ import annotations

import re
from typing import Any

from jemmin.models import AgentDecision, ContextBundle, ReviewRequest

# Architecture rules matched against added lines of the diff.
_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "ARCH001",
        "루프 내부 I/O — 반복문 안에서 파일/네트워크 I/O 또는 DB 쿼리 금지 (Hot-path 규칙)",
        re.compile(
            r"^\s*(for|while)\b[^\r\n]*:\s*[\r\n]+"
            r"(?:[^\S\r\n]+[^\r\n]*[\r\n]+)*?"
            r"[^\S\r\n]+[^\r\n]*(open\(|requests\.|httpx\.|aiohttp\.|sqlite3\.|cursor\.execute)",
            re.MULTILINE,
        ),
    ),
    (
        "ARCH002",
        "루프 내부 lazy import — import 문은 모듈 최상단에 위치해야 함",
        re.compile(
            r"(for|while)\s+.+:\s*[\r\n]+(?:[^\S\r\n]+.*[\r\n]+)*[^\S\r\n]+import\s"
        ),
    ),
    (
        "ARCH003",
        "예외 무시 패턴 (except: pass / except Exception: pass) — Fail-Fast 규칙 위반",
        re.compile(r"except\s*(Exception|BaseException|\(.*\))?\s*:\s*\n\s*pass"),
    ),
    (
        "ARCH004",
        "빈 except 절 — 최소한 logging 또는 re-raise 필요",
        re.compile(r"except\s*:\s*\n\s*pass"),
    ),
    (
        "ARCH005",
        "print()를 프로덕션 코드에서 사용 — logging 모듈 사용 권장",
        re.compile(r"\bprint\s*\(", re.MULTILINE),
    ),
    (
        "ARCH006",
        "전역 가변 상태 — 모듈 수준 list/dict/set 리터럴 할당은 동시성 위험",
        re.compile(
            r"^(?!\s*#)\s*[A-Z_][A-Z0-9_]*\s*:\s*(list|dict|set)\s*=\s*[\[\{]",
            re.MULTILINE,
        ),
    ),
    (
        "ARCH007",
        "__all__ 없이 공개 API 노출 — 외부 가져오기 범위를 명시하는 __all__ 정의 필요",
        re.compile(r"^def [a-z][a-z_]+\(|^class [A-Z]", re.MULTILINE),
    ),
]

_ADDED_LINE_RE = re.compile(r"^\+(?!\+\+)(.*)$", re.MULTILINE)

_DIFF_HUNK_RE = re.compile(r"^@@.*?@@", re.MULTILINE)


def _added_block(diff_text: str) -> str:
    """Return a synthetic source block made of only added lines."""
    return "\n".join(m.group(1) for m in _ADDED_LINE_RE.finditer(diff_text))


def _scan_diff(diff_text: str) -> list[dict[str, Any]]:
    added = _added_block(diff_text)
    findings: list[dict[str, Any]] = []
    for code, message, pattern in _RULES:
        # ARCH007 is informational — skip if the diff is tiny (< 20 added lines)
        if code == "ARCH007" and added.count("\n") < 20:
            continue
        if pattern.search(added):
            findings.append({"code": code, "message": message})
    return findings


class ArchitectureAgent:
    """Structural rules check: hot-path I/O, Fail-Fast, global mutable state.

    Operates purely on the diff text — no file system access required.
    """

    name = "architecture"

    def __init__(self, provider: Any = None) -> None:
        self._provider = provider

    def run(self, request: ReviewRequest, context: ContextBundle) -> AgentDecision:
        findings: list[dict[str, Any]] = _scan_diff(request.diff_text)

        if findings:
            return AgentDecision(
                agent_name=self.name,
                status="warn",
                confidence_score=0.75,
                findings=findings,
                suggested_actions=[
                    f"{f['code']}: {f['message']}" for f in findings
                ],
            )

        return AgentDecision(
            agent_name=self.name,
            status="pass",
            confidence_score=0.85,
            findings=[],
            suggested_actions=[],
        )

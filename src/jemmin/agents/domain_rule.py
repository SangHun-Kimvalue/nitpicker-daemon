"""DomainRuleAgent — 프로젝트 프로파일별 도메인 규칙 적용 에이전트."""
from __future__ import annotations

import re
from typing import Any

from jemmin.models import AgentDecision, ContextBundle, ReviewRequest

_ADDED_LINE_RE = re.compile(r"^\+(?!\+\+)(.*)$", re.MULTILINE)
_CONSTANT_ASSIGN_RE = re.compile(r"^[A-Z_][A-Z0-9_]*\s*(?::[^=]+)?=\s*")

# profile -> list of (code, message, pattern)
_PROFILE_RULES: dict[str, list[tuple[str, str, re.Pattern[str]]]] = {
    "general": [
        (
            "DOM001",
            "magic number 사용 — 상수에 이름 부여 권장",
            re.compile(r"(?<![\w.])([2-9]\d{2,}|[1-9]\d{3,})(?![\w.])"),
        ),
        (
            "DOM002",
            "bare string 예외 raise — Exception 서브클래스 사용 권장",
            re.compile(r"raise\s+(Exception|RuntimeError|ValueError)\s*\(['\"][^'\"]{0,80}['\"]"),
        ),
    ],
    "api": [
        (
            "DOM101",
            "HTTP 엔드포인트에 인증 미적용 의심 — 인증 데코레이터/미들웨어 확인",
            re.compile(
                r"@(app|router)\.(get|post|put|delete|patch)\([^)]*\)\s*\ndef ",
                re.DOTALL,
            ),
        ),
        (
            "DOM102",
            "응답에 직접 dict 반환 — Pydantic 응답 모델 또는 JSONResponse 사용 권장",
            re.compile(r"return\s+\{['\"]\w+['\"]"),
        ),
    ],
    "data": [
        (
            "DOM201",
            "pandas DataFrame에 iterrows() 사용 — vectorized 연산 또는 apply() 검토",
            re.compile(r"\.iterrows\s*\("),
        ),
        (
            "DOM202",
            "학습 데이터에 random_state 미설정 — 재현성 보장을 위해 고정값 설정",
            re.compile(r"(train_test_split|RandomForest|GridSearchCV)\([^)]*\)", re.DOTALL),
        ),
    ],
    "cli": [
        (
            "DOM301",
            "sys.exit() 직접 호출 — raise SystemExit(code) 사용 권장",
            re.compile(r"sys\.exit\s*\("),
        ),
    ],
}


def _rules_for_profile(profile: str) -> list[tuple[str, str, re.Pattern[str]]]:
    rules = list(_PROFILE_RULES.get("general", []))
    if profile != "general":
        rules.extend(_PROFILE_RULES.get(profile, []))
    return rules


def _reviewable_added_lines(diff_text: str) -> str:
    """Return added source lines that domain regexes should inspect.

    Domain rules are intentionally lightweight, so filter obvious non-code and
    already-named constants before applying broad patterns such as DOM001.
    """
    lines: list[str] = []
    for match in _ADDED_LINE_RE.finditer(diff_text):
        line = match.group(1)
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("#", '"""', "'''")):
            continue
        if _CONSTANT_ASSIGN_RE.match(stripped):
            continue
        lines.append(line)
    return "\n".join(lines)


class DomainRuleAgent:
    """프로젝트 프로파일(general/api/data/cli)에 맞는 도메인 규칙 검사."""

    name = "domain_rule"

    def __init__(self, provider: Any = None) -> None:
        self._provider = provider

    def run(self, request: ReviewRequest, context: ContextBundle) -> AgentDecision:
        diff = request.diff_text
        added = _reviewable_added_lines(diff)
        profile = request.project_profile or "general"
        rules = _rules_for_profile(profile)

        findings: list[dict[str, Any]] = [
            {"code": code, "message": msg}
            for code, msg, pat in rules
            if pat.search(added)
        ]

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

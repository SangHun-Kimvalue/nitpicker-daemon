from __future__ import annotations

import re
from typing import Any

from jemmin.models import AgentDecision, ContextBundle, ReviewRequest

# Patterns matched against added lines of the diff.
# Each entry: (rule_code, human_readable_message, compiled_regex)
_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "SEC001",
        "하드코딩된 비밀 키 의심 패턴 (secret/password/token= 할당)",
        re.compile(
            r"(?i)(secret|password|passwd|api_key|token|auth_key)\s*=\s*['\"][^'\"]{4,}",
            re.IGNORECASE,
        ),
    ),
    (
        "SEC002",
        "shell=True subprocess 호출은 명령어 인젝션 위험",
        re.compile(r"subprocess\.(run|Popen|call|check_output)\([^)]*shell\s*=\s*True"),
    ),
    (
        "SEC003",
        "eval() / exec() 사용은 임의 코드 실행 위험",
        re.compile(r"\b(eval|exec)\s*\("),
    ),
    (
        "SEC004",
        "assert 문은 -O 플래그로 제거되므로 보안 검사에 사용 금지",
        re.compile(r"^\+\s*assert\s+", re.MULTILINE),
    ),
    (
        "SEC005",
        "pickle.loads / pickle.load 는 신뢰할 수 없는 데이터에 사용 금지",
        re.compile(r"pickle\.loads?\s*\("),
    ),
    (
        "SEC006",
        "yaml.load() 에 Loader 없으면 임의 코드 실행 가능 — yaml.safe_load() 사용",
        re.compile(r"yaml\.load\s*\([^)]*\)"),
    ),
    (
        "SEC007",
        "random 모듈은 암호학적으로 안전하지 않음 — secrets 모듈 사용",
        re.compile(r"\brandom\.(random|randint|choice|shuffle)\s*\("),
    ),
    (
        "SEC008",
        "MD5 / SHA1 은 보안 목적으로 사용 금지",
        re.compile(r"hashlib\.(md5|sha1)\s*\(", re.IGNORECASE),
    ),
    (
        "SEC009",
        "SQL 쿼리 문자열 포매팅 — 파라미터 바인딩 사용",
        re.compile(
            r"""(execute|cursor\.execute)\s*\([^)]*(%|format|f['"]).*?(SELECT|INSERT|UPDATE|DELETE)""",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
]

_ADDED_LINE_RE = re.compile(r"^\+(?!\+\+)(.*)$", re.MULTILINE)


def _scan_diff(diff_text: str) -> list[dict[str, Any]]:
    added_content = "\n".join(
        m.group(1) for m in _ADDED_LINE_RE.finditer(diff_text)
    )
    findings: list[dict[str, Any]] = []
    for code, message, pattern in _RULES:
        if pattern.search(added_content):
            findings.append({"code": code, "message": message})
    return findings


class SecurityAgent:
    """Static security scan on the diff's added lines.

    Checks for hardcoded secrets, dangerous subprocess usage, insecure
    deserialization, weak crypto, and SQL injection patterns.
    """

    name = "security"

    def __init__(self, provider: Any = None) -> None:
        self._provider = provider

    def run(self, request: ReviewRequest, context: ContextBundle) -> AgentDecision:
        findings: list[dict[str, Any]] = _scan_diff(request.diff_text)

        if findings:
            return AgentDecision(
                agent_name=self.name,
                status="reject",
                confidence_score=0.95,
                findings=findings,
                suggested_actions=[
                    f"SEC{f['code'][-3:]}: {f['message']}" for f in findings
                ],
            )

        return AgentDecision(
            agent_name=self.name,
            status="pass",
            confidence_score=0.9,
            findings=[],
            suggested_actions=[],
        )

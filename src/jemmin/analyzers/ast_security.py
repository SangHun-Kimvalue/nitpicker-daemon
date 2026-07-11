"""AST Security Analyzer — Python ast 모듈을 사용한 보안 취약점 탐지.

diff에서 추가된 Python 코드 라인을 AST 파싱하여 위험한 함수 호출,
안전하지 않은 역직렬화, 동적 코드 실행 등을 정확하게 탐지합니다.

regex 기반 SecurityAgent와 달리 AST 수준에서 분석하므로:
  - 주석/문자열 안의 코드를 오탐하지 않습니다.
  - 함수 호출 체인을 정확하게 추적합니다.
  - import alias를 통한 우회를 탐지합니다.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

from jemmin.models import AgentDecision, ContextBundle, ReviewRequest


@dataclass(slots=True)
class AstFinding:
    """AST 분석에서 발견된 단일 이슈."""
    code: str
    message: str
    severity: str  # "error" | "warning" | "info"
    line_number: int
    node_type: str


# ── 위험 함수 호출 규칙 정의 ──────────────────────────────────────

_DANGEROUS_CALLS: dict[str, tuple[str, str, str]] = {
    # function_name: (code, message, severity)
    "eval": ("AST_EVAL", "eval() 호출 — 동적 코드 실행은 코드 인젝션 위험", "error"),
    "exec": ("AST_EXEC", "exec() 호출 — 임의 코드 실행 가능", "error"),
    "__import__": ("AST_DYNAMIC_IMPORT", "__import__() 호출 — 동적 임포트는 코드 인젝션 위험", "warning"),
    "compile": ("AST_COMPILE", "compile() 호출 — 동적 코드 컴파일 가능", "warning"),
    "getattr": ("AST_GETATTR", "getattr() 호출 — 동적 속성 접근은 보안 위험 가능", "info"),
}

_DANGEROUS_METHOD_CALLS: dict[str, tuple[str, str, str]] = {
    # attr_name: (code, message, severity)
    "loads": ("AST_PICKLE_LOADS", "pickle/yaml.loads() — 안전하지 않은 역직렬화", "error"),
    "load": ("AST_PICKLE_LOAD", "pickle/yaml.load() — 안전하지 않은 역직렬화", "warning"),
    "system": ("AST_OS_SYSTEM", "os.system() 호출 — 셸 명령 인젝션 위험", "error"),
    "popen": ("AST_OS_POPEN", "os.popen() 호출 — 셸 명령 인젝션 위험", "error"),
}

# pickle/yaml 관련 메서드만 loads/load 경고 대상
_UNSAFE_MODULES = {"pickle", "cPickle", "shelve", "yaml", "marshal"}

_SEVERITY_ORDER = {"error": 3, "warning": 2, "info": 1}


def _extract_added_lines(diff_text: str) -> str:
    """diff에서 +로 시작하는 추가된 라인만 추출하여 파싱 가능한 Python 코드로 결합."""
    lines: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(line[1:])  # + 접두사 제거
    return "\n".join(lines)


class _DangerousCallVisitor(ast.NodeVisitor):
    """AST 방문자 — 위험한 함수 호출을 수집합니다."""

    def __init__(self) -> None:
        self.findings: list[AstFinding] = []
        self._import_aliases: dict[str, str] = {}  # alias → module name

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name
            self._import_aliases[name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            name = alias.asname or alias.name
            self._import_aliases[name] = f"{module}.{alias.name}"
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._check_call(node)
        self.generic_visit(node)

    def _check_call(self, node: ast.Call) -> None:
        func = node.func

        # 단순 함수 호출: eval(...), exec(...)
        if isinstance(func, ast.Name):
            rule = _DANGEROUS_CALLS.get(func.id)
            if rule:
                code, message, severity = rule
                self.findings.append(AstFinding(
                    code=code,
                    message=message,
                    severity=severity,
                    line_number=node.lineno,
                    node_type="Call",
                ))

        # 메서드 호출: pickle.loads(...), os.system(...)
        elif isinstance(func, ast.Attribute):
            attr_name = func.attr
            rule = _DANGEROUS_METHOD_CALLS.get(attr_name)
            if rule:
                # 호출 대상이 위험 모듈인지 확인
                caller_name = self._resolve_caller(func.value)
                is_unsafe_module = any(
                    mod in caller_name for mod in _UNSAFE_MODULES
                ) if caller_name else False

                # os.system, os.popen은 모듈 무관하게 경고
                if attr_name in ("system", "popen"):
                    if caller_name and ("os" in caller_name or "subprocess" in caller_name):
                        code, message, severity = rule
                        self.findings.append(AstFinding(
                            code=code,
                            message=message,
                            severity=severity,
                            line_number=node.lineno,
                            node_type="MethodCall",
                        ))
                elif is_unsafe_module:
                    code, message, severity = rule
                    self.findings.append(AstFinding(
                        code=code,
                        message=f"{caller_name}.{attr_name}() — {message}",
                        severity=severity,
                        line_number=node.lineno,
                        node_type="MethodCall",
                    ))

            # subprocess.call/run with shell=True
            if attr_name in ("call", "run", "Popen") and self._has_shell_true(node):
                caller_name = self._resolve_caller(func.value)
                if caller_name and "subprocess" in caller_name:
                    self.findings.append(AstFinding(
                        code="AST_SUBPROCESS_SHELL",
                        message=f"subprocess.{attr_name}(shell=True) — 셸 인젝션 위험",
                        severity="error",
                        line_number=node.lineno,
                        node_type="MethodCall",
                    ))

    def _resolve_caller(self, node: ast.expr) -> str:
        """호출 대상의 이름을 해석합니다."""
        if isinstance(node, ast.Name):
            return self._import_aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            parent = self._resolve_caller(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return ""

    def _has_shell_true(self, node: ast.Call) -> bool:
        """keyword 인자 중 shell=True가 있는지 확인합니다."""
        for kw in node.keywords:
            if kw.arg == "shell":
                if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    return True
        return False


class AstSecurityAnalyzer:
    """Python AST 기반 보안 분석기.

    ReviewAgent 인터페이스(name, run)를 구현하여 기존 에이전트 시스템과 호환됩니다.
    """

    name = "ast_security"

    def __init__(self, provider: Any = None) -> None:
        self._provider = provider  # 미사용, 인터페이스 호환

    def run(self, request: ReviewRequest, context: ContextBundle) -> AgentDecision:
        diff_text = request.diff_text or ""
        if not diff_text:
            return self._pass_decision()

        # .py 파일만 분석
        target = request.target_file or ""
        if target and not target.endswith(".py"):
            return self._pass_decision()

        added_code = _extract_added_lines(diff_text)
        if not added_code.strip():
            return self._pass_decision()

        findings = self._analyze_code(added_code)
        if not findings:
            return self._pass_decision()

        max_severity = max(findings, key=lambda f: _SEVERITY_ORDER.get(f.severity, 0))

        status = "reject" if max_severity.severity == "error" else "warn"
        finding_dicts = [
            {
                "code": f.code,
                "message": f.message,
                "severity": f.severity,
                "line_number": f.line_number,
                "node_type": f.node_type,
            }
            for f in findings
        ]

        return AgentDecision(
            agent_name=self.name,
            status=status,
            confidence_score=0.95,
            findings=finding_dicts,
            suggested_actions=[f"Review {f.code}: {f.message}" for f in findings],
        )

    def _analyze_code(self, code: str) -> list[AstFinding]:
        """Python 코드를 AST 파싱하고 위험 패턴을 탐지합니다."""
        try:
            tree = ast.parse(code, mode="exec")
        except SyntaxError:
            # 불완전한 diff 코드는 파싱 실패 가능 — 무시
            return []

        visitor = _DangerousCallVisitor()
        visitor.visit(tree)
        return visitor.findings

    def _pass_decision(self) -> AgentDecision:
        return AgentDecision(
            agent_name=self.name,
            status="pass",
            confidence_score=1.0,
            findings=[],
            suggested_actions=[],
        )

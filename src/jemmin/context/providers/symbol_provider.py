"""SymbolProvider — AST 기반 tier2 심볼 그래프 수집기.

변경 파일의 Python AST를 파싱하여 import, 클래스, 함수, 호출 관계를
tier2 ContextFragment로 변환합니다.

비-Python 파일이나 구문 오류가 있는 코드에 대해서는 빈 fragment를 반환합니다.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any

from jemmin.models import ReviewRequest

from .base import ContextFragment

_logger = logging.getLogger(__name__)


class SymbolProvider:
    """ReviewRequest.target_file의 AST를 분석하여 심볼 그래프를 수집합니다."""

    name: str = "symbol"

    def __init__(self, *, project_root: str | Path | None = None) -> None:
        self._project_root = Path(project_root) if project_root else None

    def collect(self, request: ReviewRequest) -> ContextFragment:
        target = request.target_file
        if not target or not target.endswith(".py"):
            return ContextFragment(tier="tier2", entries=[], metadata={"source": "symbol", "reason": "not_python"})

        source = self._read_source(target)
        if source is None:
            # diff_text 에서 추가된 라인만 추출하여 파싱 시도
            source = self._extract_added_lines(request.diff_text or "")

        if not source:
            return ContextFragment(tier="tier2", entries=[], metadata={"source": "symbol", "reason": "no_source"})

        try:
            tree = ast.parse(source, filename=target)
        except SyntaxError:
            return ContextFragment(tier="tier2", entries=[], metadata={"source": "symbol", "reason": "syntax_error"})

        symbols = self._extract_symbols(tree, target)
        entries = self._format_entries(symbols)

        return ContextFragment(
            tier="tier2",
            entries=entries,
            metadata={
                "source": "symbol",
                "target_file": target,
                "imports": len(symbols.get("imports", [])),
                "classes": len(symbols.get("classes", [])),
                "functions": len(symbols.get("functions", [])),
                "calls": len(symbols.get("calls", [])),
            },
        )

    def _read_source(self, target_file: str) -> str | None:
        """실제 파일에서 소스 코드를 읽습니다."""
        candidates: list[Path] = [Path(target_file)]
        if self._project_root:
            candidates.append(self._project_root / target_file)

        for path in candidates:
            try:
                if path.is_file():
                    return path.read_text(encoding="utf-8")
            except OSError:
                continue
        return None

    @staticmethod
    def _extract_added_lines(diff_text: str) -> str:
        """diff에서 추가된 라인(+)만 추출합니다."""
        lines: list[str] = []
        for line in diff_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                lines.append(line[1:])  # '+' prefix 제거
        return "\n".join(lines)

    @staticmethod
    def _extract_symbols(tree: ast.Module, filename: str) -> dict[str, list[dict[str, Any]]]:
        """AST에서 import, class, function, call 심볼을 추출합니다."""
        symbols: dict[str, list[dict[str, Any]]] = {
            "imports": [],
            "classes": [],
            "functions": [],
            "calls": [],
        }

        # 클래스 내부 메서드 이름을 수집하여 functions에서 제외
        class_method_lines: set[int] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    symbols["imports"].append({
                        "module": alias.name,
                        "alias": alias.asname,
                        "line": getattr(node, "lineno", 0),
                    })

            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    symbols["imports"].append({
                        "module": f"{module}.{alias.name}" if module else alias.name,
                        "alias": alias.asname,
                        "line": getattr(node, "lineno", 0),
                    })

            elif isinstance(node, ast.ClassDef):
                bases = [
                    ast.unparse(b) if hasattr(ast, "unparse") else b.__class__.__name__
                    for b in node.bases
                ]
                methods = [
                    n.name for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                # 클래스 내부 메서드 라인 번호 기록 — functions 중복 방지
                for n in node.body:
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        class_method_lines.add(n.lineno)
                symbols["classes"].append({
                    "name": node.name,
                    "bases": bases,
                    "methods": methods,
                    "line": node.lineno,
                })

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # 클래스 내부 메서드는 classes에서 이미 수집됨 — 최상위만 수집
                if node.lineno in class_method_lines:
                    continue
                symbols["functions"].append({
                    "name": node.name,
                    "args": [a.arg for a in node.args.args],
                    "decorators": [
                        ast.unparse(d) if hasattr(ast, "unparse") else ""
                        for d in node.decorator_list
                    ],
                    "is_async": isinstance(node, ast.AsyncFunctionDef),
                    "line": node.lineno,
                })

            elif isinstance(node, ast.Call):
                func_name = _resolve_call_name(node.func)
                if func_name:
                    symbols["calls"].append({
                        "name": func_name,
                        "line": getattr(node, "lineno", 0),
                    })

        return symbols

    @staticmethod
    def _format_entries(symbols: dict[str, list[dict[str, Any]]]) -> list[str]:
        """심볼 정보를 LLM이 읽기 좋은 텍스트로 변환합니다."""
        parts: list[str] = []

        if symbols["imports"]:
            lines = [f"  - {imp['module']}" + (f" as {imp['alias']}" if imp.get("alias") else "")
                     for imp in symbols["imports"]]
            parts.append("Imports:\n" + "\n".join(lines))

        if symbols["classes"]:
            for cls in symbols["classes"]:
                bases_str = f"({', '.join(cls['bases'])})" if cls["bases"] else ""
                methods_str = ", ".join(cls["methods"][:10])  # 최대 10개
                if len(cls["methods"]) > 10:
                    methods_str += f" ... +{len(cls['methods']) - 10} more"
                parts.append(f"Class: {cls['name']}{bases_str} [line {cls['line']}]\n  methods: {methods_str}")

        if symbols["functions"]:
            for fn in symbols["functions"]:
                prefix = "async " if fn.get("is_async") else ""
                args_str = ", ".join(fn["args"][:8])
                parts.append(f"Function: {prefix}{fn['name']}({args_str}) [line {fn['line']}]")

        if symbols["calls"]:
            # 빈도순으로 상위 15개만
            call_counts: dict[str, int] = {}
            for call in symbols["calls"]:
                call_counts[call["name"]] = call_counts.get(call["name"], 0) + 1
            top_calls = sorted(call_counts.items(), key=lambda x: -x[1])[:15]
            call_lines = [f"  - {name} (x{count})" for name, count in top_calls]
            parts.append("Calls:\n" + "\n".join(call_lines))

        return parts


def _resolve_call_name(node: ast.expr) -> str:
    """ast.Call의 func 노드에서 호출 대상 이름을 추출합니다."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        value_name = _resolve_call_name(node.value)
        if value_name:
            return f"{value_name}.{node.attr}"
        return node.attr
    return ""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from jemmin.models import AgentDecision, ContextBundle, ReviewRequest

_ADDED_LINE_RE = re.compile(r"^\+(?!\+\+)(.*)$", re.MULTILINE)
_log = logging.getLogger(__name__)

# Fast Gate SLO: ruff ≤ 5 s, mypy ≤ 10 s — both Fail-Open on timeout
_RUFF_TIMEOUT = 5
_MYPY_TIMEOUT = 10


def _find_tool(name: str) -> list[str]:
    """Return the command to invoke ``name``, preferring the venv binary.

    Checks ``<venv>/Scripts/<name>.exe`` (Windows) and ``<venv>/bin/<name>``
    (Linux/macOS) before falling back to ``python -m <name>``.
    """
    scripts_dir = Path(sys.executable).parent
    for candidate in (
        scripts_dir / f"{name}.exe",   # Windows venv
        scripts_dir / name,            # Linux / macOS venv
    ):
        if candidate.exists():
            return [str(candidate)]
    return [sys.executable, "-m", name]  # fallback: module mode


def _extract_added_file_path(diff_text: str) -> str | None:
    """Return the b-side file path from the diff header, or None."""
    m = re.search(r"^\+\+\+ b/(.+)$", diff_text, re.MULTILINE)
    return m.group(1) if m else None


def _run_ruff(file_path: str) -> list[dict[str, Any]]:
    """Run ruff on a single .py file using ``--output-format=json``.

    Each returned dict contains:
        ``code``        – ruff rule code (e.g. "F401")
        ``line_number`` – 1-based line number or None
        ``message``     – human-readable description
        ``severity``    – ``"error"`` | ``"warning"`` (default ``"error"`` if absent)

    Returns a GATE_TIMEOUT sentinel on timeout; returns ``[]`` on missing /
    non-Python file or when the ruff binary is not found.
    """
    path = Path(file_path)
    if not path.exists() or path.suffix != ".py":
        return []
    cmd = _find_tool("ruff") + ["check", "--output-format=json", str(path)]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_RUFF_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _log.warning("ruff timed out after %ds on %s", _RUFF_TIMEOUT, file_path)
        return [
            {
                "code": "GATE_TIMEOUT",
                "line_number": None,
                "message": f"ruff timed out after {_RUFF_TIMEOUT}s",
                "severity": "warning",
            }
        ]
    except FileNotFoundError:
        _log.warning("ruff binary not found; skipping static analysis")
        return []
    if not result.stdout.strip():
        return []
    try:
        items: list[dict[str, Any]] = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return [
        {
            "code": item.get("code", "RUFF"),
            "line_number": item.get("location", {}).get("row"),
            "message": item.get("message", ""),
            # ruff populates this field; default to "error" so old mocks still work
            "severity": item.get("severity", "error"),
        }
        for item in items
    ]


def _run_mypy(file_path: str) -> list[dict[str, Any]]:
    """Run mypy on a single .py file using ``--output json`` (NDJSON).

    Each returned dict contains:
        ``code``        – always ``"MYPY"``
        ``line_number`` – 1-based line number or None
        ``message``     – human-readable description
        ``severity``    – always ``"warn"`` (mypy errors → ConsensusEngine decides)

    ``note``-severity lines are skipped (informational context, not actionable).
    Returns a GATE_TIMEOUT sentinel on timeout; returns ``[]`` on missing /
    non-Python file or when the mypy binary is not found.
    """
    path = Path(file_path)
    if not path.exists() or path.suffix != ".py":
        return []
    cmd = _find_tool("mypy") + [
        "--output", "json",
        "--ignore-missing-imports",
        "--no-error-summary",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_MYPY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _log.warning("mypy timed out after %ds on %s", _MYPY_TIMEOUT, file_path)
        return [
            {
                "code": "GATE_TIMEOUT",
                "line_number": None,
                "message": f"mypy timed out after {_MYPY_TIMEOUT}s",
                "severity": "warn",
            }
        ]
    except FileNotFoundError:
        _log.warning("mypy binary not found; skipping type checking")
        return []
    findings: list[dict[str, Any]] = []
    for raw_line in result.stdout.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            item: dict[str, Any] = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        # "note" lines are informational context attached to an error — skip them
        if item.get("severity") == "note":
            continue
        findings.append(
            {
                "code": "MYPY",
                "line_number": item.get("line"),
                "message": item.get("message", ""),
                # mypy type errors are warn-grade; ruff errors take priority
                "severity": "warn",
            }
        )
    return findings


class FastGateAgent:
    """Static analysis gate: runs Ruff + mypy on the changed file.

    Severity contract (ruff errors take priority over everything else):

        ruff  severity=error   → status="reject", confidence=0.95
        ruff  severity=warning → status="warn",   confidence=0.80
        mypy  any finding      → status="warn",   confidence=0.85
        GATE_TIMEOUT           → status="warn",   confidence=0.60  (Fail-Open)
        no findings            → status="pass",   confidence=0.90

    Falls back gracefully when the file doesn't exist on disk (e.g. staged diff).
    """

    name = "fast_gate"

    def __init__(self, provider: Any = None) -> None:
        self._provider = provider

    def run(self, request: ReviewRequest, context: ContextBundle) -> AgentDecision:
        file_path = request.target_file
        if not file_path:
            file_path_candidate = _extract_added_file_path(request.diff_text)
            if file_path_candidate:
                file_path = file_path_candidate

        ruff_findings = _run_ruff(file_path)
        mypy_findings = _run_mypy(file_path)

        # Classify ruff by severity; absent severity defaults to "error" (conservative)
        ruff_errors = [f for f in ruff_findings if f.get("severity", "error") == "error"]
        ruff_warns  = [f for f in ruff_findings if f.get("severity", "error") != "error"]

        def _fmt(f: dict[str, Any]) -> dict[str, Any]:
            loc = f"line {f['line_number']}: " if f.get("line_number") else ""
            return {"code": f["code"], "message": f"{f['code']} {loc}{f['message']}"}

        all_findings = (
            [_fmt(f) for f in ruff_errors]
            + [_fmt(f) for f in ruff_warns]
            + [_fmt(f) for f in mypy_findings]
        )

        # ruff errors → immediate reject (authoritative static analysis)
        if ruff_errors:
            return AgentDecision(
                agent_name=self.name,
                status="reject",
                confidence_score=0.95,
                findings=all_findings,
                suggested_actions=["fix ruff errors before merging"],
            )

        # mypy errors or ruff warnings → warn (ConsensusEngine decides weight)
        if mypy_findings or ruff_warns:
            confidence = 0.85 if mypy_findings else 0.80
            return AgentDecision(
                agent_name=self.name,
                status="warn",
                confidence_score=confidence,
                findings=all_findings,
                suggested_actions=["address mypy / ruff warnings"],
            )

        return AgentDecision(
            agent_name=self.name,
            status="pass",
            confidence_score=0.9,
            findings=[],
            suggested_actions=[],
        )

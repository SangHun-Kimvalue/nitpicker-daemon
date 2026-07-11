"""VerificationAgent — 패치 적용 후 pytest를 실행해 계단 검증하는 에이전트."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from jemmin.models import AgentDecision, ContextBundle, ReviewRequest

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _run_pytest(cwd: Path, timeout: int = 60) -> tuple[bool, str]:
    """pytest를 실행해 (성공, 출력) 튜플 반환."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--tb=short", "-q"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd),
        timeout=timeout,
    )
    combined = (result.stdout + result.stderr).strip()
    return result.returncode == 0, combined


class VerificationAgent:
    """패치 적용 후 테스트 통과 여부를 확인한다.

    context.metadata['run_verification'] == True 일 때만 pytest를 실행한다.
    기본적으로는 pass-through (LLM 단계 이후에만 활성화).
    """

    name = "verification"

    def __init__(self, provider: Any = None) -> None:
        self._provider = provider

    def run(self, request: ReviewRequest, context: ContextBundle) -> AgentDecision:
        if not context.metadata.get("run_verification"):
            return AgentDecision(
                agent_name=self.name,
                status="pass",
                confidence_score=1.0,
                findings=[],
                suggested_actions=[],
            )

        project_root = Path(
            context.metadata.get("project_root", str(_PROJECT_ROOT))
        )

        try:
            passed, output = _run_pytest(project_root)
        except subprocess.TimeoutExpired:
            return AgentDecision(
                agent_name=self.name,
                status="warn",
                confidence_score=0.3,
                findings=[{"code": "VER001", "message": "pytest 실행 시간 초과"}],
                suggested_actions=["VER001: 테스트 실행 시간을 줄이거나 슬로테스트 분리 권장"],
            )

        if passed:
            return AgentDecision(
                agent_name=self.name,
                status="pass",
                confidence_score=0.95,
                findings=[{"code": "VER_OK", "message": f"pytest 통과: {output[:200]}"}],
                suggested_actions=[],
            )

        return AgentDecision(
            agent_name=self.name,
            status="reject",
            confidence_score=0.95,
            findings=[{"code": "VER002", "message": f"pytest 실패: {output[:500]}"}],
            suggested_actions=["VER002: 패치 적용 후 테스트가 실패함 — 패치 재검토 필요"],
        )

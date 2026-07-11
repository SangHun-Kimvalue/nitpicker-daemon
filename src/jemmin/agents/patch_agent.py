"""PatchAgent — consensus.status=='patch'일 때 LLM이 제쥜한 패치를 diff에 적용하는 에이전트."""
from __future__ import annotations

import re
from typing import Any

from jemmin.models import AgentDecision, ContextBundle, ReviewRequest

# unified diff 헤더 패턴
_DIFF_HEADER_RE = re.compile(r"^(---\s+|\+\+\+\s+|@@)", re.MULTILINE)


def _is_valid_unified_diff(text: str) -> bool:
    """unified diff 콘텐츠인지 최소한 확인한다."""
    return bool(_DIFF_HEADER_RE.search(text))


class PatchAgent:
    """신청된 패치를 유효성 검사하는 에이전트.

    실제 패치 적용은 PatchService를 통해 수행된다.
    에이전트는 패치가 유효한 unified diff 형식인지 확인하고
    metadata에 'patch_proposal'이 존재하면 통과주는 역할을 한다.
    """

    name = "patch"

    def __init__(self, provider: Any = None) -> None:
        self._provider = provider

    def run(self, request: ReviewRequest, context: ContextBundle) -> AgentDecision:
        proposal: str | None = context.metadata.get("patch_proposal")

        if not proposal:
            # 패치 제쥜없음 — 에이전트 범위 밖
            return AgentDecision(
                agent_name=self.name,
                status="pass",
                confidence_score=1.0,
                findings=[],
                suggested_actions=[],
            )

        if not _is_valid_unified_diff(proposal):
            return AgentDecision(
                agent_name=self.name,
                status="warn",
                confidence_score=0.5,
                findings=[{"code": "PATCH001", "message": "패치 제쥜이 유효한 unified diff 형식이 아님"}],
                suggested_actions=["PATCH001: 패치를 unified diff(--- +++ @@) 형식으로 숙정하세요"],
            )

        return AgentDecision(
            agent_name=self.name,
            status="pass",
            confidence_score=0.9,
            findings=[{"code": "PATCH_OK", "message": "패치 제쥜 유효성 확인 완료"}],
            suggested_actions=[],
        )

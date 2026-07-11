from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jemmin.models import ReviewRequest

# passive_save 분기 시 실행할 에이전트명 세트
# — 이 등록된 에이전트만 Fast Path 실행
_FAST_PATH_AGENTS: frozenset[str] = frozenset({
    "fast_gate",
    "security",
    "architecture",
})


@dataclass(slots=True)
class PolicyDecision:
    accepted: bool
    project_profile: str
    offload_allowed: bool
    auto_apply_allowed: bool
    redaction_required: bool
    reason: str | None = None
    # trigger_intent 기반 라우팅 정보
    fast_path_only: bool = False
    allowed_agents: frozenset[str] | None = None  # None = 전체 허용


class DefaultPolicyEngine:
    """트리거 인텐트에 따라 Fast Path/Heavy Path를 분기하는 정책 엔진.

    - passive_save: FastGate + Security + Architecture 경량 에이전트만 실행 (비용 0원)
    - active_intent: 전체 7-Agent 심층 실행 + LLM 오프로딩 허용
    """

    def __init__(
        self,
        max_file_size_bytes: int = 512_000,
        fast_path_agents: frozenset[str] | None = None,
    ) -> None:
        self._max_file_size_bytes = max_file_size_bytes
        self._fast_path_agents = fast_path_agents or _FAST_PATH_AGENTS

    def evaluate_request(self, request: ReviewRequest) -> PolicyDecision:
        # 파일 크기 상한 검사
        file_path = Path(request.target_file)
        if file_path.exists() and file_path.stat().st_size > self._max_file_size_bytes:
            return PolicyDecision(
                accepted=False,
                project_profile=request.project_profile,
                offload_allowed=False,
                auto_apply_allowed=False,
                redaction_required=False,
                reason="file too large",
            )

        is_passive = request.trigger_intent == "passive_save"

        return PolicyDecision(
            accepted=True,
            project_profile=request.project_profile,
            offload_allowed=not is_passive,
            auto_apply_allowed=False,
            redaction_required=False,
            fast_path_only=is_passive,
            allowed_agents=self._fast_path_agents if is_passive else None,
        )

    def should_offload(self, request: ReviewRequest, context_size: int) -> bool:
        """passive_save는 절대 오프로드하지 않는다."""
        if request.trigger_intent == "passive_save":
            return False
        return context_size > 4000

    def should_promote_deep_review(self, request: ReviewRequest, signals: dict) -> bool:
        """신호(errors, warnings)를 보고 심층 리뷰를 트리거할지 판단."""
        if request.trigger_intent == "passive_save":
            return False
        return bool(signals.get("has_errors") or signals.get("high_risk"))

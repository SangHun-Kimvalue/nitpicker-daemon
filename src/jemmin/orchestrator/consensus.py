from __future__ import annotations

from jemmin.models import AgentDecision, ConsensusResult

_STATUS_WEIGHT = {"reject": 3, "error": 2, "warn": 1, "pass": 0}


class DefaultConsensusEngine:
    """가중 합의 엔진.

    규칙:
    - agent 중 하나라도 reject → 전체 reject.
    - reject 없고 warn만 있으면 advisory pass. 잔소리는 남기되 커밋 게이트로 승격하지 않는다.
    - 모두 pass / warn=0 → pass.
    - confidence_score는 모든 에이전트의 가중 평균 (reject 에이전트에 2배 가중).
    """

    def decide(self, decisions: list[AgentDecision]) -> ConsensusResult:
        if not decisions:
            return ConsensusResult(
                status="pass",
                summary="에이전트 결정 없음 — 기본 pass",
                confidence_score=0.5,
                winning_reasons=[],
                conflicting_agents=[],
            )

        rejecters = [d for d in decisions if d.status == "reject"]
        warners = [d for d in decisions if d.status == "warn"]
        passers = [d for d in decisions if d.status == "pass"]

        # 가중 confidence 평균
        total_weight = sum(
            (2 if d.status == "reject" else 1) for d in decisions
        )
        weighted_conf = sum(
            d.confidence_score * (2 if d.status == "reject" else 1)
            for d in decisions
        ) / total_weight

        if rejecters:
            rejector_names = [d.agent_name for d in rejecters]
            all_findings = [
                f"{f.get('code','?')}: {f.get('message','')}"
                for d in rejecters
                for f in d.findings
            ]
            summary = (
                f"reject by {', '.join(rejector_names)}. "
                f"findings: {'; '.join(all_findings[:5])}"
            )
            return ConsensusResult(
                status="reject",
                summary=summary,
                confidence_score=round(weighted_conf, 4),
                winning_reasons=rejector_names,
                conflicting_agents=[d.agent_name for d in passers],
            )

        if warners:
            warn_names = [d.agent_name for d in warners]
            all_findings = [
                f"{f.get('code','?')}: {f.get('message','')}"
                for d in warners
                for f in d.findings
            ]
            summary = (
                f"advisory by {', '.join(warn_names)}: "
                f"{'; '.join(all_findings[:3])}"
            )
            return ConsensusResult(
                status="pass",
                summary=summary,
                confidence_score=round(weighted_conf, 4),
                winning_reasons=warn_names,
                conflicting_agents=[d.agent_name for d in passers],
            )

        return ConsensusResult(
            status="pass",
            summary="모든 에이전트 통과",
            confidence_score=round(weighted_conf, 4),
            winning_reasons=[d.agent_name for d in passers],
            conflicting_agents=[],
        )

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from jemmin.models import AgentDecision, ContextBundle, ReviewRequest


@dataclass(slots=True)
class AnalyzerFinding:
    code: str
    message: str
    severity: str = "warning"
    line_number: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AnalyzerResult:
    analyzer_name: str
    status: str
    confidence_score: float
    findings: list[AnalyzerFinding] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class Analyzer(Protocol):
    """Shared contract for tool-based, regex, AST, LLM, and runtime analyzers."""

    name: str

    def analyze(self, request: ReviewRequest, context: ContextBundle) -> AnalyzerResult: ...


class ReviewAgentAnalyzerAdapter:
    """Adapter that lets existing ReviewAgent implementations participate as Analyzers."""

    def __init__(self, review_agent: Any) -> None:
        self.name = getattr(review_agent, "name", review_agent.__class__.__name__)
        self._review_agent = review_agent

    def analyze(self, request: ReviewRequest, context: ContextBundle) -> AnalyzerResult:
        decision: AgentDecision = self._review_agent.run(request, context)
        findings = [
            AnalyzerFinding(
                code=str(item.get("code", "GENERIC")),
                message=str(item.get("message") or item.get("issue") or "finding"),
                severity=str(item.get("severity", "warning")),
                line_number=item.get("line_number"),
                metadata=dict(item),
            )
            for item in decision.findings
        ]
        return AnalyzerResult(
            analyzer_name=decision.agent_name,
            status=decision.status,
            confidence_score=decision.confidence_score,
            findings=findings,
            metadata={"suggested_actions": list(decision.suggested_actions)},
        )
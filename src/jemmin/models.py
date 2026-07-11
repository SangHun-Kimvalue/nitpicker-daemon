from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class ReviewState(str, Enum):
    QUEUED = "queued"
    PRECHECK_FAILED = "precheck_failed"
    CONTEXT_READY = "context_ready"
    ANALYZING = "analyzing"
    CONSENSUS_REACHED = "consensus_reached"
    PATCH_PROPOSED = "patch_proposed"
    VERIFIED = "verified"
    DELIVERED = "delivered"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(slots=True)
class ReviewRequest:
    request_id: str
    idempotency_key: str
    project_id: str
    project_profile: str
    trigger: Literal["lsp", "cli", "git_hook", "web"]
    trigger_intent: Literal["passive_save", "active_intent"] = "active_intent"
    target_file: str = ""
    git_revision: str = ""
    base_file_hash: str = ""
    diff_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ReviewResult:
    request_id: str
    state: ReviewState
    status: Literal["pass", "rejected", "degraded", "failed", "ignored"]
    summary: str
    confidence_score: float
    result_code: str | None = None
    patch_hash: str | None = None
    verification_result: dict[str, Any] | None = None
    reviewer: str = ""


@dataclass(slots=True)
class ContextBundle:
    request_id: str
    context_hash: str
    token_estimate: int
    tiers: dict[str, list[str]]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentDecision:
    agent_name: str
    status: Literal["pass", "warn", "reject", "error"]
    confidence_score: float
    findings: list[dict[str, Any]]
    suggested_actions: list[str]
    raw_ref: str | None = None


@dataclass(slots=True)
class ConsensusResult:
    status: Literal["pass", "reject", "patch", "degraded"]
    summary: str
    confidence_score: float
    winning_reasons: list[str]
    conflicting_agents: list[str]

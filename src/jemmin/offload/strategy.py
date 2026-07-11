from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from jemmin.models import ReviewRequest


class OffloadMode(str, Enum):
    LOCAL_ONLY = "local_only"
    SHADOW_REMOTE = "shadow_remote"
    REMOTE_VERIFY_ONLY = "remote_verify_only"
    REMOTE_PRIMARY = "remote_primary"


@dataclass(slots=True)
class OffloadDecision:
    accepted: bool
    mode: OffloadMode = OffloadMode.LOCAL_ONLY
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class OffloadStrategy(Protocol):
    """Policy boundary that decides whether a review path should leave the local machine."""

    def decide(self, request: ReviewRequest, *, context_size: int) -> OffloadDecision: ...
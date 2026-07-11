from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class ReviewArtifact:
    channel: str
    payload: dict[str, Any]
    target_file: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class ReviewArtifactPublisher(Protocol):
    """Stable publisher boundary for diagnostics, quick-fix, logs, analytics, webhooks."""

    def publish(self, artifact: ReviewArtifact) -> None: ...


class CompositeArtifactPublisher:
    def __init__(self, publishers: list[ReviewArtifactPublisher] | None = None) -> None:
        self._publishers = list(publishers or [])

    def register(self, publisher: ReviewArtifactPublisher) -> None:
        self._publishers.append(publisher)

    def publish(self, artifact: ReviewArtifact) -> None:
        for publisher in self._publishers:
            publisher.publish(artifact)
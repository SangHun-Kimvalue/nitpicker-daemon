from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from jemmin.models import ReviewRequest


@dataclass(slots=True)
class TriggerEvent:
    """Normalized trigger input before conversion into ReviewRequest."""

    source: str
    payload: dict[str, Any]
    schema_version: str = "1.0"
    metadata: dict[str, Any] = field(default_factory=dict)


class TriggerAdapter(Protocol):
    """Stable extension point for CLI/LSP/watchdog/git-hook trigger inputs."""

    name: str

    def supports(self, event: TriggerEvent) -> bool: ...

    def build_request(self, event: TriggerEvent) -> ReviewRequest: ...
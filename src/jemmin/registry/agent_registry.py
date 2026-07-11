from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(slots=True)
class AgentManifest:
    name: str
    capabilities: set[str] = field(default_factory=set)
    supported_profiles: set[str] = field(default_factory=lambda: {"general"})
    supported_intents: set[str] = field(default_factory=lambda: {"active_intent", "passive_save"})
    required_context_tiers: set[str] = field(default_factory=set)
    cost_class: str = "medium"
    blocking_level: str = "warn"
    enabled: bool = True


@dataclass(slots=True)
class AgentRegistration:
    manifest: AgentManifest
    agent: Any


class AgentRegistry:
    """Manifest-driven registry for analyzer/agent selection and rollout control."""

    def __init__(self) -> None:
        self._entries: dict[str, AgentRegistration] = {}

    def register(self, manifest: AgentManifest, agent: Any) -> None:
        self._entries[manifest.name] = AgentRegistration(manifest=manifest, agent=agent)

    def get(self, name: str) -> AgentRegistration | None:
        return self._entries.get(name)

    def list(self) -> list[AgentRegistration]:
        return list(self._entries.values())

    def select(
        self,
        *,
        project_profile: str,
        trigger_intent: str,
        required_capabilities: Iterable[str] = (),
    ) -> list[Any]:
        required = set(required_capabilities)
        selected: list[Any] = []
        for entry in self._entries.values():
            manifest = entry.manifest
            if not manifest.enabled:
                continue
            if project_profile not in manifest.supported_profiles and "*" not in manifest.supported_profiles:
                continue
            if trigger_intent not in manifest.supported_intents:
                continue
            if required and not required.issubset(manifest.capabilities):
                continue
            selected.append(entry.agent)
        return selected
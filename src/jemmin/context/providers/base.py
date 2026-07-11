from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from jemmin.models import ReviewRequest


@dataclass(slots=True)
class ContextFragment:
    tier: str
    entries: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)


class ContextProvider(Protocol):
    """Composable provider contract for diff, symbol, history, policy, and vector context."""

    name: str

    def collect(self, request: ReviewRequest) -> ContextFragment: ...


class CompositeContextProvider:
    def __init__(self, providers: list[ContextProvider] | None = None) -> None:
        self._providers = list(providers or [])

    def register(self, provider: ContextProvider) -> None:
        self._providers.append(provider)

    def collect(self, request: ReviewRequest) -> dict[str, list[str]]:
        tiers: dict[str, list[str]] = {}
        for provider in self._providers:
            fragment = provider.collect(request)
            tiers.setdefault(fragment.tier, []).extend(fragment.entries)
        return tiers
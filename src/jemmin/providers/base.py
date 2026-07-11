from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class ProviderRequest:
    prompt_pack_version: str
    system_prompt: str
    user_prompt: str
    response_schema: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CacheEntry:
    """LLM 공급자의 Context Cache 메타데이터."""
    cache_id: str
    context_hash: str
    provider_name: str
    expires_at: float  # UNIX timestamp


class LLMProvider(Protocol):
    name: str

    def generate(self, request: ProviderRequest) -> dict[str, Any]: ...

    def generate_with_cache(
        self,
        cache_id: str,
        tier1_prompt: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def create_context_cache(
        self,
        system_prompt: str,
        static_context: str,
        ttl_seconds: int = 3600,
    ) -> str: ...  # returns cache_id

    def delete_context_cache(self, cache_id: str) -> None: ...

    def available(self) -> bool: ...

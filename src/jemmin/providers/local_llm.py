from __future__ import annotations

import time
import uuid
from typing import Any

from jemmin.providers.base import ProviderRequest

# 인메모리 캐시 저장소 (프로세스 수명 동안 유효)
_CACHE_STORE: dict[str, dict[str, Any]] = {}


class MockLocalLLMProvider:
    """테스트/로컬 전용 LLM 에뮬레이션 공급자.

    Context Cache API를 인메모리 dict으로 시뮬레이션합니다.
    """

    name = "mock_local"

    def generate(self, request: ProviderRequest) -> dict[str, Any]:
        return {"status": "PASS", "reason": "mock provider", "patch_code": ""}

    def generate_with_cache(
        self,
        cache_id: str,
        tier1_prompt: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # 캐시 해시 유효성 점검 후 응답 돌려줘
        entry = _CACHE_STORE.get(cache_id)
        if entry and entry["expires_at"] > time.time():
            return {"status": "PASS", "reason": "mock cache hit", "patch_code": "", "cache_id": cache_id}
        return {"status": "PASS", "reason": "mock cache miss fallback", "patch_code": ""}

    def create_context_cache(
        self,
        system_prompt: str,
        static_context: str,
        ttl_seconds: int = 3600,
    ) -> str:
        cache_id = f"mock-cache-{uuid.uuid4().hex[:12]}"
        _CACHE_STORE[cache_id] = {
            "system_prompt": system_prompt,
            "static_context": static_context,
            "expires_at": time.time() + ttl_seconds,
        }
        return cache_id

    def delete_context_cache(self, cache_id: str) -> None:
        _CACHE_STORE.pop(cache_id, None)

    def available(self) -> bool:
        return True

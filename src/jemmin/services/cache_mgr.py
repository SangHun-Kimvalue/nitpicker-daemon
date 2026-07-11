"""Semantic Context Cache Manager — Tier2/3 컨텍스트를 LLM 서버에 캐싱합니다.

context_hash가 동일하면 기존 cache_id를 재사용하고,
변경된 경우에만 새 캐시를 생성합니다.
만료된 캐시는 cleanup_expired()로 정리합니다.
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from jemmin.providers.base import CacheEntry, LLMProvider


@dataclass(slots=True)
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0


class ContextCacheManager:
    """Tier2/3 정적 컨텍스트를 LLM Provider의 Context Cache API를 통해 관리합니다.

    Usage::

        mgr = ContextCacheManager(provider=gemini_provider, ttl_seconds=3600)
        cache_id = mgr.get_or_create(system_prompt, static_context)
        result = provider.generate_with_cache(cache_id, tier1_diff)
    """

    def __init__(
        self,
        provider: LLMProvider,
        ttl_seconds: int = 3600,
        max_entries: int = 64,
    ) -> None:
        self._provider = provider
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._store: dict[str, CacheEntry] = {}  # context_hash -> CacheEntry
        self._lock = threading.Lock()
        self.stats = CacheStats()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_or_create(
        self,
        system_prompt: str,
        static_context: str,
    ) -> str:
        """context_hash에 대응하는 cache_id를 반환합니다.

        캐시가 없거나 만료된 경우 새 캐시를 생성합니다.
        Returns:
            cache_id (str): LLM Provider에 전달 가능한 캐시 식별자.
        """
        ctx_hash = self._hash(system_prompt, static_context)
        with self._lock:
            entry = self._store.get(ctx_hash)
            if entry and entry.expires_at > time.time():
                self.stats.hits += 1
                return entry.cache_id

            # 만료됐거나 없으면 새 캐시 생성
            if entry:
                self.stats.evictions += 1
                self._delete_safe(entry.cache_id)

            # LRU-lite: 최대 항목 초과 시 가장 오래된 항목 제거
            if len(self._store) >= self._max_entries:
                self._evict_oldest()

            self.stats.misses += 1

        # 락 밖에서 네트워크 호출 (blocking)
        cache_id = self._provider.create_context_cache(
            system_prompt=system_prompt,
            static_context=static_context,
            ttl_seconds=self._ttl,
        )
        new_entry = CacheEntry(
            cache_id=cache_id,
            context_hash=ctx_hash,
            provider_name=self._provider.name,
            expires_at=time.time() + self._ttl,
        )
        with self._lock:
            self._store[ctx_hash] = new_entry
        return cache_id

    def invalidate(self, system_prompt: str, static_context: str) -> bool:
        """특정 컨텍스트 캐시를 강제 무효화합니다.

        Returns:
            True if an entry was removed, False otherwise.
        """
        ctx_hash = self._hash(system_prompt, static_context)
        with self._lock:
            entry = self._store.pop(ctx_hash, None)
        if entry:
            self._delete_safe(entry.cache_id)
            self.stats.evictions += 1
            return True
        return False

    def cleanup_expired(self) -> int:
        """만료된 캐시 항목을 모두 제거하고 삭제 건수를 반환합니다."""
        now = time.time()
        expired: list[CacheEntry] = []
        with self._lock:
            to_remove = [k for k, v in self._store.items() if v.expires_at <= now]
            for k in to_remove:
                expired.append(self._store.pop(k))
        for entry in expired:
            self._delete_safe(entry.cache_id)
            self.stats.evictions += 1
        return len(expired)

    def clear_all(self) -> int:
        """모든 캐시 항목을 제거합니다. (테스트 / 셧다운 시 사용)"""
        with self._lock:
            all_entries = list(self._store.values())
            self._store.clear()
        for entry in all_entries:
            self._delete_safe(entry.cache_id)
            self.stats.evictions += 1
        return len(all_entries)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hash(system_prompt: str, static_context: str) -> str:
        combined = f"{system_prompt}\x00{static_context}"
        return hashlib.sha256(combined.encode()).hexdigest()

    def _delete_safe(self, cache_id: str) -> None:
        try:
            self._provider.delete_context_cache(cache_id)
        except Exception:  # noqa: BLE001
            pass  # 이미 만료된 원격 캐시는 무시

    def _evict_oldest(self) -> None:
        """만료 시간 기준으로 가장 오래된 항목을 제거합니다 (락 보유 상태에서 호출)."""
        if not self._store:
            return
        oldest_key = min(self._store, key=lambda k: self._store[k].expires_at)
        entry = self._store.pop(oldest_key)
        self._delete_safe(entry.cache_id)
        self.stats.evictions += 1

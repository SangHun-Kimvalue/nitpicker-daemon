"""Context Service — ReviewRequest로부터 ContextBundle을 구성합니다.

StaticContextService: CompositeContextProvider 기반으로 문맥을 수집하고,
ContextBundle로 정규화하여 오케스트레이터에 전달합니다.

확장 포인트:
  - DiffProvider (tier1): git diff 기반 문맥 → 기본 등록됨
  - SymbolProvider (tier2): AST/symbol graph 문맥 → 향후 구현
  - PolicyProvider (tier3): 정책/룰 문맥 → 향후 구현
  - HistoryProvider (tier4): 유사 리뷰 이력 → 향후 구현

Context Cache: build_context() 결과를 target_file 기반으로 캐싱하여 동일 파일
재리뷰 시 Provider 재수집을 건너뜁니다. invalidate_for_path()로 파일 변경 시
해당 캐시를 제거합니다.

Similar Review Lookup: JSONL 리뷰 로그를 역순 스캔하여 파일 경로 또는
키워드 기반으로 유사 과거 리뷰를 검색합니다.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from jemmin.context.providers import (
    CompositeContextProvider,
    DiffProvider,
    HistoryProvider,
    PolicyProvider,
    SymbolProvider,
)
from jemmin.models import ContextBundle, ReviewRequest

_logger = logging.getLogger(__name__)


class StaticContextService:
    """CompositeContextProvider 기반 문맥 서비스.

    기본적으로 DiffProvider가 등록되며, register_provider()로
    추가 프로바이더를 런타임에 주입할 수 있습니다.

    Context Cache:
      - build_context()는 target_file → ContextBundle 캐시를 유지합니다.
      - invalidate_for_path()로 파일 변경 시 해당 엔트리를 제거합니다.
      - invalidate_all()로 전체 캐시를 비울 수 있습니다.

    Similar Review Lookup:
      - review_log_path를 주입하면 JSONL 로그에서 유사 리뷰를 검색합니다.
    """

    def __init__(
        self,
        *,
        review_log_path: Path | str | None = None,
        cache_enabled: bool = True,
        project_root: Path | str | None = None,
        rules_dir: Path | str | None = None,
        profiles_dir: Path | str | None = None,
    ) -> None:
        providers: list[Any] = [DiffProvider()]

        # Phase V: 추가 Context Provider 자동 등록
        if project_root:
            providers.append(SymbolProvider(project_root=project_root))

        _rules = Path(rules_dir) if rules_dir else (Path(project_root) / "config" / "rules" if project_root else None)
        _profiles = Path(profiles_dir) if profiles_dir else (Path(project_root) / "config" / "profiles" if project_root else None)
        if _rules or _profiles:
            providers.append(PolicyProvider(rules_dir=_rules, profiles_dir=_profiles))

        _log_path = Path(review_log_path) if review_log_path else None
        if _log_path:
            providers.append(HistoryProvider(review_log_path=_log_path))

        self._composite = CompositeContextProvider(providers)
        self._cache: dict[str, ContextBundle] = {}
        self._cache_enabled = cache_enabled
        self._review_log_path: Path | None = _log_path

    def register_provider(self, provider: Any) -> None:
        """런타임에 추가 문맥 프로바이더를 등록합니다."""
        self._composite.register(provider)

    def build_context(self, request: ReviewRequest) -> ContextBundle:
        cache_key = request.target_file

        # 캐시 히트: 동일 파일에 대한 이전 결과 재사용
        if self._cache_enabled and cache_key in self._cache:
            cached = self._cache[cache_key]
            _logger.debug("Context cache hit for %s (hash=%s)", cache_key, cached.context_hash)
            return cached

        tiers: dict[str, list[str]] = self._composite.collect(request)

        # 하위 호환: 빈 tier 슬롯 보장
        for tier_name in ("tier1", "tier2", "tier3", "tier4"):
            tiers.setdefault(tier_name, [])

        # diff 전체 텍스트 기반 해시
        all_content: str = "\n".join(
            entry for entries in tiers.values() for entry in entries
        )
        digest: str = hashlib.sha256(all_content.encode("utf-8")).hexdigest()

        bundle = ContextBundle(
            request_id=request.request_id,
            context_hash=digest,
            token_estimate=max(1, len(all_content.split())),
            tiers=tiers,
            metadata={"source": "composite"},
        )

        if self._cache_enabled:
            self._cache[cache_key] = bundle

        return bundle

    def invalidate_for_path(self, file_path: str) -> int:
        """경로 기반 문맥 캐시 무효화.

        file_path와 일치하거나 file_path를 접두사로 포함하는 모든 캐시 엔트리를 제거합니다.
        반환값: 제거된 엔트리 수.
        """
        normalized = file_path.replace("\\", "/")
        to_remove = [
            key for key in self._cache
            if key.replace("\\", "/") == normalized
            or key.replace("\\", "/").startswith(normalized + "/")
        ]
        for key in to_remove:
            del self._cache[key]
        if to_remove:
            _logger.info("Invalidated %d context cache entries for %s", len(to_remove), file_path)
        return len(to_remove)

    def invalidate_all(self) -> int:
        """전체 캐시를 비웁니다. 반환값: 제거된 엔트리 수."""
        count = len(self._cache)
        self._cache.clear()
        return count

    @property
    def cache_size(self) -> int:
        """현재 캐시된 엔트리 수."""
        return len(self._cache)

    def lookup_similar_reviews(self, query: str, limit: int = 5) -> list[dict]:
        """유사 리뷰 검색 — JSONL 로그를 역순 스캔하여 query 키워드 매칭.

        query는 파일 경로 또는 키워드 문자열입니다.
        summary, request_id, 그리고 query 문자열이 포함된 리뷰를 최대 limit개 반환합니다.
        """
        if not self._review_log_path or not self._review_log_path.exists():
            return []

        query_lower = query.lower()
        results: list[dict] = []

        try:
            lines = self._review_log_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            _logger.warning("Failed to read review log %s: %s", self._review_log_path, exc)
            return []

        # 역순 스캔 — 최신 리뷰가 먼저 매칭
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            # request_id, summary, result_code 등에서 키워드 검색
            searchable = " ".join(
                str(record.get(field, ""))
                for field in ("request_id", "summary", "result_code", "status", "state")
            ).lower()

            if query_lower in searchable:
                results.append(record)
                if len(results) >= limit:
                    break

        return results

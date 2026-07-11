"""PolicyProvider — 프로젝트 정책/규칙 기반 tier3 컨텍스트 수집기.

config/rules/ 디렉토리의 규칙 파일과 config/profiles/ 의 프로필 설정을
tier3 ContextFragment로 변환합니다. LLM이 프로젝트 고유의 코딩 규칙과
정책을 참고하여 리뷰를 생성할 수 있도록 합니다.

규칙 파일 형식:
  - .md 파일: 마크다운 텍스트 그대로 context에 포함
  - .yaml/.yml 파일: YAML 키-값을 텍스트로 변환

프로필 설정:
  - config/profiles/<profile_name>.yaml 에서 프로젝트별 설정을 로드
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from jemmin.models import ReviewRequest

from .base import ContextFragment

_logger = logging.getLogger(__name__)

_MAX_RULE_SIZE = 8192  # 개별 규칙 파일 최대 크기 (바이트)
_MAX_TOTAL_SIZE = 32768  # 전체 규칙 텍스트 최대 크기


class PolicyProvider:
    """프로젝트 정책/규칙을 수집하여 tier3 context로 제공합니다."""

    name: str = "policy"

    def __init__(
        self,
        *,
        rules_dir: str | Path | None = None,
        profiles_dir: str | Path | None = None,
    ) -> None:
        self._rules_dir: Path | None = Path(rules_dir) if rules_dir else None
        self._profiles_dir: Path | None = Path(profiles_dir) if profiles_dir else None
        # 캐시: 규칙은 자주 바뀌지 않으므로 한 번 로드 후 재사용
        self._rules_cache: list[str] | None = None
        self._profiles_cache: dict[str, list[str]] = {}

    def collect(self, request: ReviewRequest) -> ContextFragment:
        entries: list[str] = []
        metadata: dict[str, Any] = {"source": "policy"}

        # 1. 프로젝트 규칙 로드
        rules = self._load_rules()
        if rules:
            entries.extend(rules)
            metadata["rules_count"] = len(rules)

        # 2. 프로필 설정 로드
        profile_name = request.project_profile or "general"
        profile_entries = self._load_profile(profile_name)
        if profile_entries:
            entries.extend(profile_entries)
            metadata["profile"] = profile_name

        if not entries:
            metadata["reason"] = "no_rules"

        return ContextFragment(tier="tier3", entries=entries, metadata=metadata)

    def _load_rules(self) -> list[str]:
        """config/rules/ 디렉토리에서 규칙 파일들을 로드합니다."""
        if self._rules_cache is not None:
            return self._rules_cache

        if not self._rules_dir or not self._rules_dir.is_dir():
            self._rules_cache = []
            return self._rules_cache

        rules: list[str] = []
        total_size = 0

        rule_files = sorted(self._rules_dir.iterdir())
        for rule_file in rule_files:
            if not rule_file.is_file():
                continue
            if rule_file.suffix not in (".md", ".yaml", ".yml", ".txt"):
                continue

            try:
                content = rule_file.read_text(encoding="utf-8")
            except OSError as exc:
                _logger.warning("Failed to read rule file %s: %s", rule_file, exc)
                continue

            # 크기 제한
            if len(content) > _MAX_RULE_SIZE:
                content = content[:_MAX_RULE_SIZE] + "\n... (truncated)"
            total_size += len(content)
            if total_size > _MAX_TOTAL_SIZE:
                _logger.info("Total rule size exceeded %d, stopping", _MAX_TOTAL_SIZE)
                break

            header = f"[Rule: {rule_file.name}]"
            rules.append(f"{header}\n{content.strip()}")

        self._rules_cache = rules
        return rules

    def _load_profile(self, profile_name: str) -> list[str]:
        """config/profiles/<name>.yaml 에서 프로필 설정을 로드합니다."""
        if profile_name in self._profiles_cache:
            return self._profiles_cache[profile_name]

        if not self._profiles_dir or not self._profiles_dir.is_dir():
            self._profiles_cache[profile_name] = []
            return []

        profile_file = self._profiles_dir / f"{profile_name}.yaml"
        if not profile_file.is_file():
            profile_file = self._profiles_dir / f"{profile_name}.yml"
        if not profile_file.is_file():
            self._profiles_cache[profile_name] = []
            return []

        try:
            content = profile_file.read_text(encoding="utf-8")
        except OSError as exc:
            _logger.warning("Failed to read profile %s: %s", profile_file, exc)
            self._profiles_cache[profile_name] = []
            return []

        if len(content) > _MAX_RULE_SIZE:
            content = content[:_MAX_RULE_SIZE] + "\n... (truncated)"

        entries = [f"[Profile: {profile_name}]\n{content.strip()}"]
        self._profiles_cache[profile_name] = entries
        return entries

    def invalidate_cache(self) -> None:
        """규칙/프로필 캐시를 무효화합니다. 파일 변경 후 호출하세요."""
        self._rules_cache = None
        self._profiles_cache.clear()

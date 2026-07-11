"""PromptLoader — 모든 LLM 프로바이더에 동일한 시스템 프롬프트를 제공하는 싱글 소스.

핫 리로드: 파일 mtime을 체크하여 변경 시 자동으로 다시 읽습니다.
데몬 재시작 없이 system_prompt.md를 수정하면 다음 리뷰부터 반영됩니다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

_logger = logging.getLogger(__name__)

__all__ = ["PromptLoader"]

_DEFAULT_PROMPT = (
    "You are Nitpicker, an ultra-strict senior software architect.\n"
    "Review the provided git diff. Respond only in valid JSON.\n"
    "CRITICAL: All review summaries, issues, and reasons MUST be written in Korean.\n"
)


class _CacheEntry:
    """파일 내용 + mtime을 함께 저장하는 캐시 엔트리."""

    __slots__ = ("text", "mtime")

    def __init__(self, text: str, mtime: float) -> None:
        self.text = text
        self.mtime = mtime


class PromptLoader:
    """config/system_prompt.md 를 읽어 시스템 프롬프트를 제공합니다.

    **핫 리로드**: get_system_prompt() 호출 시 파일 mtime을 체크하여
    변경되었으면 자동으로 다시 읽습니다. stat() 호출은 0.01ms 이하이므로
    매 리뷰마다 호출해도 성능 영향 없습니다.

    사용법::

        loader = PromptLoader()                    # 프로젝트 루트 자동 탐색
        loader = PromptLoader(project_root=root)   # 명시 지정

    어떤 LLM 프로바이더든 ``loader.get_system_prompt()`` 한 줄로 동일한 프롬프트를 주입할 수 있습니다.
    """

    _cache: ClassVar[dict[Path, _CacheEntry]] = {}

    def __init__(self, project_root: Path | str | None = None) -> None:
        if project_root is None:
            project_root = Path(__file__).resolve().parents[3]
        self._root = Path(project_root)
        self._prompt_path = self._root / "config" / "system_prompt.md"

    def get_system_prompt(self) -> str:
        """시스템 프롬프트를 반환합니다.

        파일 mtime을 체크하여 변경 시 자동 재로드 (핫 리로드).
        파일이 없으면 기본값을 사용합니다.
        """
        if self._prompt_path.is_file():
            try:
                current_mtime = self._prompt_path.stat().st_mtime
            except OSError:
                current_mtime = 0.0

            cached = self._cache.get(self._prompt_path)
            if cached is not None and cached.mtime == current_mtime:
                return cached.text

            # 캐시 miss 또는 mtime 변경 → 재로드
            try:
                text = self._prompt_path.read_text(encoding="utf-8").strip()
                if text:
                    if cached is not None and cached.mtime != current_mtime:
                        _logger.info("system_prompt.md 변경 감지 — 핫 리로드")
                    self._cache[self._prompt_path] = _CacheEntry(text, current_mtime)
                    return text
            except OSError:
                _logger.warning("Failed to read %s — using default", self._prompt_path, exc_info=True)
        else:
            _logger.info("system_prompt.md not found at %s — using default", self._prompt_path)

        self._cache[self._prompt_path] = _CacheEntry(_DEFAULT_PROMPT, 0.0)
        return _DEFAULT_PROMPT

    def invalidate_cache(self) -> None:
        """캐시를 무효화합니다. 테스트 등에서 강제 리로드 시 사용합니다."""
        self._cache.pop(self._prompt_path, None)

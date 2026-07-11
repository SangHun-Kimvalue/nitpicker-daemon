"""PromptLoader 테스트 — 시스템 프롬프트 로딩 및 캐싱."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from jemmin.prompts.prompt_loader import PromptLoader, _DEFAULT_PROMPT


class TestPromptLoader:
    def test_loads_from_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir()
            prompt_file = config_dir / "system_prompt.md"
            prompt_file.write_text("Custom prompt for testing", encoding="utf-8")

            loader = PromptLoader(project_root=root)
            result = loader.get_system_prompt()
            assert result == "Custom prompt for testing"

    def test_falls_back_to_default_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            loader = PromptLoader(project_root=tmp)
            loader.invalidate_cache()
            result = loader.get_system_prompt()
            assert result == _DEFAULT_PROMPT

    def test_caches_when_mtime_unchanged(self) -> None:
        """mtime이 동일하면 캐시된 값 반환 (stat만, read 안 함)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir()
            prompt_file = config_dir / "system_prompt.md"
            prompt_file.write_text("First version", encoding="utf-8")

            loader = PromptLoader(project_root=root)
            first = loader.get_system_prompt()
            assert first == "First version"

            # 동일 mtime이면 캐시 반환
            second = loader.get_system_prompt()
            assert second == "First version"

    def test_hot_reloads_when_mtime_changes(self) -> None:
        """mtime 변경 시 자동 핫 리로드."""
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir()
            prompt_file = config_dir / "system_prompt.md"
            prompt_file.write_text("First version", encoding="utf-8")

            loader = PromptLoader(project_root=root)
            assert loader.get_system_prompt() == "First version"

            _time.sleep(0.05)  # mtime 해상도 보장
            prompt_file.write_text("Second version", encoding="utf-8")
            assert loader.get_system_prompt() == "Second version"

    def test_invalidate_cache_reloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir()
            prompt_file = config_dir / "system_prompt.md"
            prompt_file.write_text("Original", encoding="utf-8")

            loader = PromptLoader(project_root=root)
            loader.get_system_prompt()

            prompt_file.write_text("Updated", encoding="utf-8")
            loader.invalidate_cache()
            assert loader.get_system_prompt() == "Updated"

    def test_empty_file_falls_back_to_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "system_prompt.md").write_text("", encoding="utf-8")

            loader = PromptLoader(project_root=root)
            loader.invalidate_cache()
            assert loader.get_system_prompt() == _DEFAULT_PROMPT

    def test_default_project_root_finds_config(self) -> None:
        """프로젝트 루트 자동 탐색 — 실제 config/system_prompt.md 로드."""
        loader = PromptLoader()
        loader.invalidate_cache()
        prompt = loader.get_system_prompt()
        assert "Nitpicker" in prompt
        assert "Korean" in prompt or "한국어" in prompt

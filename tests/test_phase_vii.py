"""Phase VII 테스트 — Auto-fix + Config 핫 리로드 + 프롬프트 최적화.

§1 AutoFixService 단위 테스트           (6 tests)
§2 Config 핫 리로드 테스트              (5 tests)
§3 PromptLoader 핫 리로드 테스트        (3 tests)
Total: 14 tests
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jemmin.prompts.prompt_loader import PromptLoader
from jemmin.services.autofix_svc import AutoFixResult, AutoFixService
from jemmin.services.config_watcher import ConfigWatcher


# ---------------------------------------------------------------------------
# §1 AutoFixService 단위 테스트
# ---------------------------------------------------------------------------


class TestAutoFixService:
    def test_empty_patch_returns_not_applied(self):
        """빈 패치 → applied=False."""
        svc = AutoFixService()
        result = svc.apply_patch("")
        assert not result.applied
        assert "빈 패치" in result.reason

    def test_empty_whitespace_patch_returns_not_applied(self):
        """공백만 있는 패치 → applied=False."""
        svc = AutoFixService()
        result = svc.apply_patch("   \n\n  ")
        assert not result.applied

    @patch("jemmin.services.autofix_svc.subprocess.run")
    def test_dryrun_failure_returns_not_applied(self, mock_run):
        """git apply --check 실패 → applied=False."""
        mock_run.return_value = MagicMock(returncode=1, stderr="error: patch does not apply", stdout="")
        svc = AutoFixService()
        result = svc.apply_patch("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new")
        assert not result.applied
        assert "dry-run" in result.reason

    @patch("jemmin.services.autofix_svc.subprocess.run")
    def test_successful_apply_no_verify(self, mock_run):
        """git apply 성공 + 재검증 미설정 → applied=True."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        svc = AutoFixService()
        result = svc.apply_patch("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new")
        assert result.applied
        assert result.verify_passed is None  # 재검증 미실행

    @patch("jemmin.services.autofix_svc.subprocess.run")
    def test_apply_with_verify_pass(self, mock_run):
        """git apply 성공 + 재검증 통과."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        svc = AutoFixService(verify_command=["python", "-c", "pass"])
        result = svc.apply_patch("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new")
        assert result.applied
        assert result.verify_passed is True

    @patch("jemmin.services.autofix_svc.subprocess.run")
    def test_apply_with_verify_fail_rollback(self, mock_run):
        """git apply 성공 + 재검증 실패 → 롤백."""
        # git apply --check OK, git apply OK, verify FAIL, git checkout OK
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr="", stdout=""),  # --check
            MagicMock(returncode=0, stderr="", stdout=""),  # apply
            MagicMock(returncode=1, stderr="test failed", stdout=""),  # verify
            MagicMock(returncode=0, stderr="", stdout=""),  # checkout (rollback)
        ]
        svc = AutoFixService(verify_command=["pytest", "-x"], auto_rollback=True)
        result = svc.apply_patch(
            "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new",
            target_file="x.py",
        )
        assert not result.applied
        assert result.rolled_back
        assert result.verify_passed is False


# ---------------------------------------------------------------------------
# §2 ConfigWatcher 테스트
# ---------------------------------------------------------------------------


class TestConfigWatcher:
    def test_register_and_no_change(self, tmp_path):
        """등록 후 변경 없으면 check()가 빈 리스트 반환."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "test.yaml").write_text("key: value", encoding="utf-8")

        watcher = ConfigWatcher(project_root=tmp_path)
        cb = MagicMock()
        watcher.register("test.yaml", cb)

        changed = watcher.check()
        assert changed == []
        cb.assert_not_called()

    def test_detect_change(self, tmp_path):
        """파일 변경 시 콜백 호출."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        f = config_dir / "test.json"
        f.write_text('{"a": 1}', encoding="utf-8")

        watcher = ConfigWatcher(project_root=tmp_path)
        cb = MagicMock()
        watcher.register("test.json", cb)

        # 변경 없음
        assert watcher.check() == []

        # mtime 변경 시뮬레이션 (파일 내용 수정)
        time.sleep(0.05)  # mtime 해상도
        f.write_text('{"a": 2}', encoding="utf-8")

        changed = watcher.check()
        assert "test.json" in changed
        cb.assert_called_once()
        # 콜백에 파싱된 dict 전달
        _, content = cb.call_args[0]
        assert content["a"] == 2

    def test_yaml_parsing(self, tmp_path):
        """YAML 파일 파싱 (yaml 패키지 없이)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        f = config_dir / "test.yaml"
        f.write_text("default: gemini\nmodel: qwen2.5-coder:7b\n", encoding="utf-8")

        watcher = ConfigWatcher(project_root=tmp_path)
        cb = MagicMock()
        watcher.register("test.yaml", cb)

        # 변경 시뮬레이션
        time.sleep(0.05)
        f.write_text("default: ollama\nmodel: qwen2.5-coder:7b\n", encoding="utf-8")

        watcher.check()
        _, content = cb.call_args[0]
        assert content["default"] == "ollama"

    def test_callback_error_does_not_crash(self, tmp_path):
        """콜백에서 에러 발생 시 crash하지 않음."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        f = config_dir / "err.json"
        f.write_text('{"x": 1}', encoding="utf-8")

        watcher = ConfigWatcher(project_root=tmp_path)
        cb = MagicMock(side_effect=ValueError("bad"))
        watcher.register("err.json", cb)

        time.sleep(0.05)
        f.write_text('{"x": 2}', encoding="utf-8")

        changed = watcher.check()  # should not raise
        assert "err.json" in changed

    def test_missing_file_skipped(self, tmp_path):
        """파일이 없으면 skip."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        watcher = ConfigWatcher(project_root=tmp_path)
        cb = MagicMock()
        watcher.register("nonexistent.yaml", cb)

        assert watcher.check() == []
        cb.assert_not_called()


# ---------------------------------------------------------------------------
# §3 PromptLoader 핫 리로드 테스트
# ---------------------------------------------------------------------------


class TestPromptLoaderHotReload:
    def test_hot_reload_on_mtime_change(self, tmp_path):
        """파일 mtime 변경 시 자동 재로드."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        f = config_dir / "system_prompt.md"
        f.write_text("Original prompt", encoding="utf-8")

        loader = PromptLoader(project_root=tmp_path)
        assert loader.get_system_prompt() == "Original prompt"

        # 파일 수정
        time.sleep(0.05)
        f.write_text("Updated prompt v2", encoding="utf-8")

        # 핫 리로드 발생
        assert loader.get_system_prompt() == "Updated prompt v2"

    def test_no_reload_when_unchanged(self, tmp_path):
        """mtime 동일하면 재로드하지 않음."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        f = config_dir / "system_prompt.md"
        f.write_text("Same prompt", encoding="utf-8")

        loader = PromptLoader(project_root=tmp_path)
        result1 = loader.get_system_prompt()
        result2 = loader.get_system_prompt()
        assert result1 == result2 == "Same prompt"

    def test_default_when_file_missing(self, tmp_path):
        """파일 없으면 기본값 사용."""
        loader = PromptLoader(project_root=tmp_path)
        prompt = loader.get_system_prompt()
        assert "Nitpicker" in prompt
        assert "Korean" in prompt

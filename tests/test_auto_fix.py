"""Safe Auto-Fix 파이프라인 테스트.

§1 _apply_patch_safely — git apply 기반 원자적 패치 적용   (6 tests)
§2 설정 로딩 — auto_apply_patches 플래그                   (3 tests)
§3 파이프라인 통합 — review_targets 내 자동 적용 흐름       (4 tests)
Total: 13 tests
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from jemmin.mini_reviewer import (
    MiniReviewerSettings,
    _apply_patch_safely,
    load_settings,
)


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------

def _settings(auto_apply: bool = False, skip: bool = True) -> MiniReviewerSettings:
    return MiniReviewerSettings(
        gemini_api_key="test-key",
        gemini_model="gemini-2.0-flash",
        watch_path="src",
        debounce_seconds=1.0,
        file_extensions=(".py",),
        skip=skip,
        auto_apply_patches=auto_apply,
    )


VALID_PATCH = """\
--- a/dummy.py
+++ b/dummy.py
@@ -1,3 +1,3 @@
-old_line = True
+new_line = True
 context_line = 1
 another_line = 2
"""


# ===========================================================================
# §1 _apply_patch_safely
# ===========================================================================


class TestApplyPatchSafely:
    def test_empty_patch_returns_false(self):
        """빈 패치 텍스트는 즉시 False를 반환해야 합니다."""
        assert _apply_patch_safely("") is False
        assert _apply_patch_safely("   ") is False
        assert _apply_patch_safely(None) is False  # type: ignore[arg-type]

    def test_successful_apply(self):
        """git apply --check 성공 → git apply 성공 시 True를 반환합니다."""
        mock_check = MagicMock(returncode=0, stderr="", stdout="")
        mock_apply = MagicMock(returncode=0, stderr="", stdout="")

        with patch("subprocess.run", side_effect=[mock_check, mock_apply]) as mock_run:
            result = _apply_patch_safely(VALID_PATCH)

        assert result is True
        assert mock_run.call_count == 2
        # 첫 번째 호출: --check
        first_call_args = mock_run.call_args_list[0][0][0]
        assert "--check" in first_call_args
        # 두 번째 호출: 실제 적용
        second_call_args = mock_run.call_args_list[1][0][0]
        assert "--check" not in second_call_args

    def test_check_fails_returns_false_without_apply(self):
        """git apply --check 실패 시 False를 반환하고, 실제 apply는 호출하지 않습니다."""
        mock_check = MagicMock(returncode=1, stderr="error: patch failed", stdout="")

        with patch("subprocess.run", return_value=mock_check) as mock_run:
            result = _apply_patch_safely(VALID_PATCH)

        assert result is False
        # --check 1회만 호출, 실제 apply는 호출 안 됨
        assert mock_run.call_count == 1

    def test_apply_fails_after_check_success(self):
        """--check 통과 후 실제 apply가 실패하면 False를 반환합니다."""
        mock_check = MagicMock(returncode=0, stderr="", stdout="")
        mock_apply = MagicMock(returncode=1, stderr="error: already applied", stdout="")

        with patch("subprocess.run", side_effect=[mock_check, mock_apply]):
            result = _apply_patch_safely(VALID_PATCH)

        assert result is False

    def test_tempfile_cleaned_up_on_success(self, tmp_path: Path):
        """성공 시 임시 .diff 파일이 삭제되어야 합니다."""
        mock_result = MagicMock(returncode=0, stderr="", stdout="")
        created_temps: list[str] = []

        original_mkstemp = __import__("tempfile").mkstemp

        def tracking_mkstemp(**kwargs):
            fd, path = original_mkstemp(**kwargs)
            created_temps.append(path)
            return fd, path

        with patch("subprocess.run", return_value=mock_result), \
             patch("tempfile.mkstemp", side_effect=tracking_mkstemp):
            _apply_patch_safely(VALID_PATCH)

        # 임시 파일이 삭제되었는지 확인
        for temp in created_temps:
            assert not Path(temp).exists(), f"임시 파일이 삭제되지 않음: {temp}"

    def test_git_not_found_propagates(self):
        """git 바이너리가 없는 환경에서는 FileNotFoundError가 전파됩니다 (Fail-Fast)."""
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            with pytest.raises(FileNotFoundError):
                _apply_patch_safely(VALID_PATCH)


# ===========================================================================
# §2 설정 로딩
# ===========================================================================


class TestAutoApplySettings:
    def test_default_is_false(self):
        """기본값으로 auto_apply_patches는 False입니다."""
        settings = _settings(auto_apply=False)
        assert settings.auto_apply_patches is False

    def test_env_var_enables_auto_apply(self):
        """NITPICKER_AUTO_APPLY=1 환경변수로 활성화됩니다."""
        env = {
            "GEMINI_API_KEY": "test-key",
            "NITPICKER_AUTO_APPLY": "1",
        }
        with patch.dict(os.environ, env, clear=False):
            settings = load_settings()
        assert settings.auto_apply_patches is True

    def test_config_file_enables_auto_apply(self, tmp_path: Path):
        """nitpicker.local.json의 auto_apply_patches: true로 활성화됩니다."""
        config = {
            "gemini_api_key": "test-key",
            "gemini_model": "gemini-2.0-flash",
            "auto_apply_patches": True,
        }
        config_path = tmp_path / "nitpicker.local.json"
        config_path.write_text(__import__("json").dumps(config), encoding="utf-8")

        with patch("jemmin.mini_reviewer.CONFIG_PATH", config_path):
            settings = load_settings()
        assert settings.auto_apply_patches is True


# ===========================================================================
# §3 파이프라인 통합
# ===========================================================================


class TestPipelineIntegration:
    def test_patch_applied_when_auto_apply_enabled(self):
        """auto_apply=True + PATCH_PROPOSED → _apply_patch_safely 호출 → PATCH_APPLIED 승급."""
        payload = {
            "result_code": "PATCH_PROPOSED",
            "summary": "패치 제안됨",
            "suggested_patch": VALID_PATCH,
            "target_file": "dummy.py",
        }
        settings = _settings(auto_apply=True, skip=False)

        with patch("jemmin.mini_reviewer.generate_review", return_value=payload), \
             patch("jemmin.mini_reviewer.targets_from_args", return_value=["dummy.py"]), \
             patch("jemmin.mini_reviewer._apply_patch_safely", return_value=True) as mock_apply, \
             patch("jemmin.mini_reviewer.append_review_logs"), \
             patch("jemmin.mini_reviewer.write_latest_review_files"):
            from jemmin.mini_reviewer import review_targets
            results = review_targets(["dummy.py"], staged=False, settings=settings)

        assert len(results) == 1
        assert results[0]["result_code"] == "PATCH_APPLIED"
        assert "[Auto-Fixed]" in results[0]["summary"]
        mock_apply.assert_called_once_with(VALID_PATCH)

    def test_patch_proposed_kept_on_apply_failure(self):
        """패치 적용 실패 시 PATCH_PROPOSED 상태가 유지됩니다."""
        payload = {
            "result_code": "PATCH_PROPOSED",
            "summary": "패치 제안됨",
            "suggested_patch": VALID_PATCH,
            "target_file": "dummy.py",
        }
        settings = _settings(auto_apply=True, skip=False)

        with patch("jemmin.mini_reviewer.generate_review", return_value=payload), \
             patch("jemmin.mini_reviewer.targets_from_args", return_value=["dummy.py"]), \
             patch("jemmin.mini_reviewer._apply_patch_safely", return_value=False), \
             patch("jemmin.mini_reviewer.append_review_logs"), \
             patch("jemmin.mini_reviewer.write_latest_review_files"):
            from jemmin.mini_reviewer import review_targets
            results = review_targets(["dummy.py"], staged=False, settings=settings)

        assert results[0]["result_code"] == "PATCH_PROPOSED"

    def test_no_auto_apply_when_disabled(self):
        """auto_apply=False 시 _apply_patch_safely가 호출되지 않습니다."""
        payload = {
            "result_code": "PATCH_PROPOSED",
            "summary": "패치 제안됨",
            "suggested_patch": VALID_PATCH,
            "target_file": "dummy.py",
        }
        settings = _settings(auto_apply=False, skip=False)

        with patch("jemmin.mini_reviewer.generate_review", return_value=payload), \
             patch("jemmin.mini_reviewer.targets_from_args", return_value=["dummy.py"]), \
             patch("jemmin.mini_reviewer._apply_patch_safely") as mock_apply, \
             patch("jemmin.mini_reviewer.append_review_logs"), \
             patch("jemmin.mini_reviewer.write_latest_review_files"):
            from jemmin.mini_reviewer import review_targets
            results = review_targets(["dummy.py"], staged=False, settings=settings)

        mock_apply.assert_not_called()
        assert results[0]["result_code"] == "PATCH_PROPOSED"

    def test_review_passed_not_touched(self):
        """REVIEW_PASSED 결과는 auto_apply와 무관하게 그대로 유지됩니다."""
        payload = {
            "result_code": "REVIEW_PASSED",
            "summary": "코드 문제 없음",
            "suggested_patch": None,
            "target_file": "clean.py",
        }
        settings = _settings(auto_apply=True, skip=False)

        with patch("jemmin.mini_reviewer.generate_review", return_value=payload), \
             patch("jemmin.mini_reviewer.targets_from_args", return_value=["clean.py"]), \
             patch("jemmin.mini_reviewer._apply_patch_safely") as mock_apply, \
             patch("jemmin.mini_reviewer.append_review_logs"), \
             patch("jemmin.mini_reviewer.write_latest_review_files"):
            from jemmin.mini_reviewer import review_targets
            results = review_targets(["clean.py"], staged=False, settings=settings)

        mock_apply.assert_not_called()
        assert results[0]["result_code"] == "REVIEW_PASSED"

"""Auto-fix Service — PATCH_PROPOSED 시 git apply 기반 자동 패치 적용 + 재검증.

L3 LLM이 PATCH_PROPOSED를 반환하면:
  1. suggested_patch를 git apply --check로 dry-run 검증
  2. 검증 통과 시 git apply로 원자적 적용
  3. (선택) 재검증 — pytest 등으로 패치가 깨뜨리지 않았는지 확인
  4. 실패 시 git checkout으로 롤백

절대 금지: 파이썬 코드가 대상 파일을 open('w')로 직접 덮어쓰는 행위.
반드시 git apply를 통해서만 적용합니다.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger(__name__)

__all__ = ["AutoFixService", "AutoFixResult"]

_GIT_APPLY_TIMEOUT = 10  # seconds
_GIT_CHECKOUT_TIMEOUT = 10  # seconds


@dataclass(slots=True)
class AutoFixResult:
    """자동 패치 적용 결과."""

    applied: bool = False
    rolled_back: bool = False
    reason: str = ""
    patch_text: str = ""
    verify_passed: bool | None = None  # None = 재검증 미실행


class AutoFixService:
    """git apply 기반 Auto-fix 서비스.

    사용법::

        svc = AutoFixService(project_root=Path("."))
        result = svc.apply_patch(patch_text, target_file="src/main.py")
        if result.applied:
            print("패치 적용 성공")
    """

    def __init__(
        self,
        project_root: Path | None = None,
        *,
        verify_command: list[str] | None = None,
        verify_timeout: int = 60,
        auto_rollback: bool = True,
    ) -> None:
        self._root = project_root or Path.cwd()
        self._verify_cmd = verify_command  # e.g. ["python", "-m", "pytest", "-x", "-q"]
        self._verify_timeout = verify_timeout
        self._auto_rollback = auto_rollback

    def apply_patch(
        self,
        patch_text: str,
        *,
        target_file: str = "",
    ) -> AutoFixResult:
        """패치를 git apply로 적용합니다.

        1. git apply --check (dry-run)
        2. git apply (실제 적용)
        3. verify_command가 설정되어 있으면 재검증
        4. 재검증 실패 + auto_rollback 시 git checkout으로 롤백
        """
        if not patch_text or not patch_text.strip():
            return AutoFixResult(reason="빈 패치")

        # 1-2. git apply
        ok, reason = self._git_apply(patch_text)
        if not ok:
            return AutoFixResult(reason=reason, patch_text=patch_text)

        _logger.info("[AutoFix] 패치 적용 성공: %s", target_file or "(unknown)")

        # 3. 재검증 (선택)
        if self._verify_cmd:
            verify_ok = self._run_verify()
            if not verify_ok:
                _logger.warning("[AutoFix] 재검증 실패, 롤백 시도: %s", target_file)
                if self._auto_rollback and target_file:
                    self._rollback(target_file)
                    return AutoFixResult(
                        applied=False,
                        rolled_back=True,
                        reason="재검증 실패 → 롤백 완료",
                        patch_text=patch_text,
                        verify_passed=False,
                    )
                return AutoFixResult(
                    applied=True,
                    reason="재검증 실패 (롤백 미설정)",
                    patch_text=patch_text,
                    verify_passed=False,
                )
            return AutoFixResult(
                applied=True,
                reason="패치 적용 + 재검증 통과",
                patch_text=patch_text,
                verify_passed=True,
            )

        return AutoFixResult(
            applied=True,
            reason="패치 적용 성공 (재검증 미설정)",
            patch_text=patch_text,
        )

    def _git_apply(self, patch_text: str) -> tuple[bool, str]:
        """git apply --check → git apply. Returns (success, reason)."""
        fd: int = -1
        temp_path: str = ""
        try:
            fd, temp_path = tempfile.mkstemp(suffix=".diff", prefix="autofix_")
            with os.fdopen(fd, "wb") as f:
                fd = -1
                f.write(patch_text.encode("utf-8"))

            # dry-run
            check = subprocess.run(
                ["git", "apply", "--check", temp_path],
                cwd=str(self._root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_GIT_APPLY_TIMEOUT,
            )
            if check.returncode != 0:
                reason = check.stderr.strip() or check.stdout.strip() or "unknown"
                _logger.warning("[AutoFix] git apply --check 실패: %s", reason)
                return False, f"dry-run 실패: {reason}"

            # 실제 적용
            apply = subprocess.run(
                ["git", "apply", temp_path],
                cwd=str(self._root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_GIT_APPLY_TIMEOUT,
            )
            if apply.returncode != 0:
                reason = apply.stderr.strip() or apply.stdout.strip() or "unknown"
                _logger.warning("[AutoFix] git apply 실패: %s", reason)
                return False, f"적용 실패: {reason}"

            return True, ""

        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            _logger.error("[AutoFix] git apply 오류: %s", exc)
            return False, str(exc)
        finally:
            if fd >= 0:
                os.close(fd)
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)

    def _run_verify(self) -> bool:
        """재검증 명령 실행. Returns True if passed."""
        if not self._verify_cmd:
            return True
        try:
            result = subprocess.run(
                self._verify_cmd,
                cwd=str(self._root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._verify_timeout,
            )
            if result.returncode == 0:
                _logger.info("[AutoFix] 재검증 통과")
                return True
            _logger.warning(
                "[AutoFix] 재검증 실패 (rc=%d): %s",
                result.returncode,
                result.stderr[:200],
            )
            return False
        except (subprocess.TimeoutExpired, OSError) as exc:
            _logger.error("[AutoFix] 재검증 오류: %s", exc)
            return False

    def _rollback(self, target_file: str) -> None:
        """git checkout으로 파일 롤백."""
        try:
            subprocess.run(
                ["git", "checkout", "--", target_file],
                cwd=str(self._root),
                capture_output=True,
                timeout=_GIT_CHECKOUT_TIMEOUT,
            )
            _logger.info("[AutoFix] 롤백 완료: %s", target_file)
        except (subprocess.TimeoutExpired, OSError) as exc:
            _logger.error("[AutoFix] 롤백 실패: %s", exc)

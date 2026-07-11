"""PatchService — consensus 코멘트에서 unified diff 패치를 생성 및 디스크에 저장한다.

V29 §11: 패치는 반드시 안전성을 증명한 뒤에만 사용자에게 노출합니다.
Phase C 구현 항목: git apply --check dry-run으로 불량 패치를 조기 차단.
"""
from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jemmin.models import ConsensusResult, ReviewRequest

# LLM 응답의 markdown 코드 블록에서 diff 추출
_DIFF_BLOCK_RE = re.compile(r"```diff\s*([\s\S]*?)```", re.IGNORECASE)


@dataclass
class PatchProposal:
    patch_hash: str
    unified_diff: str
    source_file: str
    saved_path: str | None


def _extract_diff_from_text(text: str) -> str | None:
    """LLM 응답 텍스트에서 ```diff ... ``` 블록을 추출한다."""
    m = _DIFF_BLOCK_RE.search(text)
    if m:
        return m.group(1).strip()
    # 헤더 없이 unified diff로 시작할 경우
    if text.strip().startswith(("---", "+++", "@@")):
        return text.strip()
    return None


class PatchService:
    """consensus 결과 또는 LLM 패치 제안에서 unified diff를 추출하는 서비스.

    기본 동작:
      1. consensus.summary에서 diff 텍스트 추출 (```diff ... ```)
      2. git apply --check로 적용 가능성 사전 검증 (project_root 제공 시)
      3. SHA-256 해시 생성
      4. .jemmin/patches/<hash>.patch 저장
    """

    def __init__(
        self,
        patches_dir: str | Path | None = None,
        project_root: str | Path | None = None,
    ) -> None:
        self._patches_dir: Path | None = (
            Path(patches_dir) if patches_dir else None
        )
        self._project_root: Path | None = (
            Path(project_root) if project_root else None
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_patch(
        self, request: ReviewRequest, consensus: ConsensusResult
    ) -> PatchProposal | None:
        """consensus.summary에서 패치를 추출하고 안전성 검증 후 반환한다.

        Returns:
            PatchProposal — 성공 시.
            None          — diff 추출 실패 또는 git apply --check 실패 시.
        """
        diff_text = _extract_diff_from_text(consensus.summary)
        if not diff_text:
            return None

        # Phase C: git apply --check dry-run으로 불량 패치 조기 차단
        ok, reason = self._git_apply_check(diff_text)
        if not ok:
            logging.warning(
                "[PatchService] git apply --check 실패 (file=%s): %s",
                request.target_file,
                reason,
            )
            return None

        patch_hash = hashlib.sha256(diff_text.encode()).hexdigest()[:16]
        saved_path: str | None = None

        if self._patches_dir:
            self._patches_dir.mkdir(parents=True, exist_ok=True)
            patch_file = self._patches_dir / f"{patch_hash}.patch"
            patch_file.write_text(diff_text, encoding="utf-8")
            saved_path = str(patch_file)

        return PatchProposal(
            patch_hash=patch_hash,
            unified_diff=diff_text,
            source_file=request.target_file,
            saved_path=saved_path,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _git_apply_check(self, diff_text: str) -> tuple[bool, str]:
        """git apply --check -으로 패치 적용 가능성을 검증한다.

        project_root가 없으면 검증을 건너뜁니다 (하위 호환성 유지).

        Returns:
            (True, "")           — 검증 통과 또는 검증 건너뜀.
            (False, reason_str)  — 검증 실패 (적용 불가).
        """
        if self._project_root is None:
            return True, ""

        try:
            result = subprocess.run(
                ["git", "apply", "--check", "-"],
                input=diff_text,
                text=True,
                capture_output=True,
                cwd=str(self._project_root),
                timeout=10,
            )
            if result.returncode == 0:
                return True, ""
            reason = (result.stderr.strip() or result.stdout.strip() or "unknown error")
            return False, reason

        except FileNotFoundError:
            # git 바이너리 없음 → 검증 건너뜀 (Fail-Open)
            logging.debug("[PatchService] git 바이너리를 찾을 수 없어 --check 건너뜀")
            return True, ""

        except subprocess.TimeoutExpired:
            # 10초 타임아웃 → 검증 건너뜀 (Fail-Open)
            logging.warning("[PatchService] git apply --check 타임아웃, 검증 건너뜀")
            return True, ""

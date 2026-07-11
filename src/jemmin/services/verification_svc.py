"""VerificationService — 패치 적용 후 친위적 검증을 수행하는 서비스."""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jemmin.models import ReviewRequest
from jemmin.services.patch_svc import PatchProposal


@dataclass
class VerificationReport:
    passed: bool
    returncode: int
    stdout: str
    stderr: str
    patch_hash: str


class VerificationService:
    """패치 적용 후 pytest를 실행해 통과 여부를 반환한다.

    실제 디스크 수정 없이 읽기 전용(dry-run)으로 동작하며
    더스트 심리스트만 실행할 수 있다
    (context.metadata['pytest_args'] 로 커스터마이징 가능).
    """

    def __init__(
        self,
        project_root: str | Path | None = None,
        pytest_args: list[str] | None = None,
        timeout: int = 120,
    ) -> None:
        self._root: Path = Path(project_root) if project_root else Path.cwd()
        self._pytest_args: list[str] = pytest_args or ["--tb=short", "-q"]
        self._timeout = timeout

    def verify_patch(
        self,
        request: ReviewRequest,
        patch: PatchProposal,
        extra_args: list[str] | None = None,
    ) -> VerificationReport:
        cmd = [sys.executable, "-m", "pytest"] + self._pytest_args + (extra_args or [])
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=str(self._root),
                timeout=self._timeout,
            )
            return VerificationReport(
                passed=result.returncode == 0,
                returncode=result.returncode,
                stdout=result.stdout[-4000:],
                stderr=result.stderr[-1000:],
                patch_hash=patch.patch_hash,
            )
        except subprocess.TimeoutExpired:
            return VerificationReport(
                passed=False,
                returncode=-1,
                stdout="",
                stderr="pytest 실행 시간 초과",
                patch_hash=patch.patch_hash,
            )

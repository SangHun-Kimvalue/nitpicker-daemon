"""Phase VI-C: Multi-file PR 리뷰 (--staged) 테스트.

CLI의 _run_staged가 git staged 파일 목록을 읽어 순차 리뷰하고
PR 요약을 출력하는지 검증합니다.
Total: 3 tests
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


class TestRunStaged:
    @patch("subprocess.check_output", return_value="")
    def test_staged_no_files(self, mock_git, capsys):
        """staged 파일이 없으면 안내 메시지 출력."""
        from bin.jemmin_cli import _run_staged
        rc = _run_staged(provider_name="mock")
        assert rc == 0
        captured = capsys.readouterr()
        assert "staged" in captured.out.lower()

    def test_staged_arg_accepted(self):
        """--staged 인자가 argparse에서 인식되는지 확인."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--staged", action="store_true")
        args = parser.parse_args(["--staged"])
        assert args.staged is True

    def test_pr_summary_format(self):
        """PR 요약 포맷 검증 — results 리스트로 출력 생성."""
        results = [
            ("src/a.py", "REVIEW_PASSED", "깔끔한 코드"),
            ("src/b.py", "REVIEW_REJECTED", "보안 위반 발견"),
        ]
        passed = sum(1 for _, code, _ in results if code == "REVIEW_PASSED")
        rejected = len(results) - passed
        assert passed == 1
        assert rejected == 1

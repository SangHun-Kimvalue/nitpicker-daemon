from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jemmin.models import ReviewResult

_KST = timezone(timedelta(hours=9), name="KST")

_STATUS_KR: dict[str, str] = {
    "pass": "통과",
    "rejected": "거부",
    "degraded": "저하",
    "failed": "실패",
    "ignored": "무시",
}

# Module-level compiled regex — Hot-path 규칙: 함수 내부 re.compile 금지
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _parse_hunk_ranges(unified_diff: str) -> list[dict[str, Any]]:
    """Parse unified diff hunk headers into line ranges for TextEdit conversion.

    Each @@ -old_start,old_len +new_start,new_len @@ header produces an entry
    with the old-file range (for replacement) and the new-file lines (replacement text).
    """
    hunks: list[dict[str, Any]] = []
    current_hunk: dict[str, Any] | None = None

    for raw_line in unified_diff.splitlines():
        m = _HUNK_RE.match(raw_line)
        if m:
            if current_hunk is not None:
                hunks.append(current_hunk)
            old_start = int(m.group(1))
            old_len = int(m.group(2)) if m.group(2) else 1
            current_hunk = {
                "start_line": old_start - 1,  # 0-based for LSP
                "end_line": old_start - 1 + old_len,
                "new_lines": [],
            }
            continue

        if current_hunk is None:
            continue

        if raw_line.startswith("-"):
            pass  # removed line — already covered by range
        elif raw_line.startswith("+"):
            current_hunk["new_lines"].append(raw_line[1:])
        elif raw_line.startswith(" "):
            current_hunk["new_lines"].append(raw_line[1:])

    if current_hunk is not None:
        hunks.append(current_hunk)
    return hunks


class FileFeedbackService:
    """파일 기반 피드백 서비스.

    - publish_diagnostics: LATEST_REVIEW.txt 텍스트 파일 작성
    - publish_quick_fix: latest_review.json 작성 (LSP diagnostics + code actions)
    - clear_feedback: 피드백 파일 삭제
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._json_path = self._path.parent / "latest_review.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def publish_diagnostics(self, result: ReviewResult, *, target_file: str = "") -> None:
        kst_now = datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S KST")
        status_kr = _STATUS_KR.get(result.status, result.status)
        reviewer = getattr(result, "reviewer", "") or ""
        self._path.write_text(
            (
                f"리뷰 시각: {kst_now}\n"
                f"대상 파일: {target_file}\n"
                f"리뷰어: {reviewer}\n"
                f"결과 코드: {result.result_code or ''}\n"
                f"요약: {result.summary}\n"
                f"신뢰도: {result.confidence_score}\n"
                f"상태: {result.state.value}\n"
                f"판정: {status_kr}\n"
            ),
            encoding="utf-8",
        )

    def publish_quick_fix(
        self,
        result: ReviewResult,
        target_file: str,
        findings: list[dict[str, Any]] | None = None,
        patch: Any | None = None,
    ) -> None:
        """latest_review.json에 diagnostics + code_actions를 기록합니다.

        LSP 서버가 이 파일을 감시하여:
        - textDocument/publishDiagnostics → details 배열
        - textDocument/codeAction → code_actions 배열
        을 클라이언트에 전달합니다.
        """
        details: list[dict[str, Any]] = []
        code_actions: list[dict[str, Any]] = []

        for finding in findings or []:
            line_no = finding.get("line_number") or 1
            severity = finding.get("severity") or "warning"
            code = finding.get("code") or ""
            message = finding.get("message") or finding.get("issue") or "review finding"
            agent = finding.get("agent_name") or ""

            details.append({
                "line_number": line_no,
                "severity": severity,
                "issue": message,
                "code": code,
                "agent": agent,
            })

            # 에이전트가 suggested_fix를 제공한 경우 code action 생성
            suggested_fix = finding.get("suggested_fix")
            if suggested_fix:
                code_actions.append({
                    "title": f"[{code}] {suggested_fix[:80]}",
                    "kind": "quickfix",
                    "line_number": line_no,
                    "edit_text": suggested_fix,
                })

            # noqa 주석 추가 제안 (Python 파일에 한정)
            if target_file.endswith(".py") and code:
                code_actions.append({
                    "title": f"Suppress {code} (# noqa: {code})",
                    "kind": "quickfix.suppress",
                    "line_number": line_no,
                    "suppress_code": code,
                })

        # 패치가 있으면 "Apply Patch" code action 생성
        if patch is not None:
            unified_diff: str = getattr(patch, "unified_diff", "") or ""
            patch_hash: str = getattr(patch, "patch_hash", "") or ""
            if unified_diff:
                hunks = _parse_hunk_ranges(unified_diff)
                code_actions.append({
                    "title": f"Apply verified patch ({patch_hash[:8]})",
                    "kind": "quickfix.patch",
                    "patch_hash": patch_hash,
                    "unified_diff": unified_diff,
                    "edits": hunks,
                })

        data: dict[str, Any] = {
            "request_id": result.request_id,
            "target_file": target_file,
            "state": result.state.value,
            "status": result.status,
            "result_code": result.result_code or "",
            "summary": result.summary,
            "details": details,
            "code_actions": code_actions,
        }
        # Atomic write: 임시 파일에 쓴 뒤 replace — LSP가 반만 쓴 파일을 읽는 Race 방지
        temp_path = self._json_path.with_name(self._json_path.name + ".tmp")
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self._json_path)

    def clear_feedback(self, target_file: str) -> None:
        self._path.unlink(missing_ok=True)
        self._json_path.unlink(missing_ok=True)

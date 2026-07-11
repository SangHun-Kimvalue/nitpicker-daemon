"""ContextAgent — diff 품질 및 변경 범위 검사 에이전트."""
from __future__ import annotations

import re
from typing import Any

from jemmin.models import AgentDecision, ContextBundle, ReviewRequest

_ADDED_LINE_RE = re.compile(r"^\+(?!\+\+)", re.MULTILINE)
_CHANGED_FILE_RE = re.compile(r"^diff --git a/.+ b/(.+)$", re.MULTILINE)

# 한 diff에서 추가된 줄이 이 수를 넘으면 "너무 큰 PR" 경고
_MAX_ADDED_LINES = 400
# 변경 파일이 이 수를 넘으면 "다중 책임" 경고
_MAX_CHANGED_FILES = 8

# 기능 추가처럼 보이는 패턴 (def/class 추가)
_NEW_PUBLIC_DEF_RE = re.compile(r"^\+(?!\+)\s*(def |class )[A-Za-z]", re.MULTILINE)
# 테스트 존재 여부
_TEST_PATTERN_RE = re.compile(r"(def test_|class Test|pytest|unittest)", re.IGNORECASE)
# TODO / FIXME / HACK 마커
_TODO_RE = re.compile(r"^\+(?!\+\+).*\b(TODO|FIXME|HACK|XXX)\b", re.MULTILINE)


class ContextAgent:
    """diff 크기·범위·품질을 검사해 리뷰 전처리 경고를 발행한다."""

    name = "context"

    def __init__(self, provider: Any = None) -> None:
        self._provider = provider

    def run(self, request: ReviewRequest, context: ContextBundle) -> AgentDecision:
        diff = request.diff_text
        findings: list[dict[str, Any]] = []

        # 1. diff 크기
        added_line_count = len(_ADDED_LINE_RE.findall(diff))
        if added_line_count > _MAX_ADDED_LINES:
            findings.append({
                "code": "CTX001",
                "message": (
                    f"추가된 줄 수가 {added_line_count}줄로 기준({_MAX_ADDED_LINES})을 초과. "
                    "더 작은 단위 PR로 분할 권장"
                ),
            })

        # 2. 변경 파일 수
        changed_files = _CHANGED_FILE_RE.findall(diff)
        if len(changed_files) > _MAX_CHANGED_FILES:
            findings.append({
                "code": "CTX002",
                "message": (
                    f"변경 파일 수 {len(changed_files)}개 — 단일 PR의 책임 범위 초과({_MAX_CHANGED_FILES}개 기준). "
                    "관심사별 PR 분리 권장"
                ),
            })

        # 3. 기능 추가인데 테스트 없음
        #    단일 파일 diff에서는 테스트가 별도 파일이므로 오탐이 불가피함.
        #    multi-file diff(2개 이상 변경)일 때만 경고한다.
        has_new_def = bool(_NEW_PUBLIC_DEF_RE.search(diff))
        has_test = bool(_TEST_PATTERN_RE.search(diff))
        is_multi_file = len(changed_files) >= 2
        if has_new_def and not has_test and is_multi_file:
            findings.append({
                "code": "CTX003",
                "message": "새로운 함수/클래스 추가 감지 — 대응하는 테스트 코드가 보이지 않음",
            })

        # 4. TODO/FIXME 마커
        todo_matches = _TODO_RE.findall(diff)
        if todo_matches:
            markers = ", ".join(sorted(set(todo_matches)))
            findings.append({
                "code": "CTX004",
                "message": f"미완성 마커 포함: {markers} — 릴리스 전 해결 또는 이슈 등록 필요",
            })

        # 5. 빈 diff
        if not diff.strip():
            findings.append({
                "code": "CTX005",
                "message": "diff 내용이 비어 있음 — 리뷰 대상이 없습니다",
            })

        if findings:
            return AgentDecision(
                agent_name=self.name,
                status="warn",
                confidence_score=0.8,
                findings=findings,
                suggested_actions=[f"{f['code']}: {f['message']}" for f in findings],
            )

        return AgentDecision(
            agent_name=self.name,
            status="pass",
            confidence_score=0.9,
            findings=[],
            suggested_actions=[],
        )

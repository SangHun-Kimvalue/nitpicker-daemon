"""DiffProvider — git diff 기반 tier1 문맥 수집기.

CompositeContextProvider에 등록하여 사용합니다.
diff_text가 있으면 tier1로 전달하고,
파일 크기/변경 라인 수 등의 메타데이터를 함께 제공합니다.
"""
from __future__ import annotations

from jemmin.models import ReviewRequest

from .base import ContextFragment


class DiffProvider:
    """ReviewRequest.diff_text를 tier1 ContextFragment로 변환합니다."""

    name: str = "diff"

    def collect(self, request: ReviewRequest) -> ContextFragment:
        diff_text: str = request.diff_text or ""
        lines = diff_text.splitlines()
        added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))

        return ContextFragment(
            tier="tier1",
            entries=[diff_text] if diff_text else [],
            metadata={
                "source": "diff",
                "target_file": request.target_file,
                "total_lines": len(lines),
                "added_lines": added,
                "removed_lines": removed,
                "word_count": len(diff_text.split()),
            },
        )

"""Cloud Offload Gateway 스캐폴드 — 장기 실행 리뷰를 원격 워커로 위임합니다.

현재는 StubOffloadGateway만 구현됩니다 (Phase F에서 실제 구현 예정).
PolicyEngine.should_offload()가 True를 반환할 때만 호출됩니다.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(slots=True)
class OffloadRequest:
    """원격 워커로 전달할 리뷰 패키지."""

    request_id: str
    git_revision: str
    context_bundle: str           # 시스템 프롬프트 + 정적 컨텍스트 직렬화
    masked_diff: str              # PII 마스킹된 Git diff
    allowlisted_files: list[str] = field(default_factory=list)
    verification_mode: Literal["strict", "lenient"] = "strict"


@dataclass(slots=True)
class OffloadResult:
    """원격 제출 결과."""

    request_id: str
    accepted: bool
    remote_job_id: str
    status_url: str
    reason: str | None = None


@dataclass(slots=True)
class JobStatus:
    """poll() 반환값."""

    remote_job_id: str
    state: Literal["queued", "running", "done", "failed"]
    progress_pct: float = 0.0
    result: dict[str, Any] | None = None
    error: str | None = None


class StubOffloadGateway:
    """로컬 테스트용 no-op 오프로드 게이트웨이.

    실제 구현은 Phase F에서 HTTP/gRPC 엔드포인트로 교체합니다.
    """

    def __init__(self, base_url: str = "https://stub.example.invalid") -> None:
        self._base_url = base_url
        self._jobs: dict[str, JobStatus] = {}

    def submit(self, request: OffloadRequest) -> OffloadResult:
        """오프로드 작업을 제출하고 즉시 수락 응답을 반환합니다."""
        remote_job_id = f"job-{uuid.uuid4().hex[:16]}"
        self._jobs[remote_job_id] = JobStatus(
            remote_job_id=remote_job_id,
            state="queued",
        )
        return OffloadResult(
            request_id=request.request_id,
            accepted=True,
            remote_job_id=remote_job_id,
            status_url=f"{self._base_url}/jobs/{remote_job_id}",
        )

    def poll(self, remote_job_id: str) -> JobStatus:
        """작업 상태를 조회합니다. 스텁은 항상 'done'을 반환합니다."""
        if remote_job_id not in self._jobs:
            return JobStatus(
                remote_job_id=remote_job_id,
                state="failed",
                error="job not found",
            )
        status = self._jobs[remote_job_id]
        # 스텁: 단순히 queued → done 진행
        if status.state == "queued":
            self._jobs[remote_job_id] = JobStatus(
                remote_job_id=remote_job_id,
                state="done",
                progress_pct=100.0,
                result={"status": "PASS", "reason": "stub offload complete", "patch_code": ""},
            )
        return self._jobs[remote_job_id]

    def cancel(self, remote_job_id: str) -> None:
        """작업 취소 요청 (스텁은 즉시 제거)."""
        self._jobs.pop(remote_job_id, None)

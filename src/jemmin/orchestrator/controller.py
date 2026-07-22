from __future__ import annotations

import logging
import time

from typing import Any

from jemmin.models import AgentDecision, ReviewRequest, ReviewResult, ReviewState
from jemmin.services.artifact_publisher import ArtifactPublisher
from jemmin.state.sqlite_spooler import JobCreationOutcome


_TERMINAL_STATE_VALUES = frozenset({
    ReviewState.PRECHECK_FAILED.value,
    ReviewState.DELIVERED.value,
    ReviewState.DEGRADED.value,
    ReviewState.FAILED.value,
})


class ReviewOrchestrator:
    def __init__(
        self,
        *,
        job_store,
        policy_engine,
        resource_manager,
        context_service,
        agents=None,
        agent_registry=None,
        consensus_engine,
        feedback_service,
        review_logger,
        patch_service=None,
        verification_service=None,
        analytics_logger=None,  # DuckDbLogger (선택)
        context_cache_manager=None,  # ContextCacheManager (선택)
        offload_gateway=None,  # StubOffloadGateway 또는 실제 게이트웨이
        llm_review_gate=None,  # L3 LlmReviewGate (선택 — 없으면 L2 결과만 사용)
        notifier=None,  # NotificationService (선택)
        autofix_service=None,  # AutoFixService (선택 — PATCH_PROPOSED 시 자동 적용)
    ) -> None:
        self._job_store = job_store
        self._policy_engine = policy_engine
        self._resource_manager = resource_manager
        self._context_service = context_service
        self._agents = agents  # 하위 호환: 직접 리스트 전달
        self._agent_registry = agent_registry  # manifest 기반 선택
        self._consensus_engine = consensus_engine
        self._patch_service = patch_service
        self._verification_service = verification_service
        self._context_cache_manager = context_cache_manager
        self._offload_gateway = offload_gateway
        self._llm_review_gate = llm_review_gate
        self._autofix_service = autofix_service

        # ArtifactPublisher: 출력 채널을 합성 레이어로 통합
        self._publisher = ArtifactPublisher(
            feedback_service=feedback_service,
            review_logger=review_logger,
            analytics_logger=analytics_logger,
            notifier=notifier,
        )

    @property
    def _analytics_logger(self) -> Any:
        """하위 호환 — 기존 테스트가 _analytics_logger에 직접 접근하는 경우 대비."""
        return self._publisher._analytics

    def _record_stage_event_best_effort(
        self,
        request_id: str,
        *,
        stage: str,
        phase: str,
        result_code: str,
        **details: Any,
    ) -> None:
        payload = {
            "stage": stage,
            "phase": phase,
            "result_code": result_code,
            **details,
        }
        try:
            self._job_store.append_event(request_id, "review.stage", payload)
        except Exception as error:
            logging.warning(
                "[Orchestrator] Failed to record stage %s/%s for %s: %s",
                stage,
                phase,
                request_id,
                error,
            )

    def run_once(self, request: ReviewRequest) -> ReviewResult:
        """
        동기 기반의 리뷰 파이프라인.

        Phase A 스캐폴딩과 CLI 환경에 맞춘 안정성 우선 구현이다.
        향후 async 오케스트레이터가 필요하면 별도 run_once_async()를 추가하고,
        현재 메서드는 sync wrapper 또는 to_thread 경로로 유지하는 편이 안전하다.
        """
        current_state = ReviewState.QUEUED
        request_persisted = False

        try:
            creation_outcome = self._job_store.create_request(request)
            if not isinstance(creation_outcome, JobCreationOutcome):
                request_persisted = True
            else:
                if creation_outcome.duplicate:
                    return self._handle_duplicate_request(
                        request,
                        existing_state=creation_outcome.existing_state,
                    )
                request_persisted = creation_outcome.created

            if not self._resource_manager.allow_new_job(request):
                result = self._build_result(
                    request,
                    ReviewState.DEGRADED,
                    "degraded",
                    "resource manager rejected the request",
                    result_code="RESOURCE_REJECTED",
                )
                self._mark_terminal_safe(
                    request.request_id,
                    ReviewState.DEGRADED,
                    reason="resource budget exceeded",
                    result_code="RESOURCE_REJECTED",
                )
                self._publisher.log_result(result)
                return result

            decision = self._policy_engine.evaluate_request(request)
            self._publisher.log_analytics(
                request.request_id,
                event_type="policy_decision",
                status="fast_path" if decision.fast_path_only else "heavy_path",
                agent_name="policy_engine",
            )
            if not decision.accepted:
                self._transition_state(
                    request.request_id,
                    ReviewState.QUEUED,
                    ReviewState.PRECHECK_FAILED,
                    decision.reason or "precheck failed",
                    result_code="POLICY_REJECTED",
                )
                current_state = ReviewState.PRECHECK_FAILED
                result = self._build_result(
                    request,
                    ReviewState.PRECHECK_FAILED,
                    "rejected",
                    decision.reason or "precheck failed",
                    1.0,
                    result_code="POLICY_REJECTED",
                )
                self._mark_terminal_safe(
                    request.request_id,
                    ReviewState.PRECHECK_FAILED,
                    reason=decision.reason or "precheck failed",
                    result_code="POLICY_REJECTED",
                )
                self._publisher.publish_diagnostics(result, target_file=request.target_file)
                self._publisher.log_result(result)
                return result

            self._transition_state(
                request.request_id,
                ReviewState.QUEUED,
                ReviewState.CONTEXT_READY,
                "context build start",
                result_code="CONTEXT_BUILD_STARTED",
            )
            current_state = ReviewState.CONTEXT_READY
            context = self._context_service.build_context(request)

            # Phase III: Offload Gateway — context_size가 임계값 초과 시 원격 위임 시도
            if self._offload_gateway is not None and hasattr(self._policy_engine, "should_offload"):
                should_offload = self._policy_engine.should_offload(request, context.token_estimate)
                if should_offload:
                    try:
                        from jemmin.ipc.offload_gateway import OffloadRequest
                        offload_req = OffloadRequest(
                            request_id=request.request_id,
                            git_revision=request.git_revision,
                            context_bundle=str(context.context_hash),
                            masked_diff=request.diff_text or "",
                        )
                        offload_result = self._offload_gateway.submit(offload_req)
                        self._publisher.log_analytics(
                            request.request_id,
                            event_type="offload_submitted",
                            status="accepted" if offload_result.accepted else "rejected",
                            agent_name="offload_gateway",
                        )
                        logging.info(
                            "[Orchestrator] Offload submitted for %s: job=%s",
                            request.request_id,
                            offload_result.remote_job_id,
                        )
                        # 현재는 로컬 실행 계속 — 향후 REMOTE_PRIMARY 모드에서는 여기서 early return
                    except Exception as offload_err:
                        logging.warning(
                            "[Orchestrator] Offload failed for %s: %s — falling back to local",
                            request.request_id,
                            offload_err,
                        )

            # Phase I: Context Cache 활용 — 동일 시스템 프롬프트 재사용 시 토큰 절감
            cache_id: str | None = None
            if self._context_cache_manager is not None:
                try:
                    system_prompt: str = context.metadata.get("system_prompt", "")
                    static_ctx: str = "\n".join(context.tiers.get("tier2", []) + context.tiers.get("tier3", []))
                    if system_prompt or static_ctx:
                        cache_id = self._context_cache_manager.get_or_create(system_prompt, static_ctx)
                        self._publisher.log_analytics(
                            request.request_id,
                            event_type="context_cache",
                            status="hit" if self._context_cache_manager.stats.hits > 0 else "miss",
                            agent_name="context_cache_manager",
                        )
                except (RuntimeError, ConnectionError, TimeoutError, OSError) as cache_err:
                    logging.warning(
                        "[Orchestrator] Context cache failed for %s: %s",
                        request.request_id,
                        cache_err,
                    )

            self._transition_state(
                request.request_id,
                ReviewState.CONTEXT_READY,
                ReviewState.ANALYZING,
                "agents start",
                result_code="AGENTS_STARTED",
            )
            current_state = ReviewState.ANALYZING

            # 에이전트 리스트 결정: registry 우선, 없으면 직접 리스트 사용
            if self._agent_registry is not None:
                all_agents = self._agent_registry.select(
                    project_profile=request.project_profile or "general",
                    trigger_intent=request.trigger_intent or "active_intent",
                )
            else:
                all_agents = self._agents or []

            # fast_path_only(passive_save) 시 허용 에이전트만 실행한다.
            # allowed_agents=None 이면 전체 에이전트 실행(active_intent).
            active_agents = [
                a for a in all_agents
                if decision.allowed_agents is None
                or getattr(a, "name", None) in decision.allowed_agents
            ]
            t_agents_start = time.monotonic()
            decisions: list[AgentDecision] = []
            total_agents = len(active_agents)
            for ordinal, agent in enumerate(active_agents, start=1):
                agent_name = getattr(agent, "name", None)
                if not isinstance(agent_name, str) or not agent_name:
                    agent_name = agent.__class__.__name__
                self._record_stage_event_best_effort(
                    request.request_id,
                    stage="agent",
                    phase="started",
                    result_code="AGENT_STARTED",
                    agent_name=agent_name,
                    ordinal=ordinal,
                    total=total_agents,
                )
                t_agent_start = time.monotonic()
                try:
                    agent_decision = agent.run(request, context)
                except Exception as error:
                    agent_latency_ms = (time.monotonic() - t_agent_start) * 1000
                    self._record_stage_event_best_effort(
                        request.request_id,
                        stage="agent",
                        phase="failed",
                        result_code="AGENT_FAILED",
                        agent_name=agent_name,
                        ordinal=ordinal,
                        total=total_agents,
                        latency_ms=agent_latency_ms,
                        exception_type=type(error).__name__,
                    )
                    raise
                agent_latency_ms = (time.monotonic() - t_agent_start) * 1000
                decisions.append(agent_decision)
                self._record_stage_event_best_effort(
                    request.request_id,
                    stage="agent",
                    phase="completed",
                    result_code="AGENT_COMPLETED",
                    agent_name=agent_name,
                    ordinal=ordinal,
                    total=total_agents,
                    latency_ms=agent_latency_ms,
                    status=agent_decision.status,
                )
            latency_ms = (time.monotonic() - t_agents_start) * 1000
            self._publisher.log_analytics(
                request.request_id,
                event_type="agents_run",
                status="done",
                latency_ms=latency_ms,
                findings_cnt=sum(1 for d in decisions if d.status != "pass"),
            )

            self._transition_state(
                request.request_id,
                ReviewState.ANALYZING,
                ReviewState.CONSENSUS_REACHED,
                "agent decisions collected",
                result_code="CONSENSUS_REACHED",
            )
            current_state = ReviewState.CONSENSUS_REACHED
            consensus = self._consensus_engine.decide(decisions)

            final_state = ReviewState.DELIVERED
            final_status = "pass" if consensus.status == "pass" else "rejected"
            final_summary = consensus.summary
            final_result_code = "REVIEW_PASSED" if consensus.status == "pass" else "REVIEW_REJECTED"
            patch_proposal = None  # 명시적 선언 — locals() 사용 금지

            # L3: LLM 최종 리뷰 — consensus PASS 시에만 호출
            if consensus.status == "pass" and self._llm_review_gate is not None:
                self._publisher.log_analytics(
                    request.request_id,
                    event_type="l3_review_start",
                    status="started",
                )
                t_l3_start = time.monotonic()
                all_findings_for_l3 = self._collect_findings(decisions)
                self._record_stage_event_best_effort(
                    request.request_id,
                    stage="l3",
                    phase="started",
                    result_code="L3_STARTED",
                )
                try:
                    llm_result = self._llm_review_gate.review(request, context, all_findings_for_l3)
                except Exception as error:
                    l3_latency_ms = (time.monotonic() - t_l3_start) * 1000
                    self._record_stage_event_best_effort(
                        request.request_id,
                        stage="l3",
                        phase="failed",
                        result_code="L3_FAILED",
                        latency_ms=l3_latency_ms,
                        exception_type=type(error).__name__,
                    )
                    raise
                l3_latency_ms = (time.monotonic() - t_l3_start) * 1000
                self._record_stage_event_best_effort(
                    request.request_id,
                    stage="l3",
                    phase="completed",
                    result_code="L3_COMPLETED",
                    latency_ms=l3_latency_ms,
                )
                l3_code = llm_result.get("result_code", "REVIEW_PASSED")
                self._publisher.log_analytics(
                    request.request_id,
                    event_type="l3_review_complete",
                    status="done",
                    result_code=l3_code,
                    latency_ms=l3_latency_ms,
                )

                if l3_code == "REVIEW_PASSED":
                    final_summary = llm_result.get("summary") or consensus.summary
                    final_result_code = "REVIEW_PASSED"
                    final_status = "pass"
                elif l3_code == "PATCH_PROPOSED":
                    final_summary = llm_result.get("summary") or consensus.summary
                    final_result_code = "PATCH_PROPOSED"
                    final_status = "rejected"
                else:
                    # REVIEW_REJECTED or unknown
                    final_summary = llm_result.get("summary") or consensus.summary
                    final_result_code = l3_code
                    final_status = "rejected"
                confidence_from_llm = llm_result.get("confidence_score")
                if isinstance(confidence_from_llm, (int, float)):
                    consensus = consensus.__class__(
                        status=consensus.status,
                        summary=final_summary,
                        confidence_score=float(confidence_from_llm),
                        winning_reasons=consensus.winning_reasons,
                        conflicting_agents=consensus.conflicting_agents,
                    )

            if consensus.status == "patch":
                if self._patch_service and self._verification_service:
                    # Phase F: PatchService → VerificationService 파이프라인
                    patch_proposal = self._patch_service.create_patch(request, consensus)
                    if patch_proposal is None:
                        # diff를 추출하지 못한 경우 → 검증 없이 PATCH_REQUIRED로 처리
                        final_status = "rejected"
                        final_summary = f"[PATCH REQUIRED] {consensus.summary}"
                        final_result_code = "PATCH_REQUIRED"
                    else:
                        verify_report = self._verification_service.verify_patch(
                            request, patch_proposal
                        )
                        self._publisher.log_analytics(
                            request.request_id,
                            event_type="patch_verify",
                            status="verified" if verify_report.passed else "rejected",
                            agent_name="verification_service",
                            confidence=1.0 if verify_report.passed else 0.0,
                        )
                        if verify_report.passed:
                            final_status = "patch"
                            final_summary = f"[PATCH VERIFIED] {consensus.summary}"
                            final_result_code = "PATCH_PROPOSED"
                        else:
                            rc = verify_report.returncode
                            final_status = "rejected"
                            final_summary = f"[PATCH REJECTED] tests failed (rc={rc}): {consensus.summary}"
                            final_result_code = "PATCH_VERIFY_FAILED"
                else:
                    # patch_service 없으면 종전대로 rejected 처리
                    final_status = "rejected"
                    final_summary = f"[PATCH REQUIRED] {consensus.summary}"
                    final_result_code = "PATCH_REQUIRED"

            # ── Auto-fix: PATCH_PROPOSED 시 자동 패치 적용 ──
            if final_result_code == "PATCH_PROPOSED" and self._autofix_service:
                suggested_patch = ""
                if self._llm_review_gate and llm_result:
                    suggested_patch = llm_result.get("suggested_patch") or ""
                if suggested_patch:
                    fix_result = self._autofix_service.apply_patch(
                        suggested_patch, target_file=request.target_file,
                    )
                    self._publisher.log_analytics(
                        request.request_id,
                        event_type="autofix",
                        status="applied" if fix_result.applied else "failed",
                        agent_name="autofix_service",
                    )
                    if fix_result.applied:
                        final_summary = f"[Auto-Fixed] {final_summary}"
                        final_result_code = "PATCH_APPLIED"
                        if fix_result.verify_passed is False:
                            final_summary = f"[Auto-Fix Rolled Back] {consensus.summary}"
                            final_result_code = "PATCH_ROLLBACK"
                            final_status = "rejected"

            result = self._build_result(
                request,
                final_state,
                final_status,
                final_summary,
                consensus.confidence_score,
                result_code=final_result_code,
            )

            self._mark_terminal_safe(
                request.request_id,
                final_state,
                reason="pipeline completed normally",
                result_code=final_result_code,
            )
            self._publisher.log_analytics(
                request.request_id,
                event_type="pipeline_complete",
                status=final_status,
                result_code=final_result_code,
                confidence=result.confidence_score,
            )
            self._publisher.publish_diagnostics(result, target_file=request.target_file)

            # Phase I: Quick-Fix — 에이전트 findings + 패치를 JSON으로 기록
            all_findings = self._collect_findings(decisions)
            self._publisher.publish_quick_fix(result, request.target_file, all_findings, patch_proposal)

            self._publisher.log_result(result)
            return result

        except Exception as error:
            logging.error(
                "[Orchestrator] Pipeline failed for %s: %s",
                request.request_id,
                error,
                exc_info=True,
            )

            fallback_state, fallback_status, fallback_result_code = self._classify_failure(error)

            if request_persisted and current_state not in self._terminal_states() and current_state != fallback_state:
                self._record_failure_transition_best_effort(
                    request.request_id,
                    current_state,
                    fallback_state,
                    reason=f"pipeline exception: {error}",
                    result_code=fallback_result_code,
                )
                current_state = fallback_state

            if request_persisted:
                self._mark_terminal_safe(
                    request.request_id,
                    fallback_state,
                    reason=f"pipeline exception: {error}",
                    result_code=fallback_result_code,
                )

            result = self._build_result(
                request,
                fallback_state,
                fallback_status,
                self._build_user_failure_summary(error, fallback_result_code),
                result_code=fallback_result_code,
            )
            self._publisher.publish_diagnostics(result, target_file=request.target_file)
            self._publisher.log_result(result)
            return result

    def _build_result(
        self,
        request: ReviewRequest,
        state: ReviewState,
        status: str,
        summary: str,
        confidence_score: float = 0.0,
        result_code: str | None = None,
    ) -> ReviewResult:
        return ReviewResult(
            request_id=request.request_id,
            state=state,
            status=status,
            summary=summary,
            confidence_score=confidence_score,
            result_code=result_code,
            reviewer=request.metadata.get("reviewer", ""),
        )

    def _transition_state(
        self,
        request_id: str,
        from_state: ReviewState,
        to_state: ReviewState,
        reason: str,
        result_code: str | None = None,
    ) -> None:
        self._job_store.transition_state(request_id, from_state, to_state, reason, result_code=result_code)

    def _record_failure_transition_best_effort(
        self,
        request_id: str,
        from_state: ReviewState,
        to_state: ReviewState,
        reason: str,
        result_code: str,
    ) -> None:
        try:
            self._transition_state(request_id, from_state, to_state, reason, result_code=result_code)
        except Exception as error:
            logging.warning(
                "[Orchestrator] Failed to record failure transition %s -> %s for %s: %s",
                from_state.value,
                to_state.value,
                request_id,
                error,
            )

    def _mark_terminal_safe(
        self,
        request_id: str,
        state: ReviewState,
        reason: str = "",
        result_code: str | None = None,
    ) -> None:
        try:
            self._job_store.mark_terminal(request_id, state, reason=reason, result_code=result_code)
        except Exception as error:
            logging.critical(
                "[Orchestrator] Failed to mark terminal state (%s) for %s: %s; reason=%s",
                state.value,
                request_id,
                error,
                reason,
            )

    def _handle_duplicate_request(
        self,
        request: ReviewRequest,
        *,
        existing_state: str | None,
    ) -> ReviewResult:
        if self._is_terminal_state_value(existing_state):
            summary = (
                "동일한 코드 변경 내역에 대한 리뷰가 이미 완료되었습니다. "
                f"새로운 변경 사항을 저장해 주세요. (기존 상태: {existing_state})"
            )
        else:
            summary = (
                "동일한 코드 변경 내역에 대한 리뷰가 이미 진행 중입니다. "
                f"잠시 후 결과를 확인해 주세요. (기존 상태: {existing_state or 'unknown'})"
            )

        result = self._build_result(
            request,
            ReviewState.DELIVERED,
            "ignored",
            summary,
            1.0,
            result_code="DUPLICATE_REQUEST_IGNORED",
        )
        self._publisher.log_analytics(
            request.request_id,
            event_type="duplicate_request",
            status="ignored",
            result_code="DUPLICATE_REQUEST_IGNORED",
            confidence=1.0,
        )
        self._publisher.publish_diagnostics(result, target_file=request.target_file)
        self._publisher.log_result(result)
        return result

    def _classify_failure(self, error: Exception) -> tuple[ReviewState, str, str]:
        if isinstance(error, TimeoutError):
            return ReviewState.DEGRADED, "degraded", "LLM_TIMEOUT"
        if isinstance(error, ConnectionError):
            return ReviewState.DEGRADED, "degraded", "LLM_CONNECTION_ERROR"

        message = str(error).lower()
        if "rate limit" in message:
            return ReviewState.DEGRADED, "degraded", "LLM_RATE_LIMITED"
        if "timeout" in message:
            return ReviewState.DEGRADED, "degraded", "LLM_TIMEOUT"
        if "connection" in message:
            return ReviewState.DEGRADED, "degraded", "LLM_CONNECTION_ERROR"
        return ReviewState.FAILED, "failed", "SYSTEM_FAILED"

    def _build_user_failure_summary(self, error: Exception, result_code: str) -> str:
        if result_code == "LLM_TIMEOUT":
            return (
                "[TIMEOUT] Gemini API 응답이 설정된 제한 시간을 초과했습니다. "
                "네트워크를 확인하거나 로컬 모델로 전환하세요."
            )
        if result_code == "LLM_CONNECTION_ERROR":
            return (
                "[CONNECTION] Gemini API 연결에 실패했습니다. "
                "네트워크 또는 API 설정을 확인하세요."
            )
        if result_code == "LLM_RATE_LIMITED":
            return (
                "[RATE LIMIT] Gemini API 호출 한도에 도달했습니다. "
                "잠시 후 다시 시도하거나 로컬 모델로 전환하세요."
            )
        return (
            "[SYSTEM] 리뷰 파이프라인 내부 오류가 발생했습니다. "
            "latest_review.json 또는 review_history.jsonl 내용을 관리자에게 전달해 주세요."
        )

    def _is_terminal_state_value(self, state_value: str | None) -> bool:
        if state_value is None:
            return False
        return state_value in _TERMINAL_STATE_VALUES

    def _collect_findings(self, decisions: list[AgentDecision]) -> list[dict[str, Any]]:
        """에이전트 결정들에서 모든 findings를 수집하고 agent_name을 태깅합니다."""
        all_findings: list[dict[str, Any]] = []
        for dec in decisions:
            for finding in dec.findings:
                enriched: dict[str, Any] = dict(finding)
                enriched.setdefault("agent_name", dec.agent_name)
                all_findings.append(enriched)
        return all_findings

    def _terminal_states(self) -> set[ReviewState]:
        return {
            ReviewState.PRECHECK_FAILED,
            ReviewState.DELIVERED,
            ReviewState.DEGRADED,
            ReviewState.FAILED,
        }

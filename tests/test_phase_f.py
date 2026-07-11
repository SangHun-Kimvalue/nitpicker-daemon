"""Phase F 테스트 — Orchestrator fast_path_only, PatchSvc 파이프라인, DuckDbLogger 연동, KST 타임스탬프."""
from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest

from jemmin.mini_reviewer import _format_review_text
from jemmin.models import (
    AgentDecision,
    ConsensusResult,
    ContextBundle,
    ReviewRequest,
    ReviewState,
)
from jemmin.orchestrator.controller import ReviewOrchestrator
from jemmin.orchestrator.policy_engine import PolicyDecision
from jemmin.services.patch_svc import PatchProposal
from jemmin.services.verification_svc import VerificationReport
from jemmin.utils.duckdb_logger import DuckDbLogger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agent_decision(name: str, status: str = "pass") -> AgentDecision:
    return AgentDecision(
        agent_name=name,
        status=status,  # type: ignore[arg-type]
        confidence_score=1.0 if status == "pass" else 0.5,
        findings=[],
        suggested_actions=[],
    )


def _req(intent: str = "active_intent") -> ReviewRequest:
    return ReviewRequest(
        request_id="f-req",
        idempotency_key="f-idem",
        project_id="f-proj",
        project_profile="general",
        trigger="cli",
        trigger_intent=intent,  # type: ignore[arg-type]
        diff_text="+ some code",
    )


def _heavy_decision() -> PolicyDecision:
    return PolicyDecision(
        accepted=True,
        project_profile="general",
        offload_allowed=True,
        auto_apply_allowed=False,
        redaction_required=False,
        fast_path_only=False,
        allowed_agents=None,
    )


def _make_mock_agent(name: str, status: str = "pass") -> MagicMock:
    a = MagicMock()
    a.name = name
    a.run.return_value = _agent_decision(name, status)
    return a


def _make_orchestrator(
    *,
    agents=None,
    policy_decision: PolicyDecision | None = None,
    consensus_status: str = "pass",
    patch_service=None,
    verification_service=None,
    analytics_logger=None,
) -> ReviewOrchestrator:
    """테스트용 ReviewOrchestrator 조립 헬퍼."""
    if policy_decision is None:
        policy_decision = _heavy_decision()

    job_store = MagicMock()
    job_store.create_request.return_value = None
    job_store.transition_state.return_value = None
    job_store.mark_terminal.return_value = None

    policy_engine = MagicMock()
    policy_engine.evaluate_request.return_value = policy_decision

    resource_manager = MagicMock()
    resource_manager.allow_new_job.return_value = True

    context_service = MagicMock()
    context_service.build_context.return_value = ContextBundle(
        request_id="f-req",
        context_hash="hash",
        token_estimate=10,
        tiers={},
    )

    if agents is None:
        agents = [_make_mock_agent("fast_gate")]

    consensus_engine = MagicMock()
    consensus_engine.decide.return_value = ConsensusResult(
        status=consensus_status,
        summary="consensus summary",
        confidence_score=0.9,
        winning_reasons=[],
        conflicting_agents=[],
    )

    feedback_service = MagicMock()
    feedback_service.publish_diagnostics.return_value = None

    review_logger = MagicMock()
    review_logger.log_result.return_value = None

    return ReviewOrchestrator(
        job_store=job_store,
        policy_engine=policy_engine,
        resource_manager=resource_manager,
        context_service=context_service,
        agents=agents,
        consensus_engine=consensus_engine,
        feedback_service=feedback_service,
        review_logger=review_logger,
        patch_service=patch_service,
        verification_service=verification_service,
        analytics_logger=analytics_logger,
    )


# ===========================================================================
# § 1. KST 타임스탬프 — _format_review_text
# ===========================================================================


class TestKstTimestamp:
    def _sample_payload(self, **kwargs) -> dict:
        base = {
            "target_file": "src/foo.py",
            "result_code": "REVIEW_PASSED",
            "summary": "ok",
            "confidence_score": 1.0,
            "details": [],
            "suggested_patch": None,
        }
        base.update(kwargs)
        return base

    def test_reviewed_at_present(self) -> None:
        text = _format_review_text(self._sample_payload())
        assert "리뷰 시각:" in text

    def test_reviewed_at_kst_format(self) -> None:
        text = _format_review_text(self._sample_payload())
        first_line = text.splitlines()[0]
        assert re.match(
            r"리뷰 시각: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} KST",
            first_line,
        ), f"Unexpected format: {first_line!r}"

    def test_reviewed_at_before_target_file(self) -> None:
        lines = _format_review_text(self._sample_payload()).splitlines()
        keys = [ln.split(":")[0].strip() for ln in lines if ":" in ln]
        assert keys.index("리뷰 시각") < keys.index("대상 파일")

    def test_all_fields_still_present(self) -> None:
        payload = self._sample_payload(
            details=[{"line_number": 10, "issue": "문제"}],
            suggested_patch="--- a\n+++ b",
        )
        text = _format_review_text(payload)
        assert "대상 파일: src/foo.py" in text
        assert "결과 코드: REVIEW_PASSED" in text
        assert "신뢰도: 1.0" in text
        assert "10번째 줄: 문제" in text

    def test_no_details_shows_none(self) -> None:
        text = _format_review_text(self._sample_payload(details=[]))
        assert "- 없음" in text


# ===========================================================================
# § 2. fast_path_only — 에이전트 필터링
# ===========================================================================


class TestFastPathAgentFiltering:
    def _fast_decision(self, allowed: frozenset[str]) -> PolicyDecision:
        return PolicyDecision(
            accepted=True,
            project_profile="general",
            offload_allowed=False,
            auto_apply_allowed=False,
            redaction_required=False,
            fast_path_only=True,
            allowed_agents=allowed,
        )

    def test_passive_save_runs_only_allowed_agents(self) -> None:
        fast_agent = _make_mock_agent("fast_gate")
        heavy_agent = _make_mock_agent("performance")

        orch = _make_orchestrator(
            agents=[fast_agent, heavy_agent],
            policy_decision=self._fast_decision(frozenset({"fast_gate"})),
        )
        orch.run_once(_req("passive_save"))

        fast_agent.run.assert_called_once()
        heavy_agent.run.assert_not_called()

    def test_active_intent_runs_all_agents(self) -> None:
        agents = [_make_mock_agent(n) for n in ["fast_gate", "security", "performance"]]
        orch = _make_orchestrator(agents=agents, policy_decision=_heavy_decision())
        orch.run_once(_req("active_intent"))
        for a in agents:
            a.run.assert_called_once()

    def test_empty_allowed_set_runs_no_agents(self) -> None:
        agent = _make_mock_agent("fast_gate")
        orch = _make_orchestrator(
            agents=[agent],
            policy_decision=self._fast_decision(frozenset()),
        )
        orch.run_once(_req("passive_save"))
        agent.run.assert_not_called()

    def test_multiple_agents_partial_filter(self) -> None:
        a1 = _make_mock_agent("fast_gate")
        a2 = _make_mock_agent("security")
        a3 = _make_mock_agent("performance")
        orch = _make_orchestrator(
            agents=[a1, a2, a3],
            policy_decision=self._fast_decision(frozenset({"fast_gate", "security"})),
        )
        orch.run_once(_req("passive_save"))
        a1.run.assert_called_once()
        a2.run.assert_called_once()
        a3.run.assert_not_called()


# ===========================================================================
# § 3. PatchService + VerificationService 파이프라인
# ===========================================================================


class TestPatchPipelineIntegration:
    def _proposal(self) -> PatchProposal:
        return PatchProposal(
            patch_hash="abc123",
            unified_diff="--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new",
            source_file="src/foo.py",
            saved_path=None,
        )

    def _patch_svc(self, proposal: PatchProposal | None = None) -> MagicMock:
        svc = MagicMock()
        svc.create_patch.return_value = proposal if proposal is not None else self._proposal()
        return svc

    def _verify_svc(self, passed: bool, returncode: int = 0) -> MagicMock:
        svc = MagicMock()
        svc.verify_patch.return_value = VerificationReport(
            passed=passed,
            returncode=returncode,
            stdout="",
            stderr="",
            patch_hash="abc123",
        )
        return svc

    def test_patch_verified_returns_patch_proposed(self) -> None:
        orch = _make_orchestrator(
            consensus_status="patch",
            patch_service=self._patch_svc(),
            verification_service=self._verify_svc(passed=True),
        )
        result = orch.run_once(_req())
        assert result.result_code == "PATCH_PROPOSED"
        assert result.status == "patch"
        assert "PATCH VERIFIED" in result.summary

    def test_patch_verify_failed_returns_verify_failed(self) -> None:
        orch = _make_orchestrator(
            consensus_status="patch",
            patch_service=self._patch_svc(),
            verification_service=self._verify_svc(passed=False, returncode=1),
        )
        result = orch.run_once(_req())
        assert result.result_code == "PATCH_VERIFY_FAILED"
        assert "PATCH REJECTED" in result.summary
        assert "rc=1" in result.summary

    def test_no_patch_service_falls_back_to_patch_required(self) -> None:
        orch = _make_orchestrator(consensus_status="patch", patch_service=None)
        result = orch.run_once(_req())
        assert result.result_code == "PATCH_REQUIRED"

    def test_none_proposal_skips_verification(self) -> None:
        verify_svc = self._verify_svc(passed=True)
        patch_svc = MagicMock()
        patch_svc.create_patch.return_value = None  # 패치 없음

        orch = _make_orchestrator(
            consensus_status="patch",
            patch_service=patch_svc,
            verification_service=verify_svc,
        )
        orch.run_once(_req())
        verify_svc.verify_patch.assert_not_called()

    def test_pass_status_does_not_call_patch_service(self) -> None:
        patch_svc = self._patch_svc()
        orch = _make_orchestrator(
            consensus_status="pass",
            patch_service=patch_svc,
        )
        orch.run_once(_req())
        patch_svc.create_patch.assert_not_called()


# ===========================================================================
# § 4. DuckDbLogger → Orchestrator 연동
# ===========================================================================


class TestOrchestratorAnalyticsLogger:
    def test_success_pipeline_logs_required_events(self) -> None:
        logger = DuckDbLogger(db_path=":memory:")
        orch = _make_orchestrator(analytics_logger=logger)
        orch.run_once(_req())
        rows = logger.query("SELECT event_type FROM review_events ORDER BY id")
        event_types = [r["event_type"] for r in rows]
        assert "policy_decision" in event_types
        assert "agents_run" in event_types
        assert "pipeline_complete" in event_types
        logger.close()

    def test_no_analytics_logger_does_not_raise(self) -> None:
        orch = _make_orchestrator(analytics_logger=None)
        result = orch.run_once(_req())
        assert result.result_code is not None

    def test_broken_analytics_logger_does_not_crash_pipeline(self) -> None:
        broken = MagicMock()
        broken.write.side_effect = RuntimeError("DB full")
        orch = _make_orchestrator(analytics_logger=broken)
        result = orch.run_once(_req())
        assert result is not None

    def test_passive_save_logs_fast_path_status(self) -> None:
        logger = DuckDbLogger(db_path=":memory:")
        decision = PolicyDecision(
            accepted=True,
            project_profile="general",
            offload_allowed=False,
            auto_apply_allowed=False,
            redaction_required=False,
            fast_path_only=True,
            allowed_agents=frozenset({"fast_gate"}),
        )
        orch = _make_orchestrator(
            agents=[_make_mock_agent("fast_gate")],
            policy_decision=decision,
            analytics_logger=logger,
        )
        orch.run_once(_req("passive_save"))
        rows = logger.query(
            "SELECT status FROM review_events WHERE event_type='policy_decision'"
        )
        assert rows[0]["status"] == "fast_path"
        logger.close()

    def test_active_intent_logs_heavy_path_status(self) -> None:
        logger = DuckDbLogger(db_path=":memory:")
        orch = _make_orchestrator(analytics_logger=logger)
        orch.run_once(_req("active_intent"))
        rows = logger.query(
            "SELECT status FROM review_events WHERE event_type='policy_decision'"
        )
        assert rows[0]["status"] == "heavy_path"
        logger.close()

    def test_agents_run_latency_recorded(self) -> None:
        logger = DuckDbLogger(db_path=":memory:")
        orch = _make_orchestrator(analytics_logger=logger)
        orch.run_once(_req())
        rows = logger.query(
            "SELECT latency_ms FROM review_events WHERE event_type='agents_run'"
        )
        assert len(rows) == 1
        assert rows[0]["latency_ms"] >= 0.0
        logger.close()

    def test_findings_count_from_rejected_agent(self) -> None:
        logger = DuckDbLogger(db_path=":memory:")
        orch = _make_orchestrator(
            agents=[_make_mock_agent("security", status="reject")],
            analytics_logger=logger,
        )
        orch.run_once(_req())
        rows = logger.query(
            "SELECT findings_cnt FROM review_events WHERE event_type='agents_run'"
        )
        assert rows[0]["findings_cnt"] == 1
        logger.close()

    def test_pipeline_complete_records_result_code(self) -> None:
        logger = DuckDbLogger(db_path=":memory:")
        orch = _make_orchestrator(analytics_logger=logger)
        orch.run_once(_req())
        rows = logger.query(
            "SELECT result_code FROM review_events WHERE event_type='pipeline_complete'"
        )
        assert rows[0]["result_code"] == "REVIEW_PASSED"
        logger.close()

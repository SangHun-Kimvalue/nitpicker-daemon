"""Phase VI-A: L3 LLM Review Gate 테스트.

§1 LlmReviewGate 단위 테스트                (7 tests)
§2 Orchestrator L3 통합 테스트              (5 tests)
Total: 12 tests
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jemmin.models import (
    ContextBundle,
    ReviewRequest,
    ReviewState,
)
from jemmin.orchestrator.consensus import DefaultConsensusEngine
from jemmin.orchestrator.controller import ReviewOrchestrator
from jemmin.orchestrator.policy_engine import DefaultPolicyEngine
from jemmin.orchestrator.resource_mgr import DefaultResourceManager
from jemmin.providers.base import ProviderRequest
from jemmin.providers.local_llm import MockLocalLLMProvider
from jemmin.services.context_svc import StaticContextService
from jemmin.services.feedback_svc import FileFeedbackService
from jemmin.services.llm_review_gate import LlmReviewGate
from jemmin.services.review_logger import JsonlReviewLogger
from jemmin.state.sqlite_spooler import SQLiteJobStore


def _request(diff: str = "+new line") -> ReviewRequest:
    return ReviewRequest(
        request_id="l3-test",
        idempotency_key="l3-idem",
        project_id="test",
        project_profile="general",
        trigger="cli",
        target_file="src/main.py",
        diff_text=diff,
    )


def _context() -> ContextBundle:
    return ContextBundle(
        request_id="l3-test",
        context_hash="abc",
        token_estimate=100,
        tiers={"tier1": ["diff content"], "tier2": ["symbols"]},
    )


# ---------------------------------------------------------------------------
# §1 LlmReviewGate 단위 테스트
# ---------------------------------------------------------------------------


class TestLlmReviewGate:
    def test_pass_through_review_passed(self):
        """LLM이 REVIEW_PASSED를 반환하면 그대로 전달."""
        provider = MagicMock()
        provider.generate.return_value = {
            "result_code": "REVIEW_PASSED",
            "summary": "깔끔한 코드입니다",
            "confidence_score": 0.95,
            "details": [],
            "suggested_patch": None,
        }
        gate = LlmReviewGate(provider=provider)
        result = gate.review(_request(), _context())
        assert result["result_code"] == "REVIEW_PASSED"
        assert result["confidence_score"] == 0.95
        provider.generate.assert_called_once()

    def test_reject_from_llm(self):
        """LLM이 REVIEW_REJECTED를 반환하면 그대로 전달."""
        provider = MagicMock()
        provider.generate.return_value = {
            "result_code": "REVIEW_REJECTED",
            "summary": "비즈니스 로직 오류 발견",
            "confidence_score": 0.8,
            "details": [{"line_number": 10, "issue": "null 체크 누락"}],
            "suggested_patch": None,
        }
        gate = LlmReviewGate(provider=provider)
        result = gate.review(_request(), _context())
        assert result["result_code"] == "REVIEW_REJECTED"

    def test_timeout_fallback_pass(self):
        """LLM 타임아웃 시 graceful degradation → PASS fallback."""
        provider = MagicMock()
        provider.generate.side_effect = TimeoutError("LLM timeout")
        gate = LlmReviewGate(provider=provider)
        result = gate.review(_request(), _context())
        assert result["result_code"] == "REVIEW_PASSED"
        assert "호출 실패" in result["summary"]

    def test_connection_error_fallback(self):
        """연결 실패 시 PASS fallback."""
        provider = MagicMock()
        provider.generate.side_effect = ConnectionError("server down")
        gate = LlmReviewGate(provider=provider)
        result = gate.review(_request(), _context())
        assert result["result_code"] == "REVIEW_PASSED"

    def test_json_text_parsing(self):
        """provider가 {"text": "JSON문자열"} 형태로 반환할 때 파싱."""
        provider = MagicMock()
        provider.generate.return_value = {
            "text": json.dumps({
                "result_code": "REVIEW_REJECTED",
                "summary": "위반 발견",
                "confidence_score": 0.7,
                "details": [{"line_number": 3, "issue": "구체적 위반"}],
                "suggested_patch": None,
            })
        }
        gate = LlmReviewGate(provider=provider)
        result = gate.review(_request(), _context())
        assert result["result_code"] == "REVIEW_REJECTED"

    def test_summary_only_rejection_is_advisory_pass(self):
        """근거 없는 L3 차단은 잔소리로 낮춘다."""
        provider = MagicMock()
        provider.generate.return_value = {
            "text": json.dumps({
                "result_code": "REVIEW_REJECTED",
                "summary": "취향성 지적",
                "confidence_score": 0.9,
                "details": [],
                "suggested_patch": None,
            })
        }
        gate = LlmReviewGate(provider=provider)
        result = gate.review(_request(), _context())
        assert result["result_code"] == "REVIEW_PASSED"
        assert "잔소리" in result["summary"]

    def test_invalid_json_fallback(self):
        """파싱 불가 응답 시 PASS fallback."""
        provider = MagicMock()
        provider.generate.return_value = {"text": "not valid json at all"}
        gate = LlmReviewGate(provider=provider)
        result = gate.review(_request(), _context())
        assert result["result_code"] == "REVIEW_PASSED"
        assert "파싱 불가" in result["summary"]

    def test_user_prompt_includes_context(self):
        """user prompt에 diff, context tiers, agent findings가 포함되는지 확인."""
        provider = MagicMock()
        provider.generate.return_value = {
            "result_code": "REVIEW_PASSED",
            "summary": "ok",
            "confidence_score": 1.0,
            "details": [],
            "suggested_patch": None,
        }
        gate = LlmReviewGate(provider=provider)
        gate.review(
            _request("+x = 1"),
            _context(),
            agent_findings=[{"code": "ARCH007", "message": "__all__ 없음"}],
        )
        call_args = provider.generate.call_args[0][0]
        assert isinstance(call_args, ProviderRequest)
        assert "x = 1" in call_args.user_prompt
        assert "tier1" in call_args.user_prompt
        assert "ARCH007" in call_args.user_prompt

    def test_large_diff_truncated(self):
        """대용량 diff가 _MAX_DIFF_CHARS 이내로 잘리는지 확인."""
        provider = MagicMock()
        provider.generate.return_value = {
            "result_code": "REVIEW_PASSED",
            "summary": "ok",
            "confidence_score": 1.0,
            "details": [],
            "suggested_patch": None,
        }
        gate = LlmReviewGate(provider=provider)
        big_diff = "+" + "x" * 10000
        gate.review(_request(big_diff), _context())
        call_args = provider.generate.call_args[0][0]
        assert "truncated" in call_args.user_prompt
        assert len(call_args.user_prompt) <= 9000  # _MAX_TOTAL_PROMPT_CHARS + overhead

    def test_large_context_truncated(self):
        """대용량 tier context가 제한되는지 확인."""
        provider = MagicMock()
        provider.generate.return_value = {
            "result_code": "REVIEW_PASSED",
            "summary": "ok",
            "confidence_score": 1.0,
            "details": [],
            "suggested_patch": None,
        }
        gate = LlmReviewGate(provider=provider)
        big_ctx = ContextBundle(
            request_id="l3-test",
            context_hash="abc",
            token_estimate=100,
            tiers={"tier1": ["diff"], "tier2": ["A" * 5000], "tier3": ["B" * 5000]},
        )
        gate.review(_request(), big_ctx)
        call_args = provider.generate.call_args[0][0]
        assert "truncated" in call_args.user_prompt


# ---------------------------------------------------------------------------
# §2 Orchestrator L3 통합 테스트
# ---------------------------------------------------------------------------


class TestOrchestratorL3Integration:
    def _build_orchestrator(self, tmp: Path, llm_result: dict | None = None):
        """L3 게이트 포함 오케스트레이터 생성."""
        from jemmin.agents.fast_gate import FastGateAgent

        mock_provider = MockLocalLLMProvider()

        gate = None
        if llm_result is not None:
            gate_provider = MagicMock()
            gate_provider.generate.return_value = llm_result
            gate = LlmReviewGate(provider=gate_provider)

        return ReviewOrchestrator(
            job_store=SQLiteJobStore(tmp / "spool.db"),
            policy_engine=DefaultPolicyEngine(max_file_size_bytes=1024),
            resource_manager=DefaultResourceManager(token_budget=1000),
            context_service=StaticContextService(),
            agents=[FastGateAgent(provider=mock_provider)],
            consensus_engine=DefaultConsensusEngine(),
            feedback_service=FileFeedbackService(tmp / "LATEST_REVIEW.txt"),
            review_logger=JsonlReviewLogger(tmp / "review_history.jsonl"),
            llm_review_gate=gate,
        )

    def test_l3_pass_delivers_llm_summary(self):
        """L2 PASS + L3 PASS → LLM의 summary가 최종 결과에 반영."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "demo.txt"
            target.write_text("demo", encoding="utf-8")

            orch = self._build_orchestrator(root, llm_result={
                "result_code": "REVIEW_PASSED",
                "summary": "LLM이 승인한 깔끔한 코드",
                "confidence_score": 0.99,
                "details": [],
                "suggested_patch": None,
            })
            request = ReviewRequest(
                request_id="l3-int-1",
                idempotency_key="l3-int-idem-1",
                project_id="test",
                project_profile="general",
                trigger="cli",
                target_file=str(target),
                diff_text="small change",
            )
            result = orch.run_once(request)
            assert result.state == ReviewState.DELIVERED
            assert result.status == "pass"
            assert result.result_code == "REVIEW_PASSED"
            assert "LLM이 승인한" in result.summary

    def test_l3_reject_overrides_l2_pass(self):
        """L2 PASS + L3 REJECT → 최종 결과는 REJECTED."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "demo.txt"
            target.write_text("demo", encoding="utf-8")

            orch = self._build_orchestrator(root, llm_result={
                "result_code": "REVIEW_REJECTED",
                "summary": "비즈니스 로직 결함 발견",
                "confidence_score": 0.85,
                "details": [{"line_number": 5, "issue": "null 처리 누락"}],
                "suggested_patch": None,
            })
            request = ReviewRequest(
                request_id="l3-int-2",
                idempotency_key="l3-int-idem-2",
                project_id="test",
                project_profile="general",
                trigger="cli",
                target_file=str(target),
                diff_text="small change",
            )
            result = orch.run_once(request)
            assert result.state == ReviewState.DELIVERED
            assert result.status == "rejected"
            assert result.result_code == "REVIEW_REJECTED"
            assert "비즈니스 로직" in result.summary

    def test_no_l3_gate_passes_through(self):
        """L3 게이트 없으면 L2 결과 그대로 전달 (하위 호환)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "demo.txt"
            target.write_text("demo", encoding="utf-8")

            orch = self._build_orchestrator(root, llm_result=None)
            request = ReviewRequest(
                request_id="l3-int-3",
                idempotency_key="l3-int-idem-3",
                project_id="test",
                project_profile="general",
                trigger="cli",
                target_file=str(target),
                diff_text="small change",
            )
            result = orch.run_once(request)
            assert result.state == ReviewState.DELIVERED
            assert result.status == "pass"
            assert result.result_code == "REVIEW_PASSED"

    def test_l3_timeout_graceful_degradation(self):
        """L3 LLM 타임아웃 → PASS fallback (L2 결과 유지)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "demo.txt"
            target.write_text("demo", encoding="utf-8")

            gate_provider = MagicMock()
            gate_provider.generate.side_effect = TimeoutError("timeout")
            gate = LlmReviewGate(provider=gate_provider)

            orch = ReviewOrchestrator(
                job_store=SQLiteJobStore(root / "spool.db"),
                policy_engine=DefaultPolicyEngine(max_file_size_bytes=1024),
                resource_manager=DefaultResourceManager(token_budget=1000),
                context_service=StaticContextService(),
                agents=[__import__("jemmin.agents.fast_gate", fromlist=["FastGateAgent"]).FastGateAgent(
                    provider=MockLocalLLMProvider()
                )],
                consensus_engine=DefaultConsensusEngine(),
                feedback_service=FileFeedbackService(root / "LATEST_REVIEW.txt"),
                review_logger=JsonlReviewLogger(root / "review_history.jsonl"),
                llm_review_gate=gate,
            )
            request = ReviewRequest(
                request_id="l3-int-4",
                idempotency_key="l3-int-idem-4",
                project_id="test",
                project_profile="general",
                trigger="cli",
                target_file=str(target),
                diff_text="small change",
            )
            result = orch.run_once(request)
            assert result.state == ReviewState.DELIVERED
            assert result.status == "pass"
            assert result.result_code == "REVIEW_PASSED"

    def test_l3_confidence_override(self):
        """L3의 confidence_score가 최종 결과에 반영."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "demo.txt"
            target.write_text("demo", encoding="utf-8")

            orch = self._build_orchestrator(root, llm_result={
                "result_code": "REVIEW_PASSED",
                "summary": "ok",
                "confidence_score": 0.42,
                "details": [],
                "suggested_patch": None,
            })
            request = ReviewRequest(
                request_id="l3-int-5",
                idempotency_key="l3-int-idem-5",
                project_id="test",
                project_profile="general",
                trigger="cli",
                target_file=str(target),
                diff_text="small change",
            )
            result = orch.run_once(request)
            assert result.confidence_score == 0.42

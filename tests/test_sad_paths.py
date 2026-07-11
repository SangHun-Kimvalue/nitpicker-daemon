from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
import unittest

from jemmin.models import ContextBundle, ReviewRequest, ReviewResult, ReviewState
from jemmin.orchestrator.consensus import DefaultConsensusEngine
from jemmin.orchestrator.controller import ReviewOrchestrator
from jemmin.orchestrator.policy_engine import PolicyDecision
from jemmin.orchestrator.resource_mgr import DefaultResourceManager
from jemmin.services.context_svc import StaticContextService
from jemmin.state.sqlite_spooler import JobCreationOutcome, SQLiteJobStore


def build_request(target: Path, diff_text: str = "small change") -> ReviewRequest:
    return ReviewRequest(
        request_id="req_1",
        idempotency_key="idem_1",
        project_id="test",
        project_profile="general",
        trigger="cli",
        target_file=str(target),
        git_revision="workspace",
        base_file_hash="abc",
        diff_text=diff_text,
    )


class RejectingPolicyEngine:
    def evaluate_request(self, request: ReviewRequest) -> PolicyDecision:
        return PolicyDecision(
            accepted=False,
            project_profile=request.project_profile,
            offload_allowed=False,
            auto_apply_allowed=False,
            redaction_required=False,
            reason="policy blocked request",
        )


class TimeoutAgent:
    name = "timeout_agent"

    def run(self, request: ReviewRequest, context: ContextBundle):
        raise TimeoutError("LLM request timeout")


class FailingJobStore:
    def create_request(self, request: ReviewRequest) -> JobCreationOutcome:
        raise RuntimeError("spool write failed")

    def transition_state(self, request_id, from_state, to_state, reason=None, result_code=None) -> None:
        raise AssertionError("transition_state should not be called after create_request failure")

    def mark_terminal(self, request_id, final_state, reason=None, result_code=None) -> None:
        raise KeyError(request_id)


@dataclass
class RecordingFeedbackService:
    last_result: ReviewResult | None = None

    def publish_diagnostics(self, result: ReviewResult, *, target_file: str = "") -> None:
        self.last_result = result

    def publish_quick_fix(self, result: ReviewResult, patch) -> None:
        return None

    def clear_feedback(self, target_file: str) -> None:
        return None


@dataclass
class RecordingReviewLogger:
    results: list[ReviewResult]

    def __init__(self) -> None:
        self.results = []

    def log_event(self, request_id: str, event_type: str, payload: dict) -> None:
        return None

    def log_result(self, result: ReviewResult) -> None:
        self.results.append(result)


class SadPathTests(unittest.TestCase):
    def test_policy_rejection_sets_policy_result_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "demo.txt"
            target.write_text("demo", encoding="utf-8")
            feedback = RecordingFeedbackService()
            logger = RecordingReviewLogger()

            orchestrator = ReviewOrchestrator(
                job_store=SQLiteJobStore(root / "spool.db"),
                policy_engine=RejectingPolicyEngine(),
                resource_manager=DefaultResourceManager(token_budget=1000),
                context_service=StaticContextService(),
                agents=[],
                consensus_engine=DefaultConsensusEngine(),
                feedback_service=feedback,
                review_logger=logger,
            )

            result = orchestrator.run_once(build_request(target))

            self.assertEqual(result.state, ReviewState.PRECHECK_FAILED)
            self.assertEqual(result.status, "rejected")
            self.assertEqual(result.result_code, "POLICY_REJECTED")
            self.assertIsNotNone(feedback.last_result)
            self.assertEqual(feedback.last_result.result_code, "POLICY_REJECTED")
            self.assertTrue(logger.results)
            self.assertEqual(logger.results[-1].result_code, "POLICY_REJECTED")

    def test_timeout_degrades_with_llm_timeout_result_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "demo.txt"
            target.write_text("demo", encoding="utf-8")
            feedback = RecordingFeedbackService()
            logger = RecordingReviewLogger()

            orchestrator = ReviewOrchestrator(
                job_store=SQLiteJobStore(root / "spool.db"),
                policy_engine=RejectingPolicyEngineAllowAll(),
                resource_manager=DefaultResourceManager(token_budget=1000),
                context_service=StaticContextService(),
                agents=[TimeoutAgent()],
                consensus_engine=DefaultConsensusEngine(),
                feedback_service=feedback,
                review_logger=logger,
            )

            result = orchestrator.run_once(build_request(target))

            self.assertEqual(result.state, ReviewState.DEGRADED)
            self.assertEqual(result.status, "degraded")
            self.assertEqual(result.result_code, "LLM_TIMEOUT")
            self.assertIn("[TIMEOUT]", result.summary)
            self.assertIn("로컬 모델로 전환", result.summary)
            self.assertIsNotNone(feedback.last_result)
            self.assertEqual(feedback.last_result.result_code, "LLM_TIMEOUT")
            self.assertTrue(logger.results)
            self.assertEqual(logger.results[-1].result_code, "LLM_TIMEOUT")

    def test_create_request_failure_returns_system_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "demo.txt"
            target.write_text("demo", encoding="utf-8")
            feedback = RecordingFeedbackService()
            logger = RecordingReviewLogger()

            orchestrator = ReviewOrchestrator(
                job_store=FailingJobStore(),
                policy_engine=RejectingPolicyEngineAllowAll(),
                resource_manager=DefaultResourceManager(token_budget=1000),
                context_service=StaticContextService(),
                agents=[],
                consensus_engine=DefaultConsensusEngine(),
                feedback_service=feedback,
                review_logger=logger,
            )

            result = orchestrator.run_once(build_request(target))

            self.assertEqual(result.state, ReviewState.FAILED)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.result_code, "SYSTEM_FAILED")
            self.assertIn("[SYSTEM]", result.summary)
            self.assertIn("관리자에게 전달", result.summary)
            self.assertIsNotNone(feedback.last_result)
            self.assertEqual(feedback.last_result.result_code, "SYSTEM_FAILED")
            self.assertTrue(logger.results)
            self.assertEqual(logger.results[-1].result_code, "SYSTEM_FAILED")


class RejectingPolicyEngineAllowAll:
    def evaluate_request(self, request: ReviewRequest) -> PolicyDecision:
        return PolicyDecision(
            accepted=True,
            project_profile=request.project_profile,
            offload_allowed=False,
            auto_apply_allowed=False,
            redaction_required=False,
        )


if __name__ == "__main__":
    unittest.main()
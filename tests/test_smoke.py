from pathlib import Path
import json
import tempfile
import unittest

from jemmin.agents.fast_gate import FastGateAgent
from jemmin.models import ReviewRequest, ReviewState
from jemmin.orchestrator.consensus import DefaultConsensusEngine
from jemmin.orchestrator.controller import ReviewOrchestrator
from jemmin.orchestrator.policy_engine import DefaultPolicyEngine
from jemmin.orchestrator.resource_mgr import DefaultResourceManager
from jemmin.providers.local_llm import MockLocalLLMProvider
from jemmin.services.context_svc import StaticContextService
from jemmin.services.feedback_svc import FileFeedbackService
from jemmin.services.review_logger import JsonlReviewLogger
from jemmin.state.sqlite_spooler import SQLiteJobStore


class SmokeTest(unittest.TestCase):
    def test_orchestrator_delivers_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "demo.txt"
            target.write_text("demo", encoding="utf-8")

            orchestrator = ReviewOrchestrator(
                job_store=SQLiteJobStore(root / "spool.db"),
                policy_engine=DefaultPolicyEngine(max_file_size_bytes=1024),
                resource_manager=DefaultResourceManager(token_budget=1000),
                context_service=StaticContextService(),
                agents=[FastGateAgent(provider=MockLocalLLMProvider())],
                consensus_engine=DefaultConsensusEngine(),
                feedback_service=FileFeedbackService(root / "LATEST_REVIEW.txt"),
                review_logger=JsonlReviewLogger(root / "review_history.jsonl"),
            )

            request = ReviewRequest(
                request_id="req_1",
                idempotency_key="idem_1",
                project_id="test",
                project_profile="general",
                trigger="cli",
                target_file=str(target),
                git_revision="workspace",
                base_file_hash="abc",
                diff_text="small change",
            )

            result = orchestrator.run_once(request)

            self.assertEqual(result.state, ReviewState.DELIVERED)
            self.assertEqual(result.status, "pass")
            self.assertEqual(result.result_code, "REVIEW_PASSED")

            feedback_text = (root / "LATEST_REVIEW.txt").read_text(encoding="utf-8")
            self.assertIn("REVIEW_PASSED", feedback_text)

            review_log = (root / "review_history.jsonl").read_text(encoding="utf-8").strip().splitlines()
            self.assertTrue(review_log)
            last_record = json.loads(review_log[-1])
            self.assertEqual(last_record["result_code"], "REVIEW_PASSED")


if __name__ == "__main__":
    unittest.main()
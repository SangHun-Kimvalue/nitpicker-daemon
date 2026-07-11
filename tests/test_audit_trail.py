from __future__ import annotations

import json
from pathlib import Path
import sqlite3
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


def build_request(target: Path) -> ReviewRequest:
    return ReviewRequest(
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


class AuditTrailTests(unittest.TestCase):
    def test_happy_path_event_sequence_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "demo.txt"
            target.write_text("demo", encoding="utf-8")
            db_path = root / "spool.db"

            orchestrator = ReviewOrchestrator(
                job_store=SQLiteJobStore(db_path),
                policy_engine=DefaultPolicyEngine(max_file_size_bytes=1024),
                resource_manager=DefaultResourceManager(token_budget=1000),
                context_service=StaticContextService(),
                agents=[FastGateAgent(provider=MockLocalLLMProvider())],
                consensus_engine=DefaultConsensusEngine(),
                feedback_service=FileFeedbackService(root / "LATEST_REVIEW.txt"),
                review_logger=JsonlReviewLogger(root / "review_history.jsonl"),
            )

            result = orchestrator.run_once(build_request(target))
            self.assertEqual(result.state, ReviewState.DELIVERED)

            connection = sqlite3.connect(db_path)
            try:
                rows = connection.execute(
                    "SELECT event_type, payload_json FROM review_events WHERE request_id = ? ORDER BY id ASC",
                    ("req_1",),
                ).fetchall()
            finally:
                connection.close()

            event_types = [row[0] for row in rows]
            self.assertEqual(
                event_types,
                [
                    "review.request",
                    "review.state_changed",
                    "review.state_changed",
                    "review.state_changed",
                    "review.terminal",
                ],
            )

            state_sequence = []
            result_code_sequence = []
            for event_type, payload_json in rows:
                payload = json.loads(payload_json)
                if event_type == "review.request":
                    state_sequence.append(payload["state"])
                elif event_type == "review.state_changed":
                    state_sequence.append(payload["to"])
                    result_code_sequence.append(payload["result_code"])
                elif event_type == "review.terminal":
                    state_sequence.append(payload["state"])
                    result_code_sequence.append(payload["result_code"])

            self.assertEqual(
                state_sequence,
                [
                    ReviewState.QUEUED.value,
                    ReviewState.CONTEXT_READY.value,
                    ReviewState.ANALYZING.value,
                    ReviewState.CONSENSUS_REACHED.value,
                    ReviewState.DELIVERED.value,
                ],
            )
            self.assertEqual(
                result_code_sequence,
                [
                    "CONTEXT_BUILD_STARTED",
                    "AGENTS_STARTED",
                    "CONSENSUS_REACHED",
                    "REVIEW_PASSED",
                ],
            )


if __name__ == "__main__":
    unittest.main()
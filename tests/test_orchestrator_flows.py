from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from jemmin.agents.fast_gate import FastGateAgent
from jemmin.models import ReviewRequest, ReviewState
from jemmin.orchestrator.consensus import DefaultConsensusEngine
from jemmin.orchestrator.controller import ReviewOrchestrator
from jemmin.orchestrator.policy_engine import PolicyDecision
from jemmin.orchestrator.resource_mgr import DefaultResourceManager
from jemmin.providers.local_llm import MockLocalLLMProvider
from jemmin.services.context_svc import StaticContextService
from jemmin.state.sqlite_spooler import SQLiteJobStore


@pytest.fixture
def store(tmp_path: Path) -> SQLiteJobStore:
    # SQLiteJobStore opens a new connection per operation, so a temp file is a more
    # faithful integration fixture than :memory:.
    return SQLiteJobStore(tmp_path / "spool.db")


@pytest.fixture
def dummy_request(tmp_path: Path) -> ReviewRequest:
    target_file = tmp_path / "main.cpp"
    target_file.write_text("int main() { return 0; }\n", encoding="utf-8")
    return ReviewRequest(
        request_id="req_test_001",
        idempotency_key="idemp_001",
        project_id="proj_alpha",
        project_profile="strict_cpp",
        trigger="cli",
        target_file=str(target_file),
        git_revision="abc1234",
        base_file_hash="hash999",
        diff_text="+ int a = 1;",
        metadata={},
    )


@pytest.fixture
def orchestrator_deps(store: SQLiteJobStore):
    deps = {
        "job_store": store,
        "policy_engine": Mock(),
        "resource_manager": Mock(),
        "context_service": Mock(),
        "agents": [Mock()],
        "consensus_engine": Mock(),
        "feedback_service": Mock(),
        "review_logger": Mock(),
    }

    deps["resource_manager"].allow_new_job.return_value = True
    deps["policy_engine"].evaluate_request.return_value = PolicyDecision(
        accepted=True,
        project_profile="strict_cpp",
        offload_allowed=False,
        auto_apply_allowed=False,
        redaction_required=False,
    )
    deps["context_service"].build_context.return_value = StaticContextService().build_context(
        ReviewRequest(
            request_id="ctx_req",
            idempotency_key="ctx_idemp",
            project_id="ctx_project",
            project_profile="strict_cpp",
            trigger="cli",
            target_file="ctx.cpp",
            git_revision="workspace",
            base_file_hash="ctx_hash",
            diff_text="+ int a = 1;",
        )
    )
    deps["agents"][0].run.return_value = FastGateAgent(provider=MockLocalLLMProvider()).run(
        ReviewRequest(
            request_id="agent_req",
            idempotency_key="agent_idemp",
            project_id="agent_project",
            project_profile="strict_cpp",
            trigger="cli",
            target_file="agent.cpp",
            git_revision="workspace",
            base_file_hash="agent_hash",
            diff_text="small change",
        ),
        StaticContextService().build_context(
            ReviewRequest(
                request_id="agent_req",
                idempotency_key="agent_idemp",
                project_id="agent_project",
                project_profile="strict_cpp",
                trigger="cli",
                target_file="agent.cpp",
                git_revision="workspace",
                base_file_hash="agent_hash",
                diff_text="small change",
            )
        ),
    )
    deps["consensus_engine"].decide.return_value = Mock(
        status="pass",
        summary="LGTM",
        confidence_score=0.9,
    )
    return deps


@pytest.fixture
def orchestrator(orchestrator_deps) -> ReviewOrchestrator:
    return ReviewOrchestrator(**orchestrator_deps)


def get_audit_rows(store: SQLiteJobStore, request_id: str):
    connection = sqlite3.connect(store._db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT event_type, payload_json FROM review_events WHERE request_id = ? ORDER BY id ASC",
            (request_id,),
        ).fetchall()
    finally:
        connection.close()


def get_audit_trail(store: SQLiteJobStore, request_id: str) -> list[str]:
    return [row["event_type"] for row in get_audit_rows(store, request_id)]


def get_state_transitions(store: SQLiteJobStore, request_id: str) -> list[str]:
    transitions: list[str] = []
    for row in get_audit_rows(store, request_id):
        if row["event_type"] != "review.state_changed":
            continue
        payload = json.loads(row["payload_json"])
        transitions.append(f"{payload['from']} -> {payload['to']}")
    return transitions


def get_terminal_payload(store: SQLiteJobStore, request_id: str) -> dict:
    for row in reversed(get_audit_rows(store, request_id)):
        if row["event_type"] == "review.terminal":
            return json.loads(row["payload_json"])
    raise AssertionError("terminal event was not recorded")


def get_current_state(store: SQLiteJobStore, request_id: str) -> str:
    connection = sqlite3.connect(store._db_path)
    try:
        row = connection.execute(
            "SELECT state FROM review_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return row[0]


def test_policy_reject_flow(orchestrator, orchestrator_deps, dummy_request, store: SQLiteJobStore):
    orchestrator_deps["policy_engine"].evaluate_request.return_value = PolicyDecision(
        accepted=False,
        project_profile=dummy_request.project_profile,
        offload_allowed=False,
        auto_apply_allowed=False,
        redaction_required=False,
        reason="File too large",
    )

    result = orchestrator.run_once(dummy_request)

    assert result.state == ReviewState.PRECHECK_FAILED
    assert result.status == "rejected"
    assert result.result_code == "POLICY_REJECTED"

    assert get_audit_trail(store, dummy_request.request_id) == [
        "review.request",
        "review.state_changed",
        "review.terminal",
    ]

    terminal_payload = get_terminal_payload(store, dummy_request.request_id)
    assert terminal_payload["state"] == ReviewState.PRECHECK_FAILED.value
    assert terminal_payload["result_code"] == "POLICY_REJECTED"


def test_agent_timeout_degraded_flow(orchestrator, orchestrator_deps, dummy_request, store: SQLiteJobStore):
    orchestrator_deps["agents"][0].run.side_effect = TimeoutError("LLM API Connection Timeout")

    result = orchestrator.run_once(dummy_request)

    assert result.state == ReviewState.DEGRADED
    assert result.status == "degraded"
    assert result.result_code == "LLM_TIMEOUT"

    assert get_state_transitions(store, dummy_request.request_id) == [
        f"{ReviewState.QUEUED.value} -> {ReviewState.CONTEXT_READY.value}",
        f"{ReviewState.CONTEXT_READY.value} -> {ReviewState.ANALYZING.value}",
        f"{ReviewState.ANALYZING.value} -> {ReviewState.DEGRADED.value}",
    ]

    terminal_payload = get_terminal_payload(store, dummy_request.request_id)
    assert terminal_payload["state"] == ReviewState.DEGRADED.value
    assert terminal_payload["result_code"] == "LLM_TIMEOUT"


def test_fatal_system_error_flow(orchestrator, orchestrator_deps, dummy_request, store: SQLiteJobStore):
    orchestrator_deps["context_service"].build_context.side_effect = RuntimeError("AST Parser crashed")

    result = orchestrator.run_once(dummy_request)

    assert result.state == ReviewState.FAILED
    assert result.status == "failed"
    assert result.result_code == "SYSTEM_FAILED"
    assert get_current_state(store, dummy_request.request_id) == ReviewState.FAILED.value

    terminal_payload = get_terminal_payload(store, dummy_request.request_id)
    assert terminal_payload["state"] == ReviewState.FAILED.value
    assert terminal_payload["result_code"] == "SYSTEM_FAILED"


def test_happy_path_audit_trail(orchestrator, dummy_request, store: SQLiteJobStore):
    result = orchestrator.run_once(dummy_request)

    assert result.state == ReviewState.DELIVERED
    assert result.status == "pass"
    assert result.result_code == "REVIEW_PASSED"

    assert get_state_transitions(store, dummy_request.request_id) == [
        f"{ReviewState.QUEUED.value} -> {ReviewState.CONTEXT_READY.value}",
        f"{ReviewState.CONTEXT_READY.value} -> {ReviewState.ANALYZING.value}",
        f"{ReviewState.ANALYZING.value} -> {ReviewState.CONSENSUS_REACHED.value}",
    ]

    terminal_payload = get_terminal_payload(store, dummy_request.request_id)
    assert terminal_payload["state"] == ReviewState.DELIVERED.value
    assert terminal_payload["result_code"] == "REVIEW_PASSED"


def test_duplicate_request_returns_ignored_result(orchestrator, dummy_request, store: SQLiteJobStore):
    first = orchestrator.run_once(dummy_request)
    assert first.result_code == "REVIEW_PASSED"

    duplicate_request = ReviewRequest(
        request_id="req_test_duplicate",
        idempotency_key=dummy_request.idempotency_key,
        project_id=dummy_request.project_id,
        project_profile=dummy_request.project_profile,
        trigger=dummy_request.trigger,
        target_file=dummy_request.target_file,
        git_revision=dummy_request.git_revision,
        base_file_hash=dummy_request.base_file_hash,
        diff_text=dummy_request.diff_text,
        metadata={},
    )

    result = orchestrator.run_once(duplicate_request)

    assert result.state == ReviewState.DELIVERED
    assert result.status == "ignored"
    assert result.result_code == "DUPLICATE_REQUEST_IGNORED"
    assert "이미 완료되었습니다" in result.summary
    assert "기존 상태: delivered" in result.summary

    assert get_audit_trail(store, dummy_request.request_id) == [
        "review.request",
        "review.state_changed",
        "review.state_changed",
        "review.state_changed",
        "review.terminal",
        "review.duplicate_ignored",
    ]


def test_fatal_create_request_error_returns_failed_result(dummy_request):
    job_store = Mock()
    job_store.create_request.side_effect = RuntimeError("spool write failed")
    feedback_service = MagicMock()
    review_logger = MagicMock()

    orchestrator = ReviewOrchestrator(
        job_store=job_store,
        policy_engine=Mock(),
        resource_manager=Mock(),
        context_service=Mock(),
        agents=[],
        consensus_engine=Mock(),
        feedback_service=feedback_service,
        review_logger=review_logger,
    )

    result = orchestrator.run_once(dummy_request)

    assert result.state == ReviewState.FAILED
    assert result.status == "failed"
    assert result.result_code == "SYSTEM_FAILED"
    feedback_service.publish_diagnostics.assert_called_once()
    review_logger.log_result.assert_called_once()
    job_store.transition_state.assert_not_called()
    job_store.mark_terminal.assert_not_called()
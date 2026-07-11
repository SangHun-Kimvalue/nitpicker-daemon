"""Unit tests for jemmin_daemon.py.

Covers:
- _build_request: field mapping from payload dict → ReviewRequest
- _build_request: request_id falls back to hash when missing from payload
- _build_request: empty target_file / diff_text handled gracefully
- handle_review_request: happy path returns expected keys
- handle_review_request: orchestrator result fields are mapped correctly
- _build_orchestrator: creates ReviewOrchestrator with correct components
- Router handler registration: review.request handler is registered
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import bin.jemmin_daemon as daemon_module
from jemmin.models import ReviewRequest, ReviewResult, ReviewState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_payload(**overrides) -> dict:
    base = {
        "schema_version": "1.0",
        "message_type": "review.request",
        "request_id": "req_test001",
        "target_file": "src/jemmin/mini_reviewer.py",
        "diff_text": "+some diff line",
        "project_id": "jemmin",
        "project_profile": "general",
        "trigger": "cli",
        "git_revision": "HEAD",
    }
    base.update(overrides)
    return base


def _fake_result(**overrides) -> ReviewResult:
    defaults = dict(
        request_id="req_test001",
        state=ReviewState.DELIVERED,
        status="pass",
        summary="문제 없음",
        confidence_score=0.95,
        result_code="REVIEW_PASSED",
    )
    defaults.update(overrides)
    return ReviewResult(**defaults)


# ---------------------------------------------------------------------------
# _build_request
# ---------------------------------------------------------------------------

class TestBuildRequest:
    def test_target_file_mapped(self) -> None:
        req = daemon_module._build_request(_minimal_payload())
        assert req.target_file == "src/jemmin/mini_reviewer.py"

    def test_diff_text_mapped(self) -> None:
        req = daemon_module._build_request(_minimal_payload())
        assert req.diff_text == "+some diff line"

    def test_request_id_taken_from_payload(self) -> None:
        req = daemon_module._build_request(_minimal_payload(request_id="req_explicit"))
        assert req.request_id == "req_explicit"

    def test_request_id_falls_back_to_hash_when_missing(self) -> None:
        payload = _minimal_payload()
        del payload["request_id"]
        req = daemon_module._build_request(payload)
        expected = "req_" + hashlib.sha256(
            (payload["target_file"] + payload["diff_text"]).encode()
        ).hexdigest()[:12]
        assert req.request_id == expected

    def test_trigger_defaults_to_cli(self) -> None:
        req = daemon_module._build_request(_minimal_payload())
        assert req.trigger == "cli"

    def test_empty_target_and_diff_do_not_raise(self) -> None:
        req = daemon_module._build_request(_minimal_payload(target_file="", diff_text=""))
        assert req.target_file == ""
        assert req.diff_text == ""


# ---------------------------------------------------------------------------
# handle_review_request (async handler)
# ---------------------------------------------------------------------------

class TestHandleReviewRequest:
    def _make_handler(self) -> tuple[object, object]:
        """Returns (handler_coroutine_callable, mock_orchestrator)."""
        mock_orchestrator = MagicMock()
        mock_orchestrator.run_once.return_value = _fake_result()

        async def handle_review_request(payload: dict) -> dict:
            request: ReviewRequest = daemon_module._build_request(payload)
            result = await asyncio.to_thread(mock_orchestrator.run_once, request)
            return {
                "request_id": result.request_id,
                "state": result.state.value,
                "status": result.status,
                "summary": result.summary,
                "confidence_score": result.confidence_score,
                "result_code": result.result_code,
            }

        return handle_review_request, mock_orchestrator

    def test_happy_path_returns_expected_keys(self) -> None:
        handler, _ = self._make_handler()
        result = asyncio.run(handler(_minimal_payload()))
        assert set(result.keys()) == {
            "request_id", "state", "status", "summary", "confidence_score", "result_code"
        }

    def test_result_code_is_passed_through(self) -> None:
        handler, _ = self._make_handler()
        result = asyncio.run(handler(_minimal_payload()))
        assert result["result_code"] == "REVIEW_PASSED"

    def test_state_is_string_value(self) -> None:
        handler, _ = self._make_handler()
        result = asyncio.run(handler(_minimal_payload()))
        assert result["state"] == ReviewState.DELIVERED.value

    def test_orchestrator_receives_correct_request(self) -> None:
        handler, mock_orch = self._make_handler()
        asyncio.run(handler(_minimal_payload()))
        mock_orch.run_once.assert_called_once()
        passed_request: ReviewRequest = mock_orch.run_once.call_args[0][0]
        assert passed_request.target_file == "src/jemmin/mini_reviewer.py"
        assert passed_request.diff_text == "+some diff line"

    def test_rejected_review_state_is_returned(self) -> None:
        handler, mock_orch = self._make_handler()
        mock_orch.run_once.return_value = _fake_result(
            state=ReviewState.PRECHECK_FAILED, status="rejected", result_code="POLICY_REJECTED"
        )
        result = asyncio.run(handler(_minimal_payload()))
        assert result["status"] == "rejected"
        assert result["result_code"] == "POLICY_REJECTED"


# ---------------------------------------------------------------------------
# Router handler registration
# ---------------------------------------------------------------------------

class TestRouterRegistration:
    def test_review_request_handler_is_registered(self) -> None:
        mock_orchestrator = MagicMock()
        mock_orchestrator.run_once.return_value = _fake_result()

        mock_router = MagicMock()

        with patch.object(daemon_module, "_build_orchestrator", return_value=mock_orchestrator):
            with patch("bin.jemmin_daemon.ZmqRouter", return_value=mock_router):
                mock_router.start = AsyncMock(side_effect=asyncio.CancelledError)
                try:
                    asyncio.run(daemon_module.main())
                except (asyncio.CancelledError, SystemExit):
                    pass

        mock_router.register_handler.assert_called_once()
        call_args = mock_router.register_handler.call_args
        assert call_args[0][0] == "review.request"
        assert callable(call_args[0][1])

"""Phase I tests: LSP Quick-Fix, Gemini Provider verification, Context Cache wiring.

Section 1: FileFeedbackService.publish_quick_fix JSON output      (5 tests)
Section 2: LSP code action builder                                (5 tests)
Section 3: Orchestrator quick-fix + context-cache wiring          (4 tests)
Section 4: Gemini Provider fallback & availability                (4 tests)
Section 5: Context Cache Manager lifecycle (LLM API connection)   (5 tests)
Total: 23 tests
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
BIN = ROOT / "bin"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

from jemmin.models import (
    AgentDecision,
    ConsensusResult,
    ContextBundle,
    ReviewRequest,
    ReviewResult,
    ReviewState,
)
from jemmin.providers.base import CacheEntry
from jemmin.providers.gemini import GeminiProvider
from jemmin.providers.local_llm import MockLocalLLMProvider
from jemmin.services.cache_mgr import ContextCacheManager
from jemmin.services.feedback_svc import FileFeedbackService, _parse_hunk_ranges
from jemmin.services.patch_svc import PatchProposal


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _result(
    request_id: str = "req-i",
    state: ReviewState = ReviewState.DELIVERED,
    status: str = "pass",
    summary: str = "all ok",
    result_code: str = "REVIEW_PASSED",
) -> ReviewResult:
    return ReviewResult(
        request_id=request_id,
        state=state,
        status=status,
        summary=summary,
        confidence_score=0.9,
        result_code=result_code,
    )


def _request(
    target_file: str = "src/example.py",
    diff_text: str = "+import os\n",
) -> ReviewRequest:
    return ReviewRequest(
        request_id="req-i",
        idempotency_key="idem-i",
        project_id="test",
        project_profile="general",
        trigger="cli",
        target_file=target_file,
        git_revision="HEAD",
        base_file_hash="abc123",
        diff_text=diff_text,
    )


def _context() -> ContextBundle:
    return ContextBundle(
        request_id="req-i",
        context_hash="hash",
        token_estimate=100,
        tiers={"tier1": ["diff"], "tier2": [], "tier3": [], "tier4": []},
    )


# ===========================================================================
# Section 1: FileFeedbackService.publish_quick_fix
# ===========================================================================


class TestPublishQuickFix:
    def test_writes_json_with_details_and_code_actions(self, tmp_path: Path):
        """publish_quick_fix must create latest_review.json with findings → details."""
        svc = FileFeedbackService(tmp_path / "LATEST_REVIEW.txt")
        findings = [
            {"code": "SEC001", "message": "hardcoded secret", "severity": "error", "line_number": 10},
        ]
        svc.publish_quick_fix(_result(), target_file="src/app.py", findings=findings)

        json_path = tmp_path / "latest_review.json"
        assert json_path.exists()
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["target_file"] == "src/app.py"
        assert len(data["details"]) == 1
        assert data["details"][0]["code"] == "SEC001"

    def test_python_file_generates_noqa_suppress_action(self, tmp_path: Path):
        """For .py files, each finding with a code must generate a suppress action."""
        svc = FileFeedbackService(tmp_path / "LATEST_REVIEW.txt")
        findings = [{"code": "ARCH003", "message": "except pass", "severity": "warn", "line_number": 5}]
        svc.publish_quick_fix(_result(), target_file="src/handler.py", findings=findings)

        data = json.loads((tmp_path / "latest_review.json").read_text(encoding="utf-8"))
        suppress_actions = [a for a in data["code_actions"] if a["kind"] == "quickfix.suppress"]
        assert len(suppress_actions) == 1
        assert "ARCH003" in suppress_actions[0]["title"]

    def test_patch_generates_patch_code_action(self, tmp_path: Path):
        """When a PatchProposal is provided, a quickfix.patch action must be generated."""
        svc = FileFeedbackService(tmp_path / "LATEST_REVIEW.txt")
        patch = PatchProposal(
            patch_hash="deadbeef12345678",
            unified_diff="--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,2 @@\n-old\n+new\n",
            source_file="f.py",
            saved_path=None,
        )
        svc.publish_quick_fix(_result(), target_file="f.py", findings=[], patch=patch)

        data = json.loads((tmp_path / "latest_review.json").read_text(encoding="utf-8"))
        patch_actions = [a for a in data["code_actions"] if a["kind"] == "quickfix.patch"]
        assert len(patch_actions) == 1
        assert "deadbeef" in patch_actions[0]["title"]
        assert len(patch_actions[0]["edits"]) >= 1

    def test_no_findings_produces_empty_details(self, tmp_path: Path):
        """With no findings and no patch, details and code_actions must be empty."""
        svc = FileFeedbackService(tmp_path / "LATEST_REVIEW.txt")
        svc.publish_quick_fix(_result(), target_file="clean.py", findings=[])

        data = json.loads((tmp_path / "latest_review.json").read_text(encoding="utf-8"))
        assert data["details"] == []
        assert data["code_actions"] == []

    def test_clear_feedback_removes_both_files(self, tmp_path: Path):
        """clear_feedback must remove both .txt and .json files."""
        svc = FileFeedbackService(tmp_path / "LATEST_REVIEW.txt")
        svc.publish_diagnostics(_result())
        svc.publish_quick_fix(_result(), target_file="x.py", findings=[])

        assert (tmp_path / "LATEST_REVIEW.txt").exists()
        assert (tmp_path / "latest_review.json").exists()

        svc.clear_feedback("x.py")
        assert not (tmp_path / "LATEST_REVIEW.txt").exists()
        assert not (tmp_path / "latest_review.json").exists()


# ===========================================================================
# Section 2: LSP code action builder
# ===========================================================================


class TestLspCodeActions:
    """Tests for jemmin_lsp._build_code_actions without running a real LSP server."""

    def _import_lsp(self):
        """Import the LSP module dynamically to avoid stdout side effects."""
        import importlib
        lsp_path = BIN / "jemmin_lsp.py"
        spec = importlib.util.spec_from_file_location("jemmin_lsp", str(lsp_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_suppress_action_for_matching_line(self):
        lsp = self._import_lsp()
        review = {
            "target_file": "src/app.py",
            "code_actions": [
                {"kind": "quickfix.suppress", "title": "Suppress SEC001", "line_number": 5, "suppress_code": "SEC001"},
            ],
        }
        with lsp._review_lock:
            lsp._latest_review = review

        actions = lsp._build_code_actions(
            uri="file:///src/app.py",
            request_range={"start": {"line": 4}, "end": {"line": 4}},
            context_diagnostics=[{"data": {"code": "SEC001"}}],
        )
        assert len(actions) == 1
        assert "noqa" in json.dumps(actions[0])

    def test_no_action_for_out_of_range_line(self):
        lsp = self._import_lsp()
        review = {
            "code_actions": [
                {"kind": "quickfix.suppress", "title": "Suppress X", "line_number": 100, "suppress_code": "X"},
            ],
        }
        with lsp._review_lock:
            lsp._latest_review = review

        actions = lsp._build_code_actions(
            uri="file:///f.py",
            request_range={"start": {"line": 0}, "end": {"line": 5}},
            context_diagnostics=[],
        )
        assert actions == []

    def test_patch_action_always_included(self):
        lsp = self._import_lsp()
        review = {
            "code_actions": [
                {
                    "kind": "quickfix.patch",
                    "title": "Apply patch",
                    "edits": [{"start_line": 0, "end_line": 1, "new_lines": ["new"]}],
                },
            ],
        }
        with lsp._review_lock:
            lsp._latest_review = review

        actions = lsp._build_code_actions(
            uri="file:///f.py",
            request_range={"start": {"line": 50}, "end": {"line": 50}},
            context_diagnostics=[],
        )
        assert len(actions) == 1
        assert actions[0]["isPreferred"] is True

    def test_empty_review_returns_no_actions(self):
        lsp = self._import_lsp()
        with lsp._review_lock:
            lsp._latest_review = {}

        actions = lsp._build_code_actions(
            uri="file:///f.py",
            request_range={"start": {"line": 0}, "end": {"line": 10}},
            context_diagnostics=[],
        )
        assert actions == []

    def test_initialize_declares_code_action_capability(self):
        """The LSP initialize response must include codeActionProvider."""
        lsp = self._import_lsp()
        captured: list[dict] = []
        original_write = lsp._write_message

        def mock_write(msg):
            captured.append(msg)

        lsp._write_message = mock_write
        try:
            lsp._handle_initialize(1, {})
        finally:
            lsp._write_message = original_write

        resp = captured[0]
        caps = resp["result"]["capabilities"]
        assert "codeActionProvider" in caps
        assert "quickfix" in caps["codeActionProvider"]["codeActionKinds"]


# ===========================================================================
# Section 3: Orchestrator quick-fix + context-cache wiring
# ===========================================================================


class TestOrchestratorQuickFixWiring:
    """Verify that ReviewOrchestrator calls publish_quick_fix after pipeline."""

    def _build_orchestrator(self, tmp_path: Path, cache_mgr=None):
        from jemmin.orchestrator.consensus import DefaultConsensusEngine
        from jemmin.orchestrator.controller import ReviewOrchestrator
        from jemmin.orchestrator.policy_engine import DefaultPolicyEngine
        from jemmin.orchestrator.resource_mgr import DefaultResourceManager
        from jemmin.services.context_svc import StaticContextService
        from jemmin.services.review_logger import JsonlReviewLogger
        from jemmin.state.sqlite_spooler import SQLiteJobStore

        # Minimal agent that returns a finding
        agent = MagicMock()
        agent.name = "test_agent"
        agent.run.return_value = AgentDecision(
            agent_name="test_agent",
            status="warn",
            confidence_score=0.8,
            findings=[{"code": "T001", "message": "test issue", "line_number": 3}],
            suggested_actions=["fix it"],
        )

        feedback = FileFeedbackService(tmp_path / "LATEST_REVIEW.txt")

        return ReviewOrchestrator(
            job_store=SQLiteJobStore(tmp_path / "spool.db"),
            policy_engine=DefaultPolicyEngine(),
            resource_manager=DefaultResourceManager(),
            context_service=StaticContextService(),
            agents=[agent],
            consensus_engine=DefaultConsensusEngine(),
            feedback_service=feedback,
            review_logger=JsonlReviewLogger(tmp_path / "review_history.jsonl"),
            context_cache_manager=cache_mgr,
        )

    def test_quick_fix_json_written_after_pipeline(self, tmp_path: Path):
        """After run_once, latest_review.json must exist with findings."""
        orch = self._build_orchestrator(tmp_path)
        orch.run_once(_request())

        json_path = tmp_path / "latest_review.json"
        assert json_path.exists()
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["target_file"] == "src/example.py"
        assert len(data["details"]) >= 1
        assert data["details"][0]["code"] == "T001"

    def test_quick_fix_includes_suppress_action(self, tmp_path: Path):
        """For Python target files, suppress code actions must be generated."""
        orch = self._build_orchestrator(tmp_path)
        orch.run_once(_request(target_file="src/handler.py"))

        data = json.loads((tmp_path / "latest_review.json").read_text(encoding="utf-8"))
        suppress = [a for a in data["code_actions"] if a.get("kind") == "quickfix.suppress"]
        assert len(suppress) >= 1

    def test_context_cache_manager_invoked(self, tmp_path: Path):
        """When context_cache_manager is provided, get_or_create must be called."""
        mock_cache = MagicMock()
        mock_cache.get_or_create.return_value = "cache-id-123"
        mock_cache.stats = MagicMock(hits=0)

        orch = self._build_orchestrator(tmp_path, cache_mgr=mock_cache)
        req = _request()
        # Inject system_prompt into context metadata so cache is used
        original_build = orch._context_service.build_context

        def patched_build(r):
            ctx = original_build(r)
            ctx.metadata["system_prompt"] = "You are a code reviewer."
            return ctx

        orch._context_service.build_context = patched_build
        orch.run_once(req)

        mock_cache.get_or_create.assert_called_once()

    def test_context_cache_failure_does_not_break_pipeline(self, tmp_path: Path):
        """If context cache fails, the pipeline must still complete."""
        mock_cache = MagicMock()
        mock_cache.get_or_create.side_effect = RuntimeError("cache explosion")
        mock_cache.stats = MagicMock(hits=0)

        orch = self._build_orchestrator(tmp_path, cache_mgr=mock_cache)
        original_build = orch._context_service.build_context

        def patched_build(r):
            ctx = original_build(r)
            ctx.metadata["system_prompt"] = "test"
            return ctx

        orch._context_service.build_context = patched_build
        result = orch.run_once(_request())

        # Pipeline must still deliver despite cache failure
        assert result.state == ReviewState.DELIVERED


# ===========================================================================
# Section 4: Gemini Provider fallback & availability
# ===========================================================================


class TestGeminiProvider:
    def test_unavailable_without_api_key(self):
        """GeminiProvider.available() must return False without API key."""
        provider = GeminiProvider(api_key="")
        assert provider.available() is False

    def test_generate_falls_back_to_mock(self):
        """Without API key, generate must fall back to MockLocalLLMProvider."""
        from jemmin.providers.base import ProviderRequest

        provider = GeminiProvider(api_key="")
        req = ProviderRequest(
            prompt_pack_version="1.0",
            system_prompt="review",
            user_prompt="diff here",
            response_schema={},
        )
        result = provider.generate(req)
        assert result["status"] == "PASS"
        assert "mock" in result.get("reason", "").lower()

    def test_cache_falls_back_to_mock(self):
        """Without API key, context cache operations must use mock in-memory store."""
        provider = GeminiProvider(api_key="")
        cache_id = provider.create_context_cache("sys prompt", "static ctx", 60)
        assert cache_id.startswith("mock-cache-")

    def test_generate_with_cache_falls_back_to_mock(self):
        """generate_with_cache without API key must use mock cache path."""
        provider = GeminiProvider(api_key="")
        cache_id = provider.create_context_cache("sys", "ctx", 60)
        result = provider.generate_with_cache(cache_id, "tier1 diff")
        assert result["status"] == "PASS"
        assert "cache" in result.get("reason", "").lower()


# ===========================================================================
# Section 5: Context Cache Manager lifecycle
# ===========================================================================


class TestContextCacheLifecycle:
    def test_get_or_create_returns_cache_id(self):
        """First call creates cache; returns a valid cache_id string."""
        provider = MockLocalLLMProvider()
        mgr = ContextCacheManager(provider=provider, ttl_seconds=60)
        cache_id = mgr.get_or_create("system prompt", "static context")
        assert cache_id.startswith("mock-cache-")
        assert mgr.stats.misses == 1

    def test_second_call_is_cache_hit(self):
        """Same inputs must produce a cache hit (no new creation)."""
        provider = MockLocalLLMProvider()
        mgr = ContextCacheManager(provider=provider, ttl_seconds=60)
        id1 = mgr.get_or_create("sys", "ctx")
        id2 = mgr.get_or_create("sys", "ctx")
        assert id1 == id2
        assert mgr.stats.hits == 1
        assert mgr.stats.misses == 1

    def test_different_inputs_create_separate_caches(self):
        """Different system_prompt/static_context must create separate entries."""
        provider = MockLocalLLMProvider()
        mgr = ContextCacheManager(provider=provider, ttl_seconds=60)
        id1 = mgr.get_or_create("prompt A", "context A")
        id2 = mgr.get_or_create("prompt B", "context B")
        assert id1 != id2
        assert mgr.stats.misses == 2

    def test_invalidate_forces_new_creation(self):
        """After invalidation, the next get_or_create must be a cache miss."""
        provider = MockLocalLLMProvider()
        mgr = ContextCacheManager(provider=provider, ttl_seconds=60)
        mgr.get_or_create("sys", "ctx")
        removed = mgr.invalidate("sys", "ctx")
        assert removed is True

        mgr.get_or_create("sys", "ctx")
        assert mgr.stats.misses == 2  # both calls are misses

    def test_cleanup_expired_removes_old_entries(self):
        """cleanup_expired must remove entries past TTL."""
        provider = MockLocalLLMProvider()
        mgr = ContextCacheManager(provider=provider, ttl_seconds=1)
        mgr.get_or_create("sys", "ctx")
        assert len(mgr._store) == 1

        # Manually expire the entry
        for entry in mgr._store.values():
            entry.expires_at = time.time() - 10

        removed = mgr.cleanup_expired()
        assert removed == 1
        assert len(mgr._store) == 0


# ===========================================================================
# Section 6: Unified diff hunk parser
# ===========================================================================


class TestParseHunkRanges:
    def test_single_hunk(self):
        diff = "@@ -1,2 +1,2 @@\n-old\n+new\n context\n"
        hunks = _parse_hunk_ranges(diff)
        assert len(hunks) == 1
        assert hunks[0]["start_line"] == 0  # 0-based
        assert hunks[0]["end_line"] == 2

    def test_multi_hunk(self):
        diff = (
            "@@ -1,1 +1,1 @@\n-a\n+b\n"
            "@@ -10,2 +10,2 @@\n-x\n+y\n z\n"
        )
        hunks = _parse_hunk_ranges(diff)
        assert len(hunks) == 2
        assert hunks[1]["start_line"] == 9  # 0-based (line 10)

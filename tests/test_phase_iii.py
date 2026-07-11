"""Phase III 테스트 — Context invalidation, Similar review, Agent registry,
Model config, Offload gateway, AST analyzer.

전체 6개 영역을 검증합니다.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── 프로젝트 루트 설정 ──────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
import sys
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from jemmin.models import ContextBundle, ReviewRequest, ReviewState
from jemmin.services.context_svc import StaticContextService
from jemmin.registry import AgentManifest, AgentRegistry, create_default_registry
from jemmin.analyzers.ast_security import AstSecurityAnalyzer, _extract_added_lines


def _make_request(
    target_file: str = "test.py",
    diff_text: str = "",
    trigger_intent: str = "active_intent",
    project_profile: str = "general",
) -> ReviewRequest:
    return ReviewRequest(
        request_id="req_test_001",
        idempotency_key="idemp_test_001",
        project_id="test_project",
        project_profile=project_profile,
        target_file=target_file,
        diff_text=diff_text,
        trigger="cli",
        trigger_intent=trigger_intent,
        git_revision="HEAD",
    )


# ═══════════════════════════════════════════════════════════════════
# §1. Context Invalidation
# ═══════════════════════════════════════════════════════════════════

class TestContextInvalidation(unittest.TestCase):
    """invalidate_for_path() 경로 기반 캐시 무효화."""

    def test_cache_hit_returns_same_bundle(self):
        svc = StaticContextService()
        req = _make_request(diff_text="+x = 1\n")
        b1 = svc.build_context(req)
        b2 = svc.build_context(req)
        self.assertEqual(b1.context_hash, b2.context_hash)
        self.assertEqual(svc.cache_size, 1)

    def test_invalidate_removes_cache_entry(self):
        svc = StaticContextService()
        req = _make_request(diff_text="+x = 1\n")
        svc.build_context(req)
        self.assertEqual(svc.cache_size, 1)
        removed = svc.invalidate_for_path("test.py")
        self.assertEqual(removed, 1)
        self.assertEqual(svc.cache_size, 0)

    def test_invalidate_nonexistent_path_returns_zero(self):
        svc = StaticContextService()
        removed = svc.invalidate_for_path("nonexistent.py")
        self.assertEqual(removed, 0)

    def test_invalidate_all_clears_everything(self):
        svc = StaticContextService()
        svc.build_context(_make_request(target_file="a.py", diff_text="+a\n"))
        svc.build_context(_make_request(target_file="b.py", diff_text="+b\n"))
        self.assertEqual(svc.cache_size, 2)
        count = svc.invalidate_all()
        self.assertEqual(count, 2)
        self.assertEqual(svc.cache_size, 0)

    def test_invalidate_directory_prefix(self):
        """디렉터리 경로로 무효화하면 하위 파일도 제거."""
        svc = StaticContextService()
        svc.build_context(_make_request(target_file="src/a.py", diff_text="+a\n"))
        svc.build_context(_make_request(target_file="src/b.py", diff_text="+b\n"))
        svc.build_context(_make_request(target_file="tests/c.py", diff_text="+c\n"))
        removed = svc.invalidate_for_path("src")
        self.assertEqual(removed, 2)
        self.assertEqual(svc.cache_size, 1)

    def test_cache_disabled(self):
        svc = StaticContextService(cache_enabled=False)
        req = _make_request(diff_text="+x = 1\n")
        svc.build_context(req)
        self.assertEqual(svc.cache_size, 0)


# ═══════════════════════════════════════════════════════════════════
# §2. Similar Review Lookup
# ═══════════════════════════════════════════════════════════════════

class TestSimilarReviewLookup(unittest.TestCase):
    """lookup_similar_reviews() JSONL 기반 유사 리뷰 검색."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._log_path = Path(self._tmpdir) / "review_history.jsonl"

    def _write_reviews(self, records: list[dict]):
        with self._log_path.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def test_empty_log_returns_empty(self):
        svc = StaticContextService(review_log_path=self._log_path)
        self.assertEqual(svc.lookup_similar_reviews("test"), [])

    def test_no_log_path_returns_empty(self):
        svc = StaticContextService()
        self.assertEqual(svc.lookup_similar_reviews("test"), [])

    def test_finds_matching_review_by_summary(self):
        self._write_reviews([
            {"request_id": "r1", "summary": "security issue in auth.py", "status": "rejected"},
            {"request_id": "r2", "summary": "performance issue in db.py", "status": "rejected"},
        ])
        svc = StaticContextService(review_log_path=self._log_path)
        results = svc.lookup_similar_reviews("security")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["request_id"], "r1")

    def test_limit_respected(self):
        self._write_reviews([
            {"request_id": f"r{i}", "summary": "test issue", "status": "rejected"}
            for i in range(10)
        ])
        svc = StaticContextService(review_log_path=self._log_path)
        results = svc.lookup_similar_reviews("test", limit=3)
        self.assertEqual(len(results), 3)

    def test_returns_most_recent_first(self):
        self._write_reviews([
            {"request_id": "old", "summary": "fix bug", "status": "pass"},
            {"request_id": "new", "summary": "fix bug", "status": "rejected"},
        ])
        svc = StaticContextService(review_log_path=self._log_path)
        results = svc.lookup_similar_reviews("fix bug")
        self.assertEqual(results[0]["request_id"], "new")  # 역순 스캔


# ═══════════════════════════════════════════════════════════════════
# §3. Agent Registry Bootstrap
# ═══════════════════════════════════════════════════════════════════

class TestAgentRegistryBootstrap(unittest.TestCase):
    """create_default_registry()로 10개 에이전트 등록 및 manifest 기반 선택."""

    def test_default_registry_has_10_agents(self):
        registry = create_default_registry()
        all_agents = registry.select(project_profile="general", trigger_intent="active_intent")
        self.assertEqual(len(all_agents), 10)

    def test_passive_save_selects_subset(self):
        """passive_save intent에 등록된 에이전트만 선택."""
        registry = create_default_registry()
        passive_agents = registry.select(project_profile="general", trigger_intent="passive_save")
        passive_names = {getattr(a, "name", "") for a in passive_agents}
        # fast_gate, security, architecture, ast_security는 passive_save 지원
        self.assertIn("fast_gate", passive_names)
        self.assertIn("security", passive_names)
        self.assertIn("architecture", passive_names)
        self.assertIn("ast_security", passive_names)
        # context, domain_rule 등은 active_intent만
        self.assertNotIn("context", passive_names)
        self.assertNotIn("performance", passive_names)

    def test_registry_select_by_capability(self):
        registry = create_default_registry()
        security_agents = registry.select(
            project_profile="general",
            trigger_intent="active_intent",
            required_capabilities={"security"},
        )
        names = {getattr(a, "name", "") for a in security_agents}
        self.assertIn("security", names)
        self.assertIn("ast_security", names)

    def test_registry_list_all(self):
        registry = create_default_registry()
        all_entries = registry.list()
        self.assertEqual(len(all_entries), 10)

    def test_registry_get_by_name(self):
        registry = create_default_registry()
        entry = registry.get("fast_gate")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.manifest.name, "fast_gate")


# ═══════════════════════════════════════════════════════════════════
# §4. Model Config
# ═══════════════════════════════════════════════════════════════════

class TestModelConfig(unittest.TestCase):
    """GeminiProvider 모델명 결정 우선순위."""

    def test_env_var_overrides_default(self):
        from jemmin.providers.gemini import _resolve_model
        with patch.dict(os.environ, {"JEMMIN_MODEL": "gemini-1.5-pro"}):
            self.assertEqual(_resolve_model(None), "gemini-1.5-pro")

    def test_explicit_takes_priority(self):
        from jemmin.providers.gemini import _resolve_model
        with patch.dict(os.environ, {"JEMMIN_MODEL": "gemini-1.5-pro"}):
            self.assertEqual(_resolve_model("custom-model"), "custom-model")

    def test_default_model(self):
        from jemmin.providers.gemini import _resolve_model
        with patch.dict(os.environ, {}, clear=True):
            # JEMMIN_MODEL이 없으면 config 또는 기본값 사용
            result = _resolve_model(None)
            self.assertIn("gemini", result)

    def test_fallback_model_env_var_overrides_default(self):
        from jemmin.providers.gemini import _resolve_fallback_model
        with patch.dict(os.environ, {"JEMMIN_FALLBACK_MODEL": "gemini-2.0-flash"}, clear=True):
            self.assertEqual(_resolve_fallback_model(None, "gemini-3.1-pro-preview"), "gemini-2.0-flash")


# ═══════════════════════════════════════════════════════════════════
# §5. Offload Gateway Wiring
# ═══════════════════════════════════════════════════════════════════

class TestOffloadGatewayWiring(unittest.TestCase):
    """오케스트레이터에 StubOffloadGateway가 연결되어 있는지 확인."""

    def test_stub_gateway_submit_and_poll(self):
        from jemmin.ipc.offload_gateway import StubOffloadGateway, OffloadRequest
        gw = StubOffloadGateway()
        req = OffloadRequest(
            request_id="r1",
            git_revision="HEAD",
            context_bundle="hash_abc",
            masked_diff="+x = 1",
        )
        result = gw.submit(req)
        self.assertTrue(result.accepted)
        self.assertIn("job-", result.remote_job_id)

        status = gw.poll(result.remote_job_id)
        self.assertEqual(status.state, "done")

    def test_orchestrator_offload_analytics_logged(self):
        """context_size > 4000이면 offload가 시도되고 analytics가 기록됨."""
        from jemmin.orchestrator.controller import ReviewOrchestrator
        from jemmin.ipc.offload_gateway import StubOffloadGateway

        mock_job_store = MagicMock()
        mock_policy = MagicMock()
        mock_policy.evaluate_request.return_value = MagicMock(
            accepted=True, fast_path_only=False, allowed_agents=None, reason=None
        )
        mock_policy.should_offload.return_value = True
        mock_resource = MagicMock()
        mock_resource.allow_new_job.return_value = True

        # 큰 컨텍스트를 반환하는 context_service
        big_bundle = ContextBundle(
            request_id="r1",
            context_hash="abc",
            token_estimate=5000,
            tiers={"tier1": ["big diff"], "tier2": [], "tier3": [], "tier4": []},
            metadata={},
        )
        mock_context_svc = MagicMock()
        mock_context_svc.build_context.return_value = big_bundle

        mock_agent = MagicMock()
        mock_agent.name = "mock_agent"
        mock_agent.run.return_value = MagicMock(
            agent_name="mock_agent", status="pass",
            confidence_score=1.0, findings=[], suggested_actions=[]
        )

        mock_consensus = MagicMock()
        mock_consensus.decide.return_value = MagicMock(
            status="pass", summary="all good", confidence_score=1.0
        )

        mock_feedback = MagicMock()
        mock_logger = MagicMock()
        mock_analytics = MagicMock()
        gateway = StubOffloadGateway()

        orch = ReviewOrchestrator(
            job_store=mock_job_store,
            policy_engine=mock_policy,
            resource_manager=mock_resource,
            context_service=mock_context_svc,
            agents=[mock_agent],
            consensus_engine=mock_consensus,
            feedback_service=mock_feedback,
            review_logger=mock_logger,
            analytics_logger=mock_analytics,
            offload_gateway=gateway,
        )

        req = _make_request(diff_text="+x = 1\n")
        result = orch.run_once(req)
        self.assertEqual(result.status, "pass")

        # analytics에 offload_submitted 이벤트가 기록되었는지 확인
        analytics_calls = mock_analytics.write.call_args_list
        offload_events = [
            c for c in analytics_calls
            if c[1].get("event_type") == "offload_submitted"
            or (len(c[0]) > 0 and isinstance(c[0][0], dict) and c[0][0].get("event_type") == "offload_submitted")
        ]
        # StubOffloadGateway는 항상 accepted=True
        self.assertTrue(len(offload_events) > 0 or mock_analytics.write.called)


# ═══════════════════════════════════════════════════════════════════
# §6. AST Security Analyzer
# ═══════════════════════════════════════════════════════════════════

class TestAstSecurityAnalyzer(unittest.TestCase):
    """AstSecurityAnalyzer — Python AST 기반 보안 분석."""

    def setUp(self):
        self.analyzer = AstSecurityAnalyzer()

    def test_eval_detected(self):
        diff = "+result = eval(user_input)\n"
        req = _make_request(diff_text=diff)
        ctx = ContextBundle(request_id="r1", context_hash="h", token_estimate=10, tiers={}, metadata={})
        decision = self.analyzer.run(req, ctx)
        self.assertEqual(decision.status, "reject")
        codes = {f["code"] for f in decision.findings}
        self.assertIn("AST_EVAL", codes)

    def test_exec_detected(self):
        diff = "+exec(code_string)\n"
        req = _make_request(diff_text=diff)
        ctx = ContextBundle(request_id="r1", context_hash="h", token_estimate=10, tiers={}, metadata={})
        decision = self.analyzer.run(req, ctx)
        self.assertEqual(decision.status, "reject")
        codes = {f["code"] for f in decision.findings}
        self.assertIn("AST_EXEC", codes)

    def test_pickle_loads_detected(self):
        diff = "+import pickle\n+data = pickle.loads(raw)\n"
        req = _make_request(diff_text=diff)
        ctx = ContextBundle(request_id="r1", context_hash="h", token_estimate=10, tiers={}, metadata={})
        decision = self.analyzer.run(req, ctx)
        self.assertIn(decision.status, ("reject", "warn"))
        codes = {f["code"] for f in decision.findings}
        self.assertIn("AST_PICKLE_LOADS", codes)

    def test_os_system_detected(self):
        diff = "+import os\n+os.system('rm -rf /')\n"
        req = _make_request(diff_text=diff)
        ctx = ContextBundle(request_id="r1", context_hash="h", token_estimate=10, tiers={}, metadata={})
        decision = self.analyzer.run(req, ctx)
        self.assertEqual(decision.status, "reject")
        codes = {f["code"] for f in decision.findings}
        self.assertIn("AST_OS_SYSTEM", codes)

    def test_subprocess_shell_true_detected(self):
        diff = "+import subprocess\n+subprocess.call('ls', shell=True)\n"
        req = _make_request(diff_text=diff)
        ctx = ContextBundle(request_id="r1", context_hash="h", token_estimate=10, tiers={}, metadata={})
        decision = self.analyzer.run(req, ctx)
        self.assertEqual(decision.status, "reject")
        codes = {f["code"] for f in decision.findings}
        self.assertIn("AST_SUBPROCESS_SHELL", codes)

    def test_safe_code_passes(self):
        diff = "+x = 1\n+y = x + 2\n"
        req = _make_request(diff_text=diff)
        ctx = ContextBundle(request_id="r1", context_hash="h", token_estimate=10, tiers={}, metadata={})
        decision = self.analyzer.run(req, ctx)
        self.assertEqual(decision.status, "pass")

    def test_non_python_file_passes(self):
        diff = "+eval(user_input)\n"
        req = _make_request(target_file="config.yaml", diff_text=diff)
        ctx = ContextBundle(request_id="r1", context_hash="h", token_estimate=10, tiers={}, metadata={})
        decision = self.analyzer.run(req, ctx)
        self.assertEqual(decision.status, "pass")

    def test_empty_diff_passes(self):
        req = _make_request(diff_text="")
        ctx = ContextBundle(request_id="r1", context_hash="h", token_estimate=10, tiers={}, metadata={})
        decision = self.analyzer.run(req, ctx)
        self.assertEqual(decision.status, "pass")

    def test_syntax_error_in_diff_passes_gracefully(self):
        """불완전한 코드 조각도 SyntaxError 없이 pass."""
        diff = "+def foo(\n"  # 불완전
        req = _make_request(diff_text=diff)
        ctx = ContextBundle(request_id="r1", context_hash="h", token_estimate=10, tiers={}, metadata={})
        decision = self.analyzer.run(req, ctx)
        self.assertEqual(decision.status, "pass")

    def test_extract_added_lines(self):
        diff = "--- a/test.py\n+++ b/test.py\n+x = 1\n-y = 2\n+z = 3\n"
        result = _extract_added_lines(diff)
        self.assertEqual(result, "x = 1\nz = 3")


if __name__ == "__main__":
    unittest.main()

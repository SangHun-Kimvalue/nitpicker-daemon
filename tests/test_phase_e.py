"""Phase E 테스트 — Trigger Intent 라우팅, Context Cache, DuckDB, GeminiProvider, OffloadGateway."""
from __future__ import annotations

import time
import unittest
from dataclasses import replace

import pytest

from jemmin.ipc.offload_gateway import (
    OffloadRequest,
    StubOffloadGateway,
)
from jemmin.models import ReviewRequest
from jemmin.orchestrator.policy_engine import DefaultPolicyEngine, PolicyDecision
from jemmin.orchestrator.resource_mgr import DefaultResourceManager
from jemmin.providers.gemini import GeminiProvider
from jemmin.providers.local_llm import MockLocalLLMProvider, _CACHE_STORE
from jemmin.services.cache_mgr import ContextCacheManager
from jemmin.utils.duckdb_logger import DuckDbLogger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _req(
    intent: str = "active_intent",
    target: str = "",
    profile: str = "general",
) -> ReviewRequest:
    return ReviewRequest(
        request_id="e-req",
        idempotency_key="e-idem",
        project_id="e-proj",
        project_profile=profile,
        trigger="cli",
        trigger_intent=intent,  # type: ignore[arg-type]
        target_file=target,
        diff_text="+ new line",
    )


# ===========================================================================
# § 1. ReviewRequest — trigger_intent 필드
# ===========================================================================


class TestReviewRequestTriggerIntent:
    def test_default_is_active_intent(self) -> None:
        req = ReviewRequest(
            request_id="r1",
            idempotency_key="k1",
            project_id="p1",
            project_profile="general",
            trigger="cli",
        )
        assert req.trigger_intent == "active_intent"

    def test_passive_save_accepted(self) -> None:
        req = _req(intent="passive_save")
        assert req.trigger_intent == "passive_save"

    def test_replace_preserves_intent(self) -> None:
        req = _req(intent="passive_save")
        req2 = replace(req, trigger_intent="active_intent")
        assert req2.trigger_intent == "active_intent"


# ===========================================================================
# § 2. PolicyEngine — Fast Path / Heavy Path 분기
# ===========================================================================


class TestPolicyEngineIntentRouting:
    def setup_method(self) -> None:
        self.engine = DefaultPolicyEngine(max_file_size_bytes=512_000)

    def test_active_intent_returns_heavy_path(self) -> None:
        decision = self.engine.evaluate_request(_req("active_intent"))
        assert decision.accepted is True
        assert decision.fast_path_only is False
        assert decision.offload_allowed is True
        assert decision.allowed_agents is None

    def test_passive_save_returns_fast_path(self) -> None:
        decision = self.engine.evaluate_request(_req("passive_save"))
        assert decision.accepted is True
        assert decision.fast_path_only is True
        assert decision.offload_allowed is False
        assert "fast_gate" in (decision.allowed_agents or set())

    def test_passive_save_should_not_offload(self) -> None:
        req = _req("passive_save")
        assert self.engine.should_offload(req, context_size=9999) is False

    def test_active_intent_may_offload_large_context(self) -> None:
        req = _req("active_intent")
        assert self.engine.should_offload(req, context_size=5000) is True

    def test_active_intent_small_context_no_offload(self) -> None:
        req = _req("active_intent")
        assert self.engine.should_offload(req, context_size=100) is False

    def test_passive_save_never_promotes_deep(self) -> None:
        req = _req("passive_save")
        assert self.engine.should_promote_deep_review(req, {"has_errors": True}) is False

    def test_active_intent_promotes_on_errors(self) -> None:
        req = _req("active_intent")
        assert self.engine.should_promote_deep_review(req, {"has_errors": True}) is True

    def test_custom_fast_path_agents(self) -> None:
        engine = DefaultPolicyEngine(fast_path_agents=frozenset({"my_agent"}))
        decision = engine.evaluate_request(_req("passive_save"))
        assert decision.allowed_agents == frozenset({"my_agent"})


# ===========================================================================
# § 3. ResourceManager — Debounce 분기
# ===========================================================================


class TestResourceManagerDebounce:
    def test_active_intent_always_allowed(self) -> None:
        mgr = DefaultResourceManager(token_budget=100_000)
        req = _req("active_intent", target="foo.py")
        assert mgr.allow_new_job(req) is True
        # 두 번 연속 호출도 허용 (debounce 0)
        assert mgr.allow_new_job(req) is True

    def test_passive_save_first_call_allowed(self) -> None:
        mgr = DefaultResourceManager(token_budget=100_000)
        req = _req("passive_save", target="bar.py")
        assert mgr.allow_new_job(req) is True

    def test_passive_save_second_call_debounced(self) -> None:
        mgr = DefaultResourceManager(token_budget=100_000)
        req = _req("passive_save", target="baz.py")
        mgr.allow_new_job(req)  # 첫 호출 → 허용
        result = mgr.allow_new_job(req)  # 즉시 재호출 → debounce 창 내
        assert result is False

    def test_circuit_breaker_blocks_all(self) -> None:
        mgr = DefaultResourceManager()
        mgr.open_circuit("overload")
        assert mgr.allow_new_job(_req("active_intent")) is False
        mgr.close_circuit()
        assert mgr.allow_new_job(_req("active_intent")) is True

    def test_custom_debounce_zero_passive(self) -> None:
        mgr = DefaultResourceManager(debounce={"passive_save": 0.0, "active_intent": 0.0})
        req = _req("passive_save", target="x.py")
        mgr.allow_new_job(req)
        assert mgr.allow_new_job(req) is True  # 0초 debounce → 즉시 허용

    def test_token_budget_reserve_release(self) -> None:
        mgr = DefaultResourceManager(token_budget=500)
        assert mgr.reserve_tokens("req-1", 300) is True
        assert mgr.reserve_tokens("req-2", 300) is False  # 예산 초과
        mgr.release_tokens("req-1")  # 현재 구현은 실제 환급 없음 (no-op)


# ===========================================================================
# § 4. MockLocalLLMProvider — Cache API
# ===========================================================================


class TestMockLocalLLMProviderCache:
    def setup_method(self) -> None:
        _CACHE_STORE.clear()
        self.provider = MockLocalLLMProvider()

    def test_create_context_cache_returns_id(self) -> None:
        cid = self.provider.create_context_cache("sys", "ctx")
        assert cid.startswith("mock-cache-")
        assert cid in _CACHE_STORE

    def test_generate_with_cache_hit(self) -> None:
        cid = self.provider.create_context_cache("sys", "ctx", ttl_seconds=3600)
        result = self.provider.generate_with_cache(cid, "diff content")
        assert result["reason"] == "mock cache hit"
        assert result.get("cache_id") == cid

    def test_generate_with_cache_miss(self) -> None:
        result = self.provider.generate_with_cache("nonexistent-id", "diff")
        assert "fallback" in result["reason"]

    def test_generate_with_expired_cache(self) -> None:
        cid = self.provider.create_context_cache("sys", "ctx", ttl_seconds=-1)
        result = self.provider.generate_with_cache(cid, "diff")
        assert "fallback" in result["reason"]

    def test_delete_context_cache(self) -> None:
        cid = self.provider.create_context_cache("sys", "ctx")
        self.provider.delete_context_cache(cid)
        assert cid not in _CACHE_STORE

    def test_delete_nonexistent_is_safe(self) -> None:
        self.provider.delete_context_cache("ghost-id")  # 예외 없이 통과


# ===========================================================================
# § 5. GeminiProvider — 폴백 + 인터페이스 준수
# ===========================================================================


class TestGeminiProvider:
    def setup_method(self) -> None:
        _CACHE_STORE.clear()
        # google-genai 미설치 환경 → MockLocalLLMProvider 폴백
        self.provider = GeminiProvider(api_key="", model="gemini-2.0-flash")

    def test_available_without_api_key(self) -> None:
        assert self.provider.available() is False

    def test_name_is_gemini(self) -> None:
        assert self.provider.name == "gemini"

    def test_generate_falls_back_to_mock(self) -> None:
        from jemmin.providers.base import ProviderRequest
        req = ProviderRequest(
            prompt_pack_version="1",
            system_prompt="sys",
            user_prompt="user",
            response_schema={},
        )
        result = self.provider.generate(req)
        assert result["status"] == "PASS"

    def test_create_cache_falls_back_to_mock(self) -> None:
        cid = self.provider.create_context_cache("sys", "ctx")
        assert cid.startswith("mock-cache-")

    def test_generate_with_cache_falls_back(self) -> None:
        cid = self.provider.create_context_cache("sys", "ctx")
        result = self.provider.generate_with_cache(cid, "diff")
        assert result["status"] == "PASS"

    def test_delete_cache_falls_back(self) -> None:
        cid = self.provider.create_context_cache("sys", "ctx")
        self.provider.delete_context_cache(cid)
        assert cid not in _CACHE_STORE


# ===========================================================================
# § 6. ContextCacheManager
# ===========================================================================


class TestContextCacheManager:
    def setup_method(self) -> None:
        _CACHE_STORE.clear()
        self.provider = MockLocalLLMProvider()
        self.mgr = ContextCacheManager(self.provider, ttl_seconds=3600)

    def test_get_or_create_returns_cache_id(self) -> None:
        cid = self.mgr.get_or_create("sys", "ctx")
        assert cid.startswith("mock-cache-")

    def test_same_context_reuses_cache(self) -> None:
        cid1 = self.mgr.get_or_create("sys", "ctx")
        cid2 = self.mgr.get_or_create("sys", "ctx")
        assert cid1 == cid2
        assert self.mgr.stats.hits == 1
        assert self.mgr.stats.misses == 1

    def test_different_context_creates_new_cache(self) -> None:
        cid1 = self.mgr.get_or_create("sys", "ctx-A")
        cid2 = self.mgr.get_or_create("sys", "ctx-B")
        assert cid1 != cid2
        assert self.mgr.stats.misses == 2

    def test_invalidate_removes_entry(self) -> None:
        cid = self.mgr.get_or_create("sys", "ctx")
        removed = self.mgr.invalidate("sys", "ctx")
        assert removed is True
        # 재조회 시 새 캐시 생성
        cid2 = self.mgr.get_or_create("sys", "ctx")
        assert cid2 != cid
        assert self.mgr.stats.evictions == 1

    def test_invalidate_nonexistent_returns_false(self) -> None:
        result = self.mgr.invalidate("ghost", "ghost")
        assert result is False

    def test_cleanup_expired_removes_expired(self) -> None:
        mgr = ContextCacheManager(self.provider, ttl_seconds=-1)
        mgr.get_or_create("sys", "ctx")
        count = mgr.cleanup_expired()
        assert count == 1

    def test_cleanup_not_expired_keeps_entry(self) -> None:
        self.mgr.get_or_create("sys", "ctx")
        count = self.mgr.cleanup_expired()
        assert count == 0

    def test_clear_all_empties_store(self) -> None:
        self.mgr.get_or_create("sys", "ctx-A")
        self.mgr.get_or_create("sys", "ctx-B")
        count = self.mgr.clear_all()
        assert count == 2
        assert len(self.mgr._store) == 0  # noqa: SLF001

    def test_max_entries_evicts_oldest(self) -> None:
        mgr = ContextCacheManager(self.provider, ttl_seconds=3600, max_entries=2)
        mgr.get_or_create("sys", "ctx-1")
        mgr.get_or_create("sys", "ctx-2")
        mgr.get_or_create("sys", "ctx-3")  # evict 발생
        assert len(mgr._store) <= 2  # noqa: SLF001


# ===========================================================================
# § 7. DuckDbLogger
# ===========================================================================


class TestDuckDbLogger:
    def setup_method(self) -> None:
        self.logger = DuckDbLogger(db_path=":memory:")

    def teardown_method(self) -> None:
        self.logger.close()

    def test_write_basic_event(self) -> None:
        self.logger.write({"request_id": "r1", "event_type": "review", "status": "PASS"})
        rows = self.logger.query("SELECT * FROM review_events")
        assert len(rows) == 1
        assert rows[0]["request_id"] == "r1"

    def test_write_multiple_events(self) -> None:
        for i in range(5):
            self.logger.write({"request_id": f"r{i}", "event_type": "agent_run"})
        rows = self.logger.query("SELECT COUNT(*) AS cnt FROM review_events")
        assert rows[0]["cnt"] == 5

    def test_write_with_full_fields(self) -> None:
        self.logger.write({
            "request_id": "full",
            "event_type": "llm_call",
            "status": "PASS",
            "result_code": "REVIEW_PASSED",
            "confidence_score": 0.95,
            "latency_ms": 123.4,
            "token_input": 1000,
            "token_output": 200,
            "cost_usd": 0.003,
            "agent_name": "fast_gate",
            "findings_cnt": 0,
        })
        rows = self.logger.query("SELECT * FROM review_events WHERE request_id = 'full'")
        assert len(rows) == 1
        row = rows[0]
        assert abs(row["confidence"] - 0.95) < 1e-9
        assert row["agent_name"] == "fast_gate"

    def test_cost_summary_groups_by_event_type(self) -> None:
        for _ in range(3):
            self.logger.write({"event_type": "llm_call", "cost_usd": 0.01})
        for _ in range(2):
            self.logger.write({"event_type": "agent_run", "cost_usd": 0.0})
        summary = self.logger.cost_summary()
        types = {row["event_type"] for row in summary}
        assert "llm_call" in types

    def test_write_noop_when_no_duckdb(self) -> None:
        """duckdb 미설치 상태를 시뮬레이션."""
        import jemmin.utils.duckdb_logger as mod
        original = mod._HAS_DUCKDB  # noqa: SLF001
        mod._HAS_DUCKDB = False  # noqa: SLF001
        try:
            # 예외 없이 no-op 처리
            logger_stub = DuckDbLogger.__new__(DuckDbLogger)
            logger_stub._conn = None  # noqa: SLF001
            logger_stub._lock = __import__("threading").Lock()  # noqa: SLF001
            logger_stub.write({"event_type": "test"})
        finally:
            mod._HAS_DUCKDB = original  # noqa: SLF001

    def test_query_returns_empty_when_no_duckdb(self) -> None:
        import jemmin.utils.duckdb_logger as mod
        original = mod._HAS_DUCKDB  # noqa: SLF001
        mod._HAS_DUCKDB = False  # noqa: SLF001
        try:
            logger_stub = DuckDbLogger.__new__(DuckDbLogger)
            logger_stub._conn = None  # noqa: SLF001
            logger_stub._lock = __import__("threading").Lock()  # noqa: SLF001
            assert logger_stub.query("SELECT 1") == []
        finally:
            mod._HAS_DUCKDB = original  # noqa: SLF001

    def test_ids_are_sequential(self) -> None:
        self.logger.write({"event_type": "a"})
        self.logger.write({"event_type": "b"})
        rows = self.logger.query("SELECT id FROM review_events ORDER BY id")
        assert rows[0]["id"] == 1
        assert rows[1]["id"] == 2


# ===========================================================================
# § 8. StubOffloadGateway
# ===========================================================================


class TestStubOffloadGateway:
    def setup_method(self) -> None:
        self.gw = StubOffloadGateway()

    def _make_offload_req(self) -> OffloadRequest:
        return OffloadRequest(
            request_id="e-req",
            git_revision="HEAD",
            context_bundle="bundle",
            masked_diff="+ code",
        )

    def test_submit_returns_accepted(self) -> None:
        req = self._make_offload_req()
        result = self.gw.submit(req)
        assert result.accepted is True
        assert result.request_id == "e-req"
        assert result.remote_job_id.startswith("job-")

    def test_poll_after_submit_returns_done(self) -> None:
        req = self._make_offload_req()
        result = self.gw.submit(req)
        status = self.gw.poll(result.remote_job_id)
        assert status.state == "done"
        assert status.result is not None

    def test_poll_unknown_job_returns_failed(self) -> None:
        status = self.gw.poll("nonexistent-job")
        assert status.state == "failed"

    def test_cancel_removes_job(self) -> None:
        req = self._make_offload_req()
        result = self.gw.submit(req)
        self.gw.cancel(result.remote_job_id)
        status = self.gw.poll(result.remote_job_id)
        assert status.state == "failed"

    def test_multiple_submit_unique_ids(self) -> None:
        ids = {self.gw.submit(self._make_offload_req()).remote_job_id for _ in range(10)}
        assert len(ids) == 10

    def test_offload_request_strict_mode_default(self) -> None:
        req = self._make_offload_req()
        assert req.verification_mode == "strict"

    def test_offload_request_lenient_mode(self) -> None:
        req = OffloadRequest(
            request_id="r",
            git_revision="HEAD",
            context_bundle="b",
            masked_diff="d",
            verification_mode="lenient",
        )
        assert req.verification_mode == "lenient"


# ===========================================================================
# § 9. 통합: PolicyEngine + ResourceManager 조합
# ===========================================================================


class TestPolicyResourceIntegration:
    def test_passive_save_blocked_by_debounce_not_rejected_by_policy(self) -> None:
        engine = DefaultPolicyEngine()
        mgr = DefaultResourceManager(token_budget=100_000)
        req = _req("passive_save", target="z.py")

        decision = engine.evaluate_request(req)
        assert decision.accepted is True
        assert decision.fast_path_only is True

        # 첫 번째 호출은 통과
        assert mgr.allow_new_job(req) is True
        # 연속 호출은 debounce로 차단
        assert mgr.allow_new_job(req) is False

    def test_active_intent_full_pipeline(self) -> None:
        engine = DefaultPolicyEngine()
        mgr = DefaultResourceManager(token_budget=100_000)
        req = _req("active_intent")

        decision = engine.evaluate_request(req)
        assert decision.accepted is True
        assert decision.fast_path_only is False
        assert mgr.allow_new_job(req) is True
        assert mgr.allow_new_job(req) is True  # 반복 허용

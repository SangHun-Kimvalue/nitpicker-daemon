"""Phase II 구조 개선 테스트.

§1 TriggerAdapter — 입력 계약 통일                          (6 tests)
§2 ArtifactPublisher — 합성 출력 채널                       (5 tests)
§3 Context Provider — DiffProvider + Composite              (5 tests)
§4 trigger_intent SQLite 영속화                             (3 tests)
Total: 19 tests
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jemmin.context.providers import CompositeContextProvider, DiffProvider
from jemmin.context.providers.base import ContextFragment
from jemmin.models import ReviewRequest, ReviewResult, ReviewState
from jemmin.services.artifact_publisher import ArtifactPublisher
from jemmin.services.context_svc import StaticContextService
from jemmin.state.sqlite_spooler import SQLiteJobStore
from jemmin.triggers import (
    CliTriggerAdapter,
    DaemonTriggerAdapter,
    GitHookTriggerAdapter,
    LspTriggerAdapter,
    TriggerEvent,
)


# ---------------------------------------------------------------------------
# §1 TriggerAdapter — 입력 계약 통일
# ---------------------------------------------------------------------------


class TestCliTriggerAdapter:
    def test_builds_request_with_correct_trigger(self):
        adapter = CliTriggerAdapter()
        event = TriggerEvent(source="cli", payload={
            "target_file": "src/main.py",
            "diff_text": "+new line",
        })
        req = adapter.build_request(event)
        assert req.trigger == "cli"
        assert req.trigger_intent == "active_intent"
        assert req.target_file == "src/main.py"

    def test_project_id_consistent(self):
        """CLI와 Daemon 모두 동일한 기본 project_id를 사용해야 합니다."""
        cli_req = CliTriggerAdapter().build_request(
            TriggerEvent(source="cli", payload={"target_file": "a.py", "diff_text": "x"})
        )
        daemon_req = DaemonTriggerAdapter().build_request(
            TriggerEvent(source="daemon", payload={"target_file": "a.py", "diff_text": "x"})
        )
        assert cli_req.project_id == daemon_req.project_id

    def test_idempotency_key_same_for_same_input(self):
        """동일한 target_file + diff_text → 동일한 idempotency_key."""
        adapter = CliTriggerAdapter()
        e1 = TriggerEvent(source="cli", payload={"target_file": "a.py", "diff_text": "diff"})
        e2 = TriggerEvent(source="cli", payload={"target_file": "a.py", "diff_text": "diff"})
        assert adapter.build_request(e1).idempotency_key == adapter.build_request(e2).idempotency_key


class TestDaemonTriggerAdapter:
    def test_supports_daemon_and_zmq(self):
        adapter = DaemonTriggerAdapter()
        assert adapter.supports(TriggerEvent(source="daemon", payload={}))
        assert adapter.supports(TriggerEvent(source="zmq", payload={}))
        assert not adapter.supports(TriggerEvent(source="cli", payload={}))

    def test_overrides_from_payload(self):
        adapter = DaemonTriggerAdapter()
        event = TriggerEvent(source="daemon", payload={
            "target_file": "b.py",
            "diff_text": "d",
            "request_id": "custom-id",
            "trigger_intent": "passive_save",
        })
        req = adapter.build_request(event)
        assert req.request_id == "custom-id"
        assert req.trigger_intent == "passive_save"


class TestLspAndGitHookAdapters:
    def test_lsp_defaults_passive_save(self):
        adapter = LspTriggerAdapter()
        event = TriggerEvent(source="lsp", payload={"target_file": "c.py", "diff_text": ""})
        req = adapter.build_request(event)
        assert req.trigger == "lsp"
        assert req.trigger_intent == "passive_save"

    def test_git_hook_defaults_active_intent(self):
        adapter = GitHookTriggerAdapter()
        event = TriggerEvent(source="git_hook", payload={"target_file": "d.py", "diff_text": ""})
        req = adapter.build_request(event)
        assert req.trigger == "git_hook"
        assert req.trigger_intent == "active_intent"
        assert req.git_revision == "HEAD"


# ---------------------------------------------------------------------------
# §2 ArtifactPublisher — 합성 출력 채널
# ---------------------------------------------------------------------------


class TestArtifactPublisher:
    def _result(self) -> ReviewResult:
        return ReviewResult(
            request_id="pub-test",
            state=ReviewState.DELIVERED,
            status="pass",
            summary="test",
            confidence_score=1.0,
        )

    def test_publish_result_calls_all_channels(self):
        fb = MagicMock()
        logger = MagicMock()
        analytics = MagicMock()
        pub = ArtifactPublisher(
            feedback_service=fb, review_logger=logger, analytics_logger=analytics,
        )
        result = self._result()
        pub.publish_result(result, target_file="a.py", findings=[])
        fb.publish_diagnostics.assert_called_once_with(result, target_file="a.py")
        fb.publish_quick_fix.assert_called_once()
        logger.log_result.assert_called_once_with(result)

    def test_none_services_no_error(self):
        """모든 서비스가 None이어도 오류 없이 동작해야 합니다."""
        pub = ArtifactPublisher()
        pub.publish_result(self._result())
        pub.log_analytics("req-1", event_type="test")
        # no exception

    def test_diagnostics_failure_isolated(self):
        """feedback_service 오류가 다른 채널에 영향을 주지 않아야 합니다."""
        fb = MagicMock()
        fb.publish_diagnostics.side_effect = OSError("disk full")
        logger = MagicMock()
        pub = ArtifactPublisher(feedback_service=fb, review_logger=logger)
        pub.publish_diagnostics(self._result())
        pub.log_result(self._result())
        logger.log_result.assert_called_once()  # 로거는 정상 호출

    def test_analytics_write(self):
        writer = MagicMock()
        pub = ArtifactPublisher(analytics_logger=writer)
        pub.log_analytics("req-1", event_type="test", status="done")
        writer.write.assert_called_once()
        payload = writer.write.call_args[0][0]
        assert payload["event_type"] == "test"
        assert payload["request_id"] == "req-1"

    def test_quick_fix_skipped_without_target_file(self):
        fb = MagicMock()
        pub = ArtifactPublisher(feedback_service=fb)
        pub.publish_result(self._result())  # target_file 미지정
        fb.publish_quick_fix.assert_not_called()


# ---------------------------------------------------------------------------
# §3 Context Provider — DiffProvider + Composite
# ---------------------------------------------------------------------------


class TestDiffProvider:
    def _request(self, diff: str = "") -> ReviewRequest:
        return ReviewRequest(
            request_id="ctx-test",
            idempotency_key="ctx-idem",
            project_id="test",
            project_profile="general",
            trigger="cli",
            target_file="src/main.py",
            diff_text=diff,
        )

    def test_empty_diff_returns_empty_entries(self):
        fragment = DiffProvider().collect(self._request(""))
        assert fragment.tier == "tier1"
        assert fragment.entries == []
        assert fragment.metadata["total_lines"] == 0

    def test_diff_parsed_correctly(self):
        diff = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+new\n context"
        fragment = DiffProvider().collect(self._request(diff))
        assert len(fragment.entries) == 1
        assert fragment.metadata["added_lines"] == 1
        assert fragment.metadata["removed_lines"] == 1
        assert fragment.metadata["target_file"] == "src/main.py"

    def test_composite_merges_providers(self):
        """CompositeContextProvider가 여러 프로바이더를 tier별로 합칩니다."""
        mock_provider = MagicMock()
        mock_provider.collect.return_value = ContextFragment(
            tier="tier2", entries=["symbol data"], metadata={},
        )
        composite = CompositeContextProvider([DiffProvider(), mock_provider])
        tiers = composite.collect(self._request("+added"))
        assert "tier1" in tiers
        assert "tier2" in tiers
        assert "symbol data" in tiers["tier2"]


class TestStaticContextService:
    def _request(self, diff: str = "+line") -> ReviewRequest:
        return ReviewRequest(
            request_id="svc-test",
            idempotency_key="svc-idem",
            project_id="test",
            project_profile="general",
            trigger="cli",
            diff_text=diff,
        )

    def test_build_context_returns_all_tiers(self):
        svc = StaticContextService()
        bundle = svc.build_context(self._request())
        assert "tier1" in bundle.tiers
        assert "tier2" in bundle.tiers
        assert "tier3" in bundle.tiers
        assert "tier4" in bundle.tiers
        assert bundle.token_estimate >= 1

    def test_register_provider_extends_context(self):
        svc = StaticContextService()
        mock_p = MagicMock()
        mock_p.collect.return_value = ContextFragment(
            tier="tier3", entries=["policy rule 1"], metadata={},
        )
        svc.register_provider(mock_p)
        bundle = svc.build_context(self._request())
        assert "policy rule 1" in bundle.tiers["tier3"]


# ---------------------------------------------------------------------------
# §4 trigger_intent SQLite 영속화
# ---------------------------------------------------------------------------


class TestTriggerIntentPersistence:
    def test_trigger_intent_stored_and_recovered(self):
        """trigger_intent가 SQLite에 저장되고 복원되어야 합니다."""
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteJobStore(Path(tmp) / "test.db")
            req = ReviewRequest(
                request_id="ti-1",
                idempotency_key="ti-idem-1",
                project_id="test",
                project_profile="general",
                trigger="lsp",
                trigger_intent="passive_save",
                target_file="x.py",
                diff_text="d",
            )
            store.create_request(req)
            recovered = store.get_request("ti-1")
            assert recovered is not None
            assert recovered.trigger_intent == "passive_save"

    def test_active_intent_default(self):
        """trigger_intent 미지정 시 기본값 active_intent."""
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteJobStore(Path(tmp) / "test.db")
            req = ReviewRequest(
                request_id="ti-2",
                idempotency_key="ti-idem-2",
                project_id="test",
                project_profile="general",
                trigger="cli",
                target_file="y.py",
                diff_text="d",
            )
            store.create_request(req)
            recovered = store.get_request("ti-2")
            assert recovered is not None
            assert recovered.trigger_intent == "active_intent"

    def test_duplicate_create_request_returns_explicit_outcome(self):
        """중복 요청은 조용히 무시되지 않고 명시적 outcome을 반환해야 합니다."""
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteJobStore(Path(tmp) / "test.db")
            first = ReviewRequest(
                request_id="ti-dup-1",
                idempotency_key="dup-key-1",
                project_id="test",
                project_profile="general",
                trigger="cli",
                target_file="same.py",
                diff_text="same diff",
            )
            duplicate = ReviewRequest(
                request_id="ti-dup-2",
                idempotency_key="dup-key-1",
                project_id="test",
                project_profile="general",
                trigger="cli",
                target_file="same.py",
                diff_text="same diff",
            )

            created = store.create_request(first)
            outcome = store.create_request(duplicate)

            assert created.created is True
            assert created.duplicate is False
            assert outcome.created is False
            assert outcome.duplicate is True
            assert outcome.existing_state == "queued"

    def test_migration_adds_column(self):
        """기존 DB에 trigger_intent 컬럼이 없으면 마이그레이션으로 추가됩니다."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "old.db"
            # 옛날 스키마로 DB 생성 (trigger_intent 없음)
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE review_requests (
                    request_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    project_profile TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    target_file TEXT NOT NULL,
                    git_revision TEXT NOT NULL,
                    base_file_hash TEXT NOT NULL,
                    diff_text TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    state TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE review_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """)
            conn.commit()
            conn.close()

            # SQLiteJobStore 초기화 시 마이그레이션이 실행되어야 함
            store = SQLiteJobStore(db_path)

            # 마이그레이션 후 trigger_intent 컬럼 존재 확인
            conn2 = sqlite3.connect(db_path)
            columns = {row[1] for row in conn2.execute("PRAGMA table_info(review_requests)").fetchall()}
            conn2.close()
            assert "trigger_intent" in columns

"""Phase G 테스트: 데몬 & CLI 풀 에이전트 연결 검증.

§1  DaemonBuilder — _build_orchestrator() 가 9개 에이전트·PatchSvc·VerificationSvc·DuckDbLogger를 포함하는지 확인
§2  CliDirectMode — _run_direct() 가 동일 구성으로 실행되는지 확인
§3  FullAgentPipeline — 9개 에이전트가 모두 연결된 오케스트레이터로 리뷰 요청 처리
§4  AnalyticsWiring — DuckDbLogger 연결 시 analytics 이벤트가 기록되는지 확인
"""
from __future__ import annotations

import importlib
import sys
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

from jemmin.agents.architecture import ArchitectureAgent
from jemmin.agents.context_agent import ContextAgent
from jemmin.agents.domain_rule import DomainRuleAgent
from jemmin.agents.fast_gate import FastGateAgent
from jemmin.agents.incident_triage import IncidentTriageAgent
from jemmin.agents.patch_agent import PatchAgent
from jemmin.agents.performance import PerformanceAgent
from jemmin.agents.security import SecurityAgent
from jemmin.agents.verification_agent import VerificationAgent
from jemmin.models import ReviewRequest
from jemmin.orchestrator.consensus import DefaultConsensusEngine
from jemmin.orchestrator.controller import ReviewOrchestrator
from jemmin.orchestrator.policy_engine import DefaultPolicyEngine
from jemmin.orchestrator.resource_mgr import DefaultResourceManager
from jemmin.providers.local_llm import MockLocalLLMProvider
from jemmin.services.context_svc import StaticContextService
from jemmin.services.feedback_svc import FileFeedbackService
from jemmin.services.patch_svc import PatchService
from jemmin.services.review_logger import JsonlReviewLogger
from jemmin.services.verification_svc import VerificationService
from jemmin.state.sqlite_spooler import SQLiteJobStore
from jemmin.utils.duckdb_logger import DuckDbLogger

# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------

_ALL_AGENT_TYPES = (
    FastGateAgent, ContextAgent, DomainRuleAgent,
    ArchitectureAgent, SecurityAgent, PerformanceAgent,
    PatchAgent, VerificationAgent, IncidentTriageAgent,
)


def _make_orchestrator(tmp_path: Path) -> ReviewOrchestrator:
    """테스트용 오케스트레이터 — 9개 에이전트 + 풀 서비스 연결."""
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "patches").mkdir(exist_ok=True)
    provider = MockLocalLLMProvider()
    return ReviewOrchestrator(
        job_store=SQLiteJobStore(tmp_path / "spool.db"),
        policy_engine=DefaultPolicyEngine(max_file_size_bytes=1_048_576),
        resource_manager=DefaultResourceManager(token_budget=200_000),
        context_service=StaticContextService(),
        agents=[
            FastGateAgent(provider=provider),
            ContextAgent(provider=provider),
            DomainRuleAgent(provider=provider),
            ArchitectureAgent(provider=provider),
            SecurityAgent(provider=provider),
            PerformanceAgent(provider=provider),
            PatchAgent(provider=provider),
            VerificationAgent(provider=provider),
            IncidentTriageAgent(provider=provider),
        ],
        consensus_engine=DefaultConsensusEngine(),
        feedback_service=FileFeedbackService(tmp_path / "logs" / "LATEST_REVIEW.txt"),
        review_logger=JsonlReviewLogger(tmp_path / "logs" / "review_history.jsonl"),
        patch_service=PatchService(patches_dir=tmp_path / "patches"),
        verification_service=VerificationService(project_root=ROOT),
        analytics_logger=DuckDbLogger(db_path=":memory:"),
    )


def _req(diff: str = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x=1\n+x=2\n") -> ReviewRequest:
    return ReviewRequest(
        request_id="g-req-001",
        idempotency_key="g-idemp-001",
        project_id="g-proj",
        project_profile="general",
        trigger="cli",
        target_file="foo.py",
        git_revision="HEAD",
        base_file_hash="abcdef1234567890",
        diff_text=diff,
    )


# ===========================================================================
# §1  DaemonBuilder
# ===========================================================================

class TestDaemonBuilder(unittest.TestCase):
    """_build_orchestrator() 이 올바른 구성을 생성하는지 검증."""

    def _load_daemon_module(self):
        """bin/jemmin_daemon.py 를 모듈로 로드 (ZmqRouter 시작 없이)."""
        spec = importlib.util.spec_from_file_location(
            "jemmin_daemon", BIN / "jemmin_daemon.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_build_orchestrator_returns_review_orchestrator(self):
        """_build_orchestrator() 반환 타입이 ReviewOrchestrator 여야 한다."""
        mod = self._load_daemon_module()
        with patch.object(mod, "RUNTIME_DIR", Path(self._tmpdir())):
            orch = mod._build_orchestrator()
        self.assertIsInstance(orch, ReviewOrchestrator)

    def test_daemon_has_nine_agents(self):
        """데몬 빌더가 정확히 9개 에이전트를 AgentRegistry에 등록한다."""
        mod = self._load_daemon_module()
        with patch.object(mod, "RUNTIME_DIR", Path(self._tmpdir())):
            orch = mod._build_orchestrator()
        # Phase III: agents는 이제 agent_registry를 통해 관리됨
        registry = orch._agent_registry
        self.assertIsNotNone(registry, "agent_registry가 연결되어 있어야 함")
        all_agents = registry.select(project_profile="general", trigger_intent="active_intent")
        self.assertEqual(len(all_agents), 10)  # 9 + AstSecurityAnalyzer

    def test_daemon_agents_are_correct_types(self):
        """데몬 에이전트 목록이 기존 9종 모두 포함한다."""
        mod = self._load_daemon_module()
        with patch.object(mod, "RUNTIME_DIR", Path(self._tmpdir())):
            orch = mod._build_orchestrator()
        registry = orch._agent_registry
        all_agents = registry.select(project_profile="general", trigger_intent="active_intent")
        agent_types = {type(a) for a in all_agents}
        for expected in _ALL_AGENT_TYPES:
            self.assertIn(expected, agent_types, f"{expected.__name__} 가 누락됨")

    def test_daemon_patch_service_wired(self):
        """데몬에 PatchService 가 연결되어 있어야 한다."""
        mod = self._load_daemon_module()
        with patch.object(mod, "RUNTIME_DIR", Path(self._tmpdir())):
            orch = mod._build_orchestrator()
        self.assertIsInstance(orch._patch_service, PatchService)

    def test_daemon_verification_service_wired(self):
        """데몬에 VerificationService 가 연결되어 있어야 한다."""
        mod = self._load_daemon_module()
        with patch.object(mod, "RUNTIME_DIR", Path(self._tmpdir())):
            orch = mod._build_orchestrator()
        self.assertIsInstance(orch._verification_service, VerificationService)

    def test_daemon_analytics_logger_wired(self):
        """데몬에 DuckDbLogger 가 연결되어 있어야 한다."""
        mod = self._load_daemon_module()
        with patch.object(mod, "RUNTIME_DIR", Path(self._tmpdir())):
            orch = mod._build_orchestrator()
        self.assertIsInstance(orch._analytics_logger, DuckDbLogger)

    def _tmpdir(self):
        import tempfile
        d = tempfile.mkdtemp()
        # 하위 디렉터리 사전 생성
        Path(d).mkdir(parents=True, exist_ok=True)
        return d


# ===========================================================================
# §2  CLI Direct Mode
# ===========================================================================

class TestCliDirectMode(unittest.TestCase):
    """jemmin_cli._run_direct() 가 9개 에이전트로 실행되는지 검증."""

    def _load_cli_module(self):
        spec = importlib.util.spec_from_file_location(
            "jemmin_cli", BIN / "jemmin_cli.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_cli_run_direct_returns_zero(self):
        """_run_direct() 가 정상 완료 시 0 을 반환한다."""
        mod = self._load_cli_module()
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        with patch.object(mod, "ROOT", tmp):
            code = mod._run_direct("foo.py", "diff --git a/foo.py b/foo.py\n")
        self.assertEqual(code, 0)

    def test_cli_direct_creates_review_artifacts(self):
        """_run_direct() 실행 후 logs/ 디렉터리가 생성된다."""
        mod = self._load_cli_module()
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        with patch.object(mod, "ROOT", tmp):
            mod._run_direct("foo.py", "diff --git a/foo.py b/foo.py\n")
        self.assertTrue((tmp / ".jemmin" / "logs").is_dir())

    def test_cli_direct_patches_dir_created(self):
        """_run_direct() 실행 후 patches/ 디렉터리가 생성된다."""
        mod = self._load_cli_module()
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        with patch.object(mod, "ROOT", tmp):
            mod._run_direct("foo.py", "diff --git a/foo.py b/foo.py\n")
        self.assertTrue((tmp / ".jemmin" / "patches").is_dir())


# ===========================================================================
# §3  FullAgentPipeline
# ===========================================================================

class TestFullAgentPipeline(unittest.TestCase):
    """9개 에이전트가 연결된 오케스트레이터로 리뷰 요청을 처리하는 통합 테스트."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.orch = _make_orchestrator(self.tmp)

    def test_run_once_returns_review_result(self):
        """run_once() 는 ReviewResult 를 반환한다."""
        from jemmin.models import ReviewResult
        result = self.orch.run_once(_req())
        self.assertIsInstance(result, ReviewResult)

    def test_run_once_result_has_request_id(self):
        """결과의 request_id 가 요청과 일치해야 한다."""
        result = self.orch.run_once(_req())
        self.assertEqual(result.request_id, "g-req-001")

    def test_run_once_result_code_is_nonempty(self):
        """result_code 가 비어 있지 않아야 한다."""
        result = self.orch.run_once(_req())
        self.assertTrue(result.result_code)

    def test_run_once_with_empty_diff(self):
        """빈 diff 로도 오케스트레이터가 정상 완료된다."""
        result = self.orch.run_once(_req(diff=""))
        self.assertIsNotNone(result.state)

    def test_all_agent_types_registered(self):
        """오케스트레이터 내부 에이전트 목록이 9종을 모두 포함한다."""
        agent_types = {type(a) for a in self.orch._agents}
        for expected in _ALL_AGENT_TYPES:
            self.assertIn(expected, agent_types, f"{expected.__name__} 누락")

    def test_patch_service_accessible(self):
        """PatchService 가 None 이 아니어야 한다."""
        self.assertIsNotNone(self.orch._patch_service)

    def test_verification_service_accessible(self):
        """VerificationService 가 None 이 아니어야 한다."""
        self.assertIsNotNone(self.orch._verification_service)

    def test_analytics_logger_accessible(self):
        """DuckDbLogger 가 None 이 아니어야 한다."""
        self.assertIsNotNone(self.orch._analytics_logger)


# ===========================================================================
# §4  AnalyticsWiring
# ===========================================================================

class TestAnalyticsWiring(unittest.TestCase):
    """DuckDbLogger 가 연결됐을 때 이벤트가 실제로 기록되는지 확인."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def test_analytics_logger_write_called_during_run(self):
        """run_once() 실행 시 DuckDbLogger.write() 가 한 번 이상 호출된다."""
        mock_logger = MagicMock(spec=DuckDbLogger)
        (self.tmp / "logs").mkdir(parents=True, exist_ok=True)
        (self.tmp / "patches").mkdir(exist_ok=True)
        provider = MockLocalLLMProvider()
        orch = ReviewOrchestrator(
            job_store=SQLiteJobStore(self.tmp / "spool.db"),
            policy_engine=DefaultPolicyEngine(max_file_size_bytes=1_048_576),
            resource_manager=DefaultResourceManager(token_budget=200_000),
            context_service=StaticContextService(),
            agents=[FastGateAgent(provider=provider)],
            consensus_engine=DefaultConsensusEngine(),
            feedback_service=FileFeedbackService(self.tmp / "logs" / "LATEST_REVIEW.txt"),
            review_logger=JsonlReviewLogger(self.tmp / "logs" / "review_history.jsonl"),
            analytics_logger=mock_logger,
        )
        orch.run_once(_req())
        self.assertTrue(mock_logger.write.called, "DuckDbLogger.write() 가 호출되지 않음")

    def test_analytics_events_contain_request_id(self):
        """기록된 이벤트에 request_id 필드가 있어야 한다."""
        recorded: list[dict] = []
        mock_logger = MagicMock(spec=DuckDbLogger)
        mock_logger.write.side_effect = lambda d: recorded.append(d)

        (self.tmp / "logs").mkdir(parents=True, exist_ok=True)
        (self.tmp / "patches").mkdir(exist_ok=True)
        provider = MockLocalLLMProvider()
        orch = ReviewOrchestrator(
            job_store=SQLiteJobStore(self.tmp / "spool.db"),
            policy_engine=DefaultPolicyEngine(max_file_size_bytes=1_048_576),
            resource_manager=DefaultResourceManager(token_budget=200_000),
            context_service=StaticContextService(),
            agents=[FastGateAgent(provider=provider)],
            consensus_engine=DefaultConsensusEngine(),
            feedback_service=FileFeedbackService(self.tmp / "logs" / "LATEST_REVIEW.txt"),
            review_logger=JsonlReviewLogger(self.tmp / "logs" / "review_history.jsonl"),
            analytics_logger=mock_logger,
        )
        req = _req()
        orch.run_once(req)
        self.assertTrue(any(d.get("request_id") == req.request_id for d in recorded))

    def test_analytics_broken_logger_does_not_crash_pipeline(self):
        """DuckDbLogger.write() 가 예외를 던져도 파이프라인이 계속 실행된다."""
        mock_logger = MagicMock(spec=DuckDbLogger)
        mock_logger.write.side_effect = RuntimeError("DB 오류 시뮬레이션")

        (self.tmp / "logs").mkdir(parents=True, exist_ok=True)
        (self.tmp / "patches").mkdir(exist_ok=True)
        provider = MockLocalLLMProvider()
        orch = ReviewOrchestrator(
            job_store=SQLiteJobStore(self.tmp / "spool.db"),
            policy_engine=DefaultPolicyEngine(max_file_size_bytes=1_048_576),
            resource_manager=DefaultResourceManager(token_budget=200_000),
            context_service=StaticContextService(),
            agents=[FastGateAgent(provider=provider)],
            consensus_engine=DefaultConsensusEngine(),
            feedback_service=FileFeedbackService(self.tmp / "logs" / "LATEST_REVIEW.txt"),
            review_logger=JsonlReviewLogger(self.tmp / "logs" / "review_history.jsonl"),
            analytics_logger=mock_logger,
        )
        # 예외 없이 정상 완료되어야 한다
        result = orch.run_once(_req())
        self.assertIsNotNone(result.state)

    def test_real_duckdb_logger_records_events(self):
        """실제 DuckDbLogger(:memory:) 로 이벤트가 저장되는지 확인한다."""
        analytics = DuckDbLogger(db_path=":memory:")
        (self.tmp / "logs").mkdir(parents=True, exist_ok=True)
        (self.tmp / "patches").mkdir(exist_ok=True)
        provider = MockLocalLLMProvider()
        orch = ReviewOrchestrator(
            job_store=SQLiteJobStore(self.tmp / "spool.db"),
            policy_engine=DefaultPolicyEngine(max_file_size_bytes=1_048_576),
            resource_manager=DefaultResourceManager(token_budget=200_000),
            context_service=StaticContextService(),
            agents=[FastGateAgent(provider=provider)],
            consensus_engine=DefaultConsensusEngine(),
            feedback_service=FileFeedbackService(self.tmp / "logs" / "LATEST_REVIEW.txt"),
            review_logger=JsonlReviewLogger(self.tmp / "logs" / "review_history.jsonl"),
            analytics_logger=analytics,
        )
        orch.run_once(_req())
        rows = analytics.query("SELECT COUNT(*) AS cnt FROM review_events")
        self.assertGreater(rows[0]["cnt"], 0, "review_events 테이블에 레코드가 없음")

    def test_analytics_pipeline_complete_event_exists(self):
        """pipeline_complete 이벤트가 기록되어야 한다."""
        analytics = DuckDbLogger(db_path=":memory:")
        (self.tmp / "logs").mkdir(parents=True, exist_ok=True)
        (self.tmp / "patches").mkdir(exist_ok=True)
        provider = MockLocalLLMProvider()
        orch = ReviewOrchestrator(
            job_store=SQLiteJobStore(self.tmp / "spool.db"),
            policy_engine=DefaultPolicyEngine(max_file_size_bytes=1_048_576),
            resource_manager=DefaultResourceManager(token_budget=200_000),
            context_service=StaticContextService(),
            agents=[FastGateAgent(provider=provider)],
            consensus_engine=DefaultConsensusEngine(),
            feedback_service=FileFeedbackService(self.tmp / "logs" / "LATEST_REVIEW.txt"),
            review_logger=JsonlReviewLogger(self.tmp / "logs" / "review_history.jsonl"),
            analytics_logger=analytics,
        )
        orch.run_once(_req())
        rows = analytics.query(
            "SELECT COUNT(*) AS cnt FROM review_events WHERE event_type = 'pipeline_complete'"
        )
        self.assertGreater(rows[0]["cnt"], 0, "pipeline_complete 이벤트가 없음")


# ===========================================================================
# §5  Redaction — 실제 패턴 기반 시크릿 마스킹
# ===========================================================================

class TestRedaction(unittest.TestCase):
    """redaction.py 의 실제 패턴이 민감 정보를 올바르게 마스킹하는지 확인."""

    def setUp(self):
        from jemmin.utils.redaction import redact_secrets
        self.redact = redact_secrets

    def test_aws_access_key_redacted(self):
        """AWS Access Key (AKIA...) 를 마스킹한다."""
        text = "access_key_id = AKIAIOSFODNN7EXAMPLE"
        result = self.redact(text)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", result)
        self.assertIn("[REDACTED]", result)

    def test_password_value_redacted_key_preserved(self):
        """password = value 에서 값만 마스킹하고 키 이름은 유지한다."""
        text = "password = mysecretpass123"
        result = self.redact(text)
        self.assertIn("password", result)
        self.assertNotIn("mysecretpass123", result)
        self.assertIn("[REDACTED]", result)

    def test_secret_key_redacted(self):
        """secret_key = ... 패턴을 마스킹한다."""
        text = "secret_key=AbCdEfGhIjKl1234"
        result = self.redact(text)
        self.assertNotIn("AbCdEfGhIjKl1234", result)
        self.assertIn("[REDACTED]", result)

    def test_token_redacted(self):
        """token = ... 패턴을 마스킹한다."""
        text = "token: ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        result = self.redact(text)
        self.assertNotIn("ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxx", result)
        self.assertIn("[REDACTED]", result)

    def test_bearer_token_redacted(self):
        """Authorization: Bearer <token> 패턴을 마스킹한다."""
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = self.redact(text)
        self.assertIn("Bearer", result)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", result)
        self.assertIn("[REDACTED]", result)

    def test_private_key_block_redacted(self):
        """PEM 형식 Private Key 블록 전체를 마스킹한다."""
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA0Z3VS5JJcds3xHn...\n"
            "-----END RSA PRIVATE KEY-----"
        )
        result = self.redact(text)
        self.assertNotIn("MIIEowIBAAKCAQEA", result)
        self.assertIn("[REDACTED_PRIVATE_KEY]", result)

    def test_db_url_password_redacted(self):
        """DB URL의 비밀번호 부분을 마스킹한다."""
        text = "postgresql://admin:supersecret@localhost:5432/mydb"
        result = self.redact(text)
        self.assertNotIn("supersecret", result)
        self.assertIn("postgresql://", result)
        self.assertIn("@localhost", result)
        self.assertIn("[REDACTED]", result)

    def test_safe_text_unchanged(self):
        """민감 정보가 없는 일반 텍스트는 변경하지 않는다."""
        text = "def calculate_total(items: list[int]) -> int:\n    return sum(items)"
        result = self.redact(text)
        self.assertEqual(text, result)

    def test_multiple_secrets_all_redacted(self):
        """한 텍스트에 여러 시크릿이 있으면 모두 마스킹한다."""
        text = (
            "api_key=myapikey12345678\n"
            "password=mypassword!\n"
        )
        result = self.redact(text)
        self.assertNotIn("myapikey12345678", result)
        self.assertNotIn("mypassword!", result)


# ===========================================================================
# §6  PatchService git apply --check
# ===========================================================================

class TestPatchServiceGitCheck(unittest.TestCase):
    """PatchService 의 git apply --check 동작을 검증한다."""

    def _svc(self, tmp: Path, project_root=None) -> "PatchService":
        from jemmin.services.patch_svc import PatchService
        return PatchService(
            patches_dir=tmp / "patches",
            project_root=project_root,
        )

    def _consensus(self, diff: str | None = None) -> "ConsensusResult":
        from jemmin.models import ConsensusResult
        summary = diff or "```diff\n--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x=1\n+x=2\n```"
        return ConsensusResult(
            status="patch",
            summary=summary,
            confidence_score=0.9,
            winning_reasons=[],
            conflicting_agents=[],
        )

    def _req(self) -> "ReviewRequest":
        from jemmin.models import ReviewRequest
        return ReviewRequest(
            request_id="g-patch-001",
            idempotency_key="g-idemp-001",
            project_id="g-proj",
            project_profile="general",
            trigger="cli",
            target_file="foo.py",
            git_revision="HEAD",
            base_file_hash="abc123",
            diff_text="",
        )

    def test_no_project_root_skips_check(self):
        """project_root 없이 생성 시 git check 건너뛰고 패치 반환."""
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        svc = self._svc(tmp, project_root=None)
        proposal = svc.create_patch(self._req(), self._consensus())
        # git check 없이 패치가 정상 반환되어야 한다
        self.assertIsNotNone(proposal)

    def test_git_not_found_still_returns_patch(self):
        """git 바이너리가 없어도 (FileNotFoundError) 패치를 반환한다 (Fail-Open)."""
        import tempfile
        from unittest.mock import patch as mock_patch
        tmp = Path(tempfile.mkdtemp())
        svc = self._svc(tmp, project_root=tmp)
        with mock_patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            proposal = svc.create_patch(self._req(), self._consensus())
        self.assertIsNotNone(proposal)

    def test_git_apply_check_failure_returns_none(self):
        """git apply --check 실패(returncode != 0) 시 None 반환."""
        import subprocess
        import tempfile
        from unittest.mock import MagicMock, patch as mock_patch
        tmp = Path(tempfile.mkdtemp())
        svc = self._svc(tmp, project_root=tmp)

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "error: patch failed: foo.py:1"
        mock_result.stdout = ""
        with mock_patch("subprocess.run", return_value=mock_result):
            proposal = svc.create_patch(self._req(), self._consensus())
        self.assertIsNone(proposal)

    def test_git_apply_check_success_returns_proposal(self):
        """git apply --check 성공(returncode == 0) 시 PatchProposal 반환."""
        import tempfile
        from unittest.mock import MagicMock, patch as mock_patch
        tmp = Path(tempfile.mkdtemp())
        svc = self._svc(tmp, project_root=tmp)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""
        mock_result.stdout = ""
        with mock_patch("subprocess.run", return_value=mock_result):
            proposal = svc.create_patch(self._req(), self._consensus())
        self.assertIsNotNone(proposal)
        self.assertIsNotNone(proposal.patch_hash)

    def test_timeout_still_returns_patch(self):
        """git apply --check 타임아웃 시 Fail-Open으로 패치 반환."""
        import subprocess
        import tempfile
        from unittest.mock import patch as mock_patch
        tmp = Path(tempfile.mkdtemp())
        svc = self._svc(tmp, project_root=tmp)
        with mock_patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 10)):
            proposal = svc.create_patch(self._req(), self._consensus())
        self.assertIsNotNone(proposal)

    def test_no_diff_in_consensus_returns_none(self):
        """consensus.summary에 diff가 없으면 None 반환 (기존 동작 유지)."""
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        svc = self._svc(tmp, project_root=None)
        consensus = self._consensus(diff="이 패치에는 diff 블록이 없습니다.")
        proposal = svc.create_patch(self._req(), consensus)
        self.assertIsNone(proposal)


# ===========================================================================
# §7  trigger_intent & --stats CLI
# ===========================================================================

class TestTriggerIntentAndStats(unittest.TestCase):
    """daemon _build_request 의 trigger_intent 반영 및 CLI --stats 동작 검증."""

    def _load_daemon_module(self):
        spec = importlib.util.spec_from_file_location(
            "jemmin_daemon", BIN / "jemmin_daemon.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _load_cli_module(self):
        spec = importlib.util.spec_from_file_location(
            "jemmin_cli", BIN / "jemmin_cli.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_daemon_build_request_defaults_active_intent(self):
        """payload에 trigger_intent 없으면 active_intent 가 기본값이다."""
        mod = self._load_daemon_module()
        req = mod._build_request({"target_file": "foo.py", "diff_text": "+"})
        self.assertEqual(req.trigger_intent, "active_intent")

    def test_daemon_build_request_passive_save_propagated(self):
        """payload의 trigger_intent=passive_save 가 ReviewRequest에 반영된다."""
        mod = self._load_daemon_module()
        req = mod._build_request({
            "target_file": "foo.py",
            "diff_text": "+x=1",
            "trigger_intent": "passive_save",
        })
        self.assertEqual(req.trigger_intent, "passive_save")

    def test_daemon_build_request_active_intent_propagated(self):
        """payload의 trigger_intent=active_intent 가 ReviewRequest에 반영된다."""
        mod = self._load_daemon_module()
        req = mod._build_request({
            "target_file": "foo.py",
            "diff_text": "+x=1",
            "trigger_intent": "active_intent",
        })
        self.assertEqual(req.trigger_intent, "active_intent")

    def test_cli_stats_returns_one_when_no_db(self):
        """analytics.duckdb 파일이 없으면 _show_stats() 가 1 을 반환한다."""
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        mod = self._load_cli_module()
        with patch.object(mod, "ROOT", tmp):
            code = mod._show_stats()
        self.assertEqual(code, 1)

    def test_cli_stats_returns_zero_when_db_exists_no_events(self):
        """analytics.duckdb 는 존재하지만 이벤트가 없으면 0 을 반환한다."""
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        (tmp / ".jemmin").mkdir()
        # 빈 DuckDB 파일 생성
        DuckDbLogger(db_path=tmp / ".jemmin" / "analytics.duckdb").close()
        mod = self._load_cli_module()
        with patch.object(mod, "ROOT", tmp):
            code = mod._show_stats()
        self.assertEqual(code, 0)

    def test_cli_stats_returns_zero_with_events(self):
        """이벤트가 기록된 analytics.duckdb 가 있으면 0 을 반환하고 요약을 출력한다."""
        import io
        import tempfile
        from contextlib import redirect_stdout
        tmp = Path(tempfile.mkdtemp())
        (tmp / ".jemmin").mkdir()
        db_path = tmp / ".jemmin" / "analytics.duckdb"
        logger = DuckDbLogger(db_path=db_path)
        logger.write({
            "request_id": "test-001",
            "event_type": "pipeline_complete",
            "status": "pass",
            "result_code": "REVIEW_PASSED",
        })
        logger.close()

        mod = self._load_cli_module()
        buf = io.StringIO()
        with patch.object(mod, "ROOT", tmp), redirect_stdout(buf):
            code = mod._show_stats()
        self.assertEqual(code, 0)
        output = buf.getvalue()
        self.assertIn("pipeline_complete", output)

    def test_daemon_patch_service_has_project_root(self):
        """데몬 PatchService 에 project_root 가 설정되어 있어야 한다."""
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        mod = self._load_daemon_module()
        with patch.object(mod, "RUNTIME_DIR", Path(tmp)):
            orch = mod._build_orchestrator()
        self.assertIsNotNone(orch._patch_service._project_root)


if __name__ == "__main__":
    unittest.main()

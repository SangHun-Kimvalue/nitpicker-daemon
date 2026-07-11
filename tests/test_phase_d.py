"""Phase D 테스트: Performance/Context/DomainRule/IncidentTriage/Patch/Verification 에이전트
+ PatchService, VerificationService, ConsensusEngine(개선).
"""
from __future__ import annotations

import subprocess
import textwrap
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jemmin.agents.context_agent import ContextAgent
from jemmin.agents.domain_rule import DomainRuleAgent
from jemmin.agents.incident_triage import IncidentTriageAgent
from jemmin.agents.patch_agent import PatchAgent
from jemmin.agents.performance import PerformanceAgent, _scan_diff as perf_scan
from jemmin.agents.verification_agent import VerificationAgent
from jemmin.models import AgentDecision, ConsensusResult, ContextBundle, ReviewRequest
from jemmin.orchestrator.consensus import DefaultConsensusEngine
from jemmin.services.patch_svc import PatchService, PatchProposal
from jemmin.services.verification_svc import VerificationService

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _req(diff: str = "", profile: str = "general", target: str = "") -> ReviewRequest:
    return ReviewRequest(
        request_id="d-req",
        idempotency_key="d-idem",
        project_id="d-proj",
        project_profile=profile,
        trigger="cli",
        target_file=target,
        git_revision="HEAD",
        base_file_hash="abc",
        diff_text=diff,
    )


def _ctx(metadata: dict | None = None) -> ContextBundle:
    return ContextBundle(
        request_id="d-req",
        context_hash="hash",
        token_estimate=10,
        tiers={},
        metadata=metadata or {},
    )


def _decision(name: str, status: str, confidence: float = 0.8) -> AgentDecision:
    return AgentDecision(
        agent_name=name,
        status=status,  # type: ignore[arg-type]
        confidence_score=confidence,
        findings=[],
        suggested_actions=[],
    )


def _diff(added: str) -> str:
    return "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n" + "\n".join(
        f"+{line}" for line in added.splitlines()
    )


# ---------------------------------------------------------------------------
# PerformanceAgent
# ---------------------------------------------------------------------------


class TestPerformanceAgent:
    def test_pass_on_clean(self):
        d = PerformanceAgent().run(_req("+x = 1\n"), _ctx())
        assert d.status == "pass"

    def test_warn_on_deepcopy(self):
        d = PerformanceAgent().run(_req(_diff("import copy\ncopy.deepcopy(obj)")), _ctx())
        assert d.status == "warn"
        assert any(f["code"] == "PERF003" for f in d.findings)

    def test_warn_on_time_sleep(self):
        d = PerformanceAgent().run(_req(_diff("import time\ntime.sleep(1)")), _ctx())
        assert d.status == "warn"
        assert any(f["code"] == "PERF008" for f in d.findings)

    def test_warn_on_triple_nested_listcomp(self):
        d = PerformanceAgent().run(
            _req(_diff("[x for a in b for c in d for x in e]")), _ctx()
        )
        assert any(f["code"] == "PERF007" for f in d.findings)

    def test_agent_name(self):
        assert PerformanceAgent().name == "performance"


# ---------------------------------------------------------------------------
# ContextAgent
# ---------------------------------------------------------------------------


class TestContextAgent:
    def test_pass_on_small_clean_diff(self):
        d = ContextAgent().run(_req(_diff("x = 1")), _ctx())
        assert d.status == "pass"

    def test_warn_on_empty_diff(self):
        d = ContextAgent().run(_req(""), _ctx())
        assert any(f["code"] == "CTX005" for f in d.findings)

    def test_warn_on_oversized_diff(self):
        big_diff = _diff("\n".join(f"x_{i} = {i}" for i in range(500)))
        d = ContextAgent().run(_req(big_diff), _ctx())
        assert any(f["code"] == "CTX001" for f in d.findings)

    def test_warn_new_def_without_test(self):
        # CTX003은 multi-file diff에서만 발생 (단일 파일은 테스트가 별도이므로 오탐 방지)
        code = "def my_new_function(x):\n    return x * 2"
        multi_diff = (
            "diff --git a/f.py b/f.py\n"
            "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n"
            + "\n".join(f"+{line}" for line in code.splitlines())
            + "\ndiff --git a/g.py b/g.py\n"
            "--- a/g.py\n+++ b/g.py\n@@ -1 +1 @@\n+x = 1"
        )
        d = ContextAgent().run(_req(multi_diff), _ctx())
        assert any(f["code"] == "CTX003" for f in d.findings)

    def test_no_warn_new_def_single_file(self):
        """단일 파일 diff에서는 CTX003이 발생하지 않아야 합니다."""
        code = "def my_new_function(x):\n    return x * 2"
        d = ContextAgent().run(_req(_diff(code)), _ctx())
        assert not any(f["code"] == "CTX003" for f in d.findings)

    def test_no_warn_when_test_present(self):
        code = "def my_fn(x):\n    return x\ndef test_my_fn():\n    assert my_fn(1) == 1"
        d = ContextAgent().run(_req(_diff(code)), _ctx())
        assert not any(f["code"] == "CTX003" for f in d.findings)

    def test_warn_on_todo_marker(self):
        d = ContextAgent().run(_req(_diff("# TODO: fix this later")), _ctx())
        assert any(f["code"] == "CTX004" for f in d.findings)

    def test_agent_name(self):
        assert ContextAgent().name == "context"


# ---------------------------------------------------------------------------
# DomainRuleAgent
# ---------------------------------------------------------------------------


class TestDomainRuleAgent:
    def test_pass_on_clean(self):
        d = DomainRuleAgent().run(_req(_diff("x = 1"), profile="general"), _ctx())
        assert d.status == "pass"

    def test_dom001_magic_number(self):
        d = DomainRuleAgent().run(_req(_diff("timeout = 9999"), profile="general"), _ctx())
        assert any(f["code"] == "DOM001" for f in d.findings)

    def test_dom101_api_route_no_auth(self):
        code = "@app.get('/users')\ndef list_users():\n    return []\n"
        d = DomainRuleAgent().run(_req(_diff(code), profile="api"), _ctx())
        assert any(f["code"] == "DOM101" for f in d.findings)

    def test_dom201_iterrows(self):
        d = DomainRuleAgent().run(
            _req(_diff("for idx, row in df.iterrows():"), profile="data"), _ctx()
        )
        assert any(f["code"] == "DOM201" for f in d.findings)

    def test_dom301_sys_exit(self):
        d = DomainRuleAgent().run(
            _req(_diff("sys.exit(1)"), profile="cli"), _ctx()
        )
        assert any(f["code"] == "DOM301" for f in d.findings)

    def test_general_rules_applied_to_api_profile(self):
        """api 프로파일에도 general 규칙이 적용돼야 한다."""
        d = DomainRuleAgent().run(_req(_diff("timeout = 99999"), profile="api"), _ctx())
        assert any(f["code"] == "DOM001" for f in d.findings)

    def test_dom001_ignores_named_constants(self):
        d = DomainRuleAgent().run(_req(_diff("MAX_TIMEOUT_MS = 9999"), profile="general"), _ctx())
        assert not any(f["code"] == "DOM001" for f in d.findings)

    def test_agent_name(self):
        assert DomainRuleAgent().name == "domain_rule"


# ---------------------------------------------------------------------------
# IncidentTriageAgent
# ---------------------------------------------------------------------------


class TestIncidentTriageAgent:
    def test_pass_on_clean(self):
        d = IncidentTriageAgent().run(_req(_diff("x = 1")), _ctx())
        assert d.status == "pass"

    def test_noqa_comment(self):
        d = IncidentTriageAgent().run(_req(_diff("x = 1  # noqa")), _ctx())
        assert any(f["code"] == "INC003" for f in d.findings)

    def test_type_ignore_comment(self):
        d = IncidentTriageAgent().run(_req(_diff("x = func()  # type: ignore")), _ctx())
        assert any(f["code"] == "INC003" for f in d.findings)

    def test_return_not_implemented(self):
        d = IncidentTriageAgent().run(_req(_diff("return NotImplemented")), _ctx())
        assert any(f["code"] == "INC005" for f in d.findings)

    def test_hardcoded_url(self):
        d = IncidentTriageAgent().run(
            _req(_diff('url = "https://api.example.com/v1/users"')), _ctx()
        )
        assert any(f["code"] == "INC006" for f in d.findings)

    def test_agent_name(self):
        assert IncidentTriageAgent().name == "incident_triage"


# ---------------------------------------------------------------------------
# PatchAgent
# ---------------------------------------------------------------------------


class TestPatchAgentRun:
    def test_pass_when_no_proposal(self):
        d = PatchAgent().run(_req(), _ctx())
        assert d.status == "pass"
        assert d.confidence_score == 1.0

    def test_warn_on_invalid_diff(self):
        ctx = _ctx({"patch_proposal": "not a unified diff"})
        d = PatchAgent().run(_req(), ctx)
        assert d.status == "warn"
        assert any(f["code"] == "PATCH001" for f in d.findings)

    def test_pass_on_valid_diff(self):
        valid = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        ctx = _ctx({"patch_proposal": valid})
        d = PatchAgent().run(_req(), ctx)
        assert d.status == "pass"
        assert any(f["code"] == "PATCH_OK" for f in d.findings)

    def test_agent_name(self):
        assert PatchAgent().name == "patch"


# ---------------------------------------------------------------------------
# VerificationAgent
# ---------------------------------------------------------------------------


class TestVerificationAgentRun:
    def test_pass_through_when_flag_off(self):
        d = VerificationAgent().run(_req(), _ctx())
        assert d.status == "pass"
        assert d.confidence_score == 1.0

    def test_pass_on_pytest_success(self, tmp_path):
        ctx = _ctx({"run_verification": True, "project_root": str(tmp_path)})
        with patch("jemmin.agents.verification_agent._run_pytest", return_value=(True, "1 passed")):
            d = VerificationAgent().run(_req(), ctx)
        assert d.status == "pass"

    def test_reject_on_pytest_failure(self, tmp_path):
        ctx = _ctx({"run_verification": True, "project_root": str(tmp_path)})
        with patch("jemmin.agents.verification_agent._run_pytest", return_value=(False, "1 failed")):
            d = VerificationAgent().run(_req(), ctx)
        assert d.status == "reject"
        assert any(f["code"] == "VER002" for f in d.findings)

    def test_warn_on_timeout(self, tmp_path):
        ctx = _ctx({"run_verification": True, "project_root": str(tmp_path)})
        with patch(
            "jemmin.agents.verification_agent._run_pytest",
            side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=60),
        ):
            d = VerificationAgent().run(_req(), ctx)
        assert d.status == "warn"
        assert any(f["code"] == "VER001" for f in d.findings)

    def test_agent_name(self):
        assert VerificationAgent().name == "verification"


# ---------------------------------------------------------------------------
# PatchService
# ---------------------------------------------------------------------------


class TestPatchService:
    def test_returns_none_when_no_diff(self):
        svc = PatchService()
        result = svc.create_patch(_req(), ConsensusResult(
            status="reject", summary="no diff here", confidence_score=0.9,
            winning_reasons=[], conflicting_agents=[],
        ))
        assert result is None

    def test_extracts_diff_from_markdown_block(self, tmp_path):
        patch_dir = tmp_path / "patches"
        svc = PatchService(patches_dir=patch_dir)
        consensus = ConsensusResult(
            status="patch",
            summary="fix:\n```diff\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-x=1\n+x=2\n```",
            confidence_score=0.8,
            winning_reasons=[],
            conflicting_agents=[],
        )
        proposal = svc.create_patch(_req(target="/some/f.py"), consensus)
        assert proposal is not None
        assert "--- a/f.py" in proposal.unified_diff
        assert len(proposal.patch_hash) == 16
        assert patch_dir.exists()
        assert (patch_dir / f"{proposal.patch_hash}.patch").exists()

    def test_extracts_raw_unified_diff(self):
        svc = PatchService()
        raw = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-x=1\n+x=2\n"
        consensus = ConsensusResult(
            status="patch", summary=raw, confidence_score=0.8,
            winning_reasons=[], conflicting_agents=[],
        )
        proposal = svc.create_patch(_req(), consensus)
        assert proposal is not None
        assert proposal.unified_diff == raw.strip()


# ---------------------------------------------------------------------------
# VerificationService
# ---------------------------------------------------------------------------


class TestVerificationService:
    def _proposal(self) -> PatchProposal:
        return PatchProposal(
            patch_hash="abc123def456abcd",
            unified_diff="--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n",
            source_file="f.py",
            saved_path=None,
        )

    def test_passed_report(self, tmp_path):
        svc = VerificationService(project_root=tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="1 passed", stderr=""
            )
            report = svc.verify_patch(_req(), self._proposal())
        assert report.passed is True
        assert report.patch_hash == "abc123def456abcd"

    def test_failed_report(self, tmp_path):
        svc = VerificationService(project_root=tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="1 failed", stderr=""
            )
            report = svc.verify_patch(_req(), self._proposal())
        assert report.passed is False
        assert report.returncode == 1

    def test_timeout_report(self, tmp_path):
        svc = VerificationService(project_root=tmp_path, timeout=1)
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=1),
        ):
            report = svc.verify_patch(_req(), self._proposal())
        assert report.passed is False
        assert report.returncode == -1


# ---------------------------------------------------------------------------
# DefaultConsensusEngine (개선)
# ---------------------------------------------------------------------------


class TestDefaultConsensusEngine:
    def _engine(self) -> DefaultConsensusEngine:
        return DefaultConsensusEngine()

    def test_all_pass(self):
        decisions = [_decision("a", "pass"), _decision("b", "pass")]
        r = self._engine().decide(decisions)
        assert r.status == "pass"

    def test_one_reject(self):
        decisions = [_decision("a", "pass"), _decision("b", "reject", 0.95)]
        r = self._engine().decide(decisions)
        assert r.status == "reject"
        assert "b" in r.winning_reasons

    def test_two_warns_remain_advisory_pass(self):
        decisions = [_decision("a", "warn"), _decision("b", "warn")]
        r = self._engine().decide(decisions)
        assert r.status == "pass"
        assert "advisory" in r.summary

    def test_one_warn_remains_advisory_pass(self):
        decisions = [_decision("a", "pass"), _decision("b", "warn")]
        r = self._engine().decide(decisions)
        assert r.status == "pass"
        assert "advisory" in r.summary

    def test_empty_decisions(self):
        r = self._engine().decide([])
        assert r.status == "pass"

    def test_confidence_is_weighted_average(self):
        decisions = [
            _decision("a", "pass", 0.8),
            _decision("b", "reject", 1.0),
        ]
        r = self._engine().decide(decisions)
        # reject 가중치 2: (0.8 * 1 + 1.0 * 2) / 3 = 2.8 / 3 ≈ 0.9333
        assert r.confidence_score == pytest.approx(0.9333, rel=1e-3)

    def test_reject_trumps_single_warn(self):
        decisions = [_decision("a", "warn"), _decision("b", "reject")]
        r = self._engine().decide(decisions)
        assert r.status == "reject"

    def test_summary_contains_finding_codes(self):
        d = AgentDecision(
            agent_name="security",
            status="reject",
            confidence_score=0.95,
            findings=[{"code": "SEC003", "message": "eval detected"}],
            suggested_actions=[],
        )
        r = self._engine().decide([d])
        assert "SEC003" in r.summary

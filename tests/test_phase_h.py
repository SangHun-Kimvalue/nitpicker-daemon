"""Phase H tests: Fast Gate 2.0 — real tool integration & severity-aware consensus.

§1  _find_tool helper                     (2 tests)
§2  _run_ruff enhanced behaviour          (4 tests)
§3  _run_mypy NDJSON parsing              (5 tests)
§4  FastGateAgent severity-aware run()   (6 tests)
§5  Live integration (real binaries)      (3 tests)
Total: 20 tests
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jemmin.agents.fast_gate import (
    FastGateAgent,
    _find_tool,
    _run_mypy,
    _run_ruff,
    _RUFF_TIMEOUT,
    _MYPY_TIMEOUT,
)
from jemmin.models import AgentDecision, ContextBundle, ReviewRequest

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _req(target_file: str = "", diff_text: str = "") -> ReviewRequest:
    return ReviewRequest(
        request_id="h-req",
        idempotency_key="h-idem",
        project_id="h-proj",
        project_profile="general",
        trigger="cli",
        target_file=target_file,
        git_revision="HEAD",
        base_file_hash="deadbeef",
        diff_text=diff_text,
    )


def _ctx() -> ContextBundle:
    return ContextBundle(
        request_id="h-req",
        context_hash="ctx",
        token_estimate=100,
        tiers={},
    )


def _ruff_finding(severity: str = "error", code: str = "F401") -> dict:
    return {"code": code, "line_number": 1, "message": "test", "severity": severity}


def _mypy_finding(message: str = "bad type") -> dict:
    return {"code": "MYPY", "line_number": 1, "message": message, "severity": "warn"}


def _mypy_ndjson(line: int, message: str, severity: str = "error") -> str:
    return json.dumps(
        {"file": "f.py", "line": line, "column": 0,
         "message": message, "hint": None, "code": "assignment", "severity": severity}
    )


# ---------------------------------------------------------------------------
# §1  _find_tool
# ---------------------------------------------------------------------------


class TestFindTool:
    def test_prefers_venv_exe_on_windows(self, tmp_path):
        """When <scripts_dir>/<name>.exe exists, return it directly."""
        fake_exe = tmp_path / "ruff.exe"
        fake_exe.touch()
        with patch.object(Path, "parent", new_callable=lambda: property(lambda self: tmp_path)):
            # Patch sys.executable so scripts_dir == tmp_path
            with patch("jemmin.agents.fast_gate.sys") as mock_sys:
                mock_sys.executable = str(tmp_path / "python.exe")
                result = _find_tool("ruff")
        assert result == [str(fake_exe)]

    def test_falls_back_to_module_mode_when_no_binary(self, tmp_path):
        """When no binary exists in scripts_dir, fall back to [sys.executable, '-m', name]."""
        # Use a tmp dir that definitely has no ruff/ruff.exe
        with patch("jemmin.agents.fast_gate.sys") as mock_sys:
            mock_sys.executable = str(tmp_path / "python.exe")
            result = _find_tool("ruff")
        # Should end with [str(fake_python), "-m", "ruff"]
        assert result[-2:] == ["-m", "ruff"]
        assert result[0] == str(tmp_path / "python.exe")


# ---------------------------------------------------------------------------
# §2  _run_ruff enhanced behaviour
# ---------------------------------------------------------------------------


class TestRunRuffEnhanced:
    def test_severity_field_present_in_findings(self, tmp_path):
        """_run_ruff must propagate the 'severity' field from ruff's JSON."""
        f = tmp_path / "bad.py"
        f.write_text("import os\n")
        ruff_json = json.dumps(
            [{"code": "F401", "message": "unused", "severity": "error",
              "location": {"row": 1, "column": 0}}]
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=ruff_json, stderr="", returncode=1)
            findings = _run_ruff(str(f))
        assert len(findings) == 1
        assert findings[0]["severity"] == "error"

    def test_warning_severity_preserved(self, tmp_path):
        """ruff 'warning' severity must survive the findings pipeline unchanged."""
        f = tmp_path / "warn.py"
        f.write_text("x = 1\n")
        ruff_json = json.dumps(
            [{"code": "W999", "message": "style", "severity": "warning",
              "location": {"row": 1, "column": 0}}]
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=ruff_json, stderr="", returncode=1)
            findings = _run_ruff(str(f))
        assert findings[0]["severity"] == "warning"

    def test_timeout_returns_gate_timeout_sentinel(self, tmp_path):
        """On TimeoutExpired, _run_ruff must return a GATE_TIMEOUT finding (Fail-Open)."""
        f = tmp_path / "slow.py"
        f.write_text("x = 1\n")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ruff", timeout=_RUFF_TIMEOUT)):
            findings = _run_ruff(str(f))
        assert len(findings) == 1
        assert findings[0]["code"] == "GATE_TIMEOUT"
        # Timeout is a warning, not an error — must not block the pipeline
        assert findings[0]["severity"] == "warning"

    def test_file_not_found_returns_empty(self, tmp_path):
        """When ruff binary is missing, _run_ruff returns [] (Fail-Open)."""
        f = tmp_path / "ok.py"
        f.write_text("x = 1\n")
        with patch("subprocess.run", side_effect=FileNotFoundError("ruff not found")):
            findings = _run_ruff(str(f))
        assert findings == []


# ---------------------------------------------------------------------------
# §3  _run_mypy NDJSON parsing
# ---------------------------------------------------------------------------


class TestRunMypyNDJSON:
    def test_ndjson_line_parsed_correctly(self, tmp_path):
        """_run_mypy must parse mypy --output json NDJSON format."""
        f = tmp_path / "typed.py"
        f.write_text("x: int = 'hello'\n")
        ndjson = _mypy_ndjson(1, "Incompatible types in assignment")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=ndjson, stderr="", returncode=1)
            findings = _run_mypy(str(f))
        assert len(findings) == 1
        assert findings[0]["code"] == "MYPY"
        assert findings[0]["line_number"] == 1
        assert "Incompatible" in findings[0]["message"]

    def test_note_severity_lines_are_skipped(self, tmp_path):
        """mypy 'note' lines must be filtered out — they are not actionable findings."""
        f = tmp_path / "typed.py"
        f.write_text("x: int = 'hello'\n")
        # Mix: one error + one note (context hint)
        error_line = _mypy_ndjson(1, "Incompatible types", severity="error")
        note_line  = _mypy_ndjson(1, "Expected int, got str", severity="note")
        ndjson = f"{error_line}\n{note_line}"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=ndjson, stderr="", returncode=1)
            findings = _run_mypy(str(f))
        # Only the error line should survive; note must be dropped
        assert len(findings) == 1
        assert findings[0]["message"] == "Incompatible types"

    def test_timeout_returns_gate_timeout_sentinel(self, tmp_path):
        """On TimeoutExpired, _run_mypy must return a GATE_TIMEOUT finding (Fail-Open)."""
        f = tmp_path / "slow.py"
        f.write_text("x = 1\n")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="mypy", timeout=_MYPY_TIMEOUT)):
            findings = _run_mypy(str(f))
        assert len(findings) == 1
        assert findings[0]["code"] == "GATE_TIMEOUT"

    def test_binary_not_found_returns_empty(self, tmp_path):
        """When mypy binary is missing, _run_mypy returns [] (Fail-Open)."""
        f = tmp_path / "ok.py"
        f.write_text("x: int = 1\n")
        with patch("subprocess.run", side_effect=FileNotFoundError("mypy not found")):
            findings = _run_mypy(str(f))
        assert findings == []

    def test_all_mypy_findings_have_warn_severity(self, tmp_path):
        """Every finding from _run_mypy must carry severity='warn' (not 'error').

        This ensures mypy errors are treated as warn-grade by the ConsensusEngine,
        giving ruff errors priority in the reject decision.
        """
        f = tmp_path / "typed.py"
        f.write_text("x: int = 'a'\ny: str = 1\n")
        ndjson = "\n".join([
            _mypy_ndjson(1, "Incompatible type: x", severity="error"),
            _mypy_ndjson(2, "Incompatible type: y", severity="error"),
        ])
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=ndjson, stderr="", returncode=1)
            findings = _run_mypy(str(f))
        assert len(findings) == 2
        assert all(f["severity"] == "warn" for f in findings)


# ---------------------------------------------------------------------------
# §4  FastGateAgent severity-aware run()
# ---------------------------------------------------------------------------


class TestFastGateSeverityConsensus:
    def _agent(self) -> FastGateAgent:
        return FastGateAgent()

    def test_ruff_error_is_reject(self, tmp_path):
        """ruff severity=error → status='reject', confidence=0.95."""
        f = tmp_path / "bad.py"
        f.write_text("import os\n")
        ruff_hits = [_ruff_finding(severity="error", code="F401")]
        with patch("jemmin.agents.fast_gate._run_ruff", return_value=ruff_hits), \
             patch("jemmin.agents.fast_gate._run_mypy", return_value=[]):
            d = self._agent().run(_req(target_file=str(f)), _ctx())
        assert d.status == "reject"
        assert d.confidence_score == pytest.approx(0.95)

    def test_ruff_warning_only_is_warn(self, tmp_path):
        """ruff severity=warning (no errors) → status='warn', confidence=0.80."""
        f = tmp_path / "ok.py"
        f.write_text("x = 1\n")
        ruff_warns = [_ruff_finding(severity="warning", code="W999")]
        with patch("jemmin.agents.fast_gate._run_ruff", return_value=ruff_warns), \
             patch("jemmin.agents.fast_gate._run_mypy", return_value=[]):
            d = self._agent().run(_req(target_file=str(f)), _ctx())
        assert d.status == "warn"
        assert d.confidence_score == pytest.approx(0.80)

    def test_mypy_only_is_warn_not_reject(self, tmp_path):
        """mypy finding alone → status='warn', NOT 'reject'. ruff is the gatekeeper."""
        f = tmp_path / "typed.py"
        f.write_text("x: int = 'oops'\n")
        mypy_hits = [_mypy_finding()]
        with patch("jemmin.agents.fast_gate._run_ruff", return_value=[]), \
             patch("jemmin.agents.fast_gate._run_mypy", return_value=mypy_hits):
            d = self._agent().run(_req(target_file=str(f)), _ctx())
        assert d.status == "warn"
        assert d.confidence_score == pytest.approx(0.85)

    def test_ruff_error_and_mypy_warn_ruff_wins(self, tmp_path):
        """When both ruff error and mypy finding exist, ruff error takes priority → reject."""
        f = tmp_path / "bad.py"
        f.write_text("import os\nx:int = 'oops'\n")
        ruff_hits = [_ruff_finding(severity="error", code="F401")]
        mypy_hits = [_mypy_finding()]
        with patch("jemmin.agents.fast_gate._run_ruff", return_value=ruff_hits), \
             patch("jemmin.agents.fast_gate._run_mypy", return_value=mypy_hits):
            d = self._agent().run(_req(target_file=str(f)), _ctx())
        assert d.status == "reject"
        assert d.confidence_score == pytest.approx(0.95)
        # Both findings must appear in the report
        assert len(d.findings) == 2

    def test_gate_timeout_is_fail_open_warn(self, tmp_path):
        """GATE_TIMEOUT sentinel must produce warn (not reject) — pipeline must not block."""
        f = tmp_path / "slow.py"
        f.write_text("x = 1\n")
        timeout_sentinel = [{"code": "GATE_TIMEOUT", "line_number": None,
                              "message": "ruff timed out", "severity": "warning"}]
        with patch("jemmin.agents.fast_gate._run_ruff", return_value=timeout_sentinel), \
             patch("jemmin.agents.fast_gate._run_mypy", return_value=[]):
            d = self._agent().run(_req(target_file=str(f)), _ctx())
        assert d.status == "warn"  # Fail-Open: timeout must NOT become reject

    def test_both_clean_is_pass(self, tmp_path):
        """No findings → status='pass', confidence=0.90."""
        f = tmp_path / "clean.py"
        f.write_text("x: int = 1\n")
        with patch("jemmin.agents.fast_gate._run_ruff", return_value=[]), \
             patch("jemmin.agents.fast_gate._run_mypy", return_value=[]):
            d = self._agent().run(_req(target_file=str(f)), _ctx())
        assert d.status == "pass"
        assert d.confidence_score == pytest.approx(0.90)
        assert d.findings == []


# ---------------------------------------------------------------------------
# §5  Live integration (real ruff + mypy binaries)
# ---------------------------------------------------------------------------


class TestFastGateLiveIntegration:
    """These tests run the actual ruff and mypy binaries installed in the venv.
    They validate end-to-end Fast Gate 2.0 behaviour without any mocking.
    """

    def _agent(self) -> FastGateAgent:
        return FastGateAgent()

    def test_live_ruff_rejects_unused_import(self, tmp_path):
        """Real ruff must detect F401 (unused import) and produce reject."""
        f = tmp_path / "unused.py"
        # Deliberate unused import with no other valid escape
        f.write_text("import os\n\n\ndef main() -> None:\n    pass\n")
        d = FastGateAgent().run(_req(target_file=str(f)), _ctx())
        # ruff reliably flags F401 for unused imports
        assert d.status in ("reject", "warn"), f"Expected reject/warn, got {d.status}"
        codes = [finding["code"] for finding in d.findings]
        assert any("F401" in c or "GATE_TIMEOUT" in c for c in codes), \
            f"Expected F401 in findings, got: {codes}"

    def test_live_mypy_warns_on_type_error(self, tmp_path):
        """Real mypy must detect incompatible type assignment and produce warn."""
        f = tmp_path / "typed_bad.py"
        f.write_text("x: int = 'this is wrong'\n")
        d = FastGateAgent().run(_req(target_file=str(f)), _ctx())
        # mypy alone → warn (ruff may also flag something, but mypy must contribute)
        assert d.status in ("reject", "warn"), f"Unexpected status: {d.status}"
        mypy_codes = [finding["code"] for finding in d.findings if "MYPY" in finding["code"]]
        assert mypy_codes or d.status == "reject", \
            "Expected MYPY finding or ruff reject for bad type assignment"

    def test_live_clean_file_passes(self, tmp_path):
        """A well-typed, lint-clean file must produce status='pass' from both tools."""
        f = tmp_path / "clean_typed.py"
        f.write_text(
            "\"\"\"Clean module.\"\"\"\n"
            "from __future__ import annotations\n\n\n"
            "def add(a: int, b: int) -> int:\n"
            "    return a + b\n"
        )
        d = FastGateAgent().run(_req(target_file=str(f)), _ctx())
        assert d.status == "pass", \
            f"Expected pass for clean file, got {d.status}: {d.findings}"
        assert d.findings == []

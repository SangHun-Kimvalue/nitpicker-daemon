"""Tests for FastGateAgent, SecurityAgent, ArchitectureAgent, and jemmin_lsp."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jemmin.agents.architecture import ArchitectureAgent, _scan_diff as arch_scan
from jemmin.agents.fast_gate import FastGateAgent, _run_mypy, _run_ruff
from jemmin.agents.security import SecurityAgent, _scan_diff as sec_scan
from jemmin.models import AgentDecision, ContextBundle, ReviewRequest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


def _make_request(
    diff_text: str = "",
    target_file: str = "",
) -> ReviewRequest:
    return ReviewRequest(
        request_id="test-req",
        idempotency_key="test-idem",
        project_id="test-proj",
        project_profile="general",
        trigger="cli",
        target_file=target_file,
        git_revision="HEAD",
        base_file_hash="abc123",
        diff_text=diff_text,
    )


def _make_context() -> ContextBundle:
    return ContextBundle(
        request_id="test-req",
        context_hash="ctx",
        token_estimate=100,
        tiers={},
    )


def _make_ruff_json(code: str, message: str, row: int = 1) -> str:
    return json.dumps(
        [{"code": code, "message": message, "location": {"row": row, "column": 0}}]
    )


def _make_mypy_ndjson(file: str, line: int, message: str, severity: str = "error") -> str:
    """Build a single mypy --output json NDJSON line."""
    import json
    return json.dumps(
        {"file": file, "line": line, "column": 0, "message": message,
         "hint": None, "code": "assignment", "severity": severity}
    )


# ---------------------------------------------------------------------------
# FastGateAgent – _run_ruff
# ---------------------------------------------------------------------------


class TestRunRuff:
    def test_missing_file_returns_empty(self):
        result = _run_ruff("/nonexistent/path/file.py")
        assert result == []

    def test_non_python_file_returns_empty(self, tmp_path):
        f = tmp_path / "README.md"
        f.write_text("# hello")
        assert _run_ruff(str(f)) == []

    def test_ruff_findings_parsed(self, tmp_path):
        bad_py = tmp_path / "bad.py"
        bad_py.write_text("import os,sys\n")
        fake_output = _make_ruff_json("E401", "multiple imports on one line", 1)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=fake_output, stderr="", returncode=1
            )
            findings = _run_ruff(str(bad_py))
        assert len(findings) == 1
        assert findings[0]["code"] == "E401"
        assert findings[0]["line_number"] == 1

    def test_empty_ruff_output_returns_empty(self, tmp_path):
        good_py = tmp_path / "ok.py"
        good_py.write_text("x = 1\n")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
            assert _run_ruff(str(good_py)) == []

    def test_ruff_invalid_json_returns_empty(self, tmp_path):
        f = tmp_path / "f.py"
        f.write_text("x=1\n")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="not-json", stderr="", returncode=1
            )
            assert _run_ruff(str(f)) == []


# ---------------------------------------------------------------------------
# FastGateAgent – _run_mypy
# ---------------------------------------------------------------------------


class TestRunMypy:
    def test_missing_file_returns_empty(self):
        assert _run_mypy("/nonexistent/file.py") == []

    def test_non_python_file_returns_empty(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text("{}")
        assert _run_mypy(str(f)) == []

    def test_mypy_error_parsed(self, tmp_path):
        f = tmp_path / "typed.py"
        f.write_text("x: int = 'hello'\n")
        # New format: mypy --output json produces NDJSON (one JSON object per line)
        fake_ndjson = _make_mypy_ndjson(str(f), 1, "Incompatible types")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=fake_ndjson, stderr="", returncode=1
            )
            findings = _run_mypy(str(f))
        assert len(findings) == 1
        assert findings[0]["code"] == "MYPY"
        assert findings[0]["line_number"] == 1
        assert findings[0]["severity"] == "warn"  # mypy findings are always warn-grade

    def test_mypy_success_returns_empty(self, tmp_path):
        f = tmp_path / "ok.py"
        f.write_text("x: int = 1\n")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
            assert _run_mypy(str(f)) == []


# ---------------------------------------------------------------------------
# FastGateAgent.run()
# ---------------------------------------------------------------------------


class TestFastGateAgentRun:
    def _agent(self) -> FastGateAgent:
        return FastGateAgent()

    def test_pass_when_no_findings(self, tmp_path):
        f = tmp_path / "clean.py"
        f.write_text("x = 1\n")
        with patch("jemmin.agents.fast_gate._run_ruff", return_value=[]), \
             patch("jemmin.agents.fast_gate._run_mypy", return_value=[]):
            decision = self._agent().run(_make_request(target_file=str(f)), _make_context())
        assert decision.status == "pass"
        assert decision.findings == []

    def test_reject_when_ruff_finds_issues(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text("import os,sys\n")
        ruff_hits = [{"code": "E401", "line_number": 1, "message": "bad import"}]
        with patch("jemmin.agents.fast_gate._run_ruff", return_value=ruff_hits), \
             patch("jemmin.agents.fast_gate._run_mypy", return_value=[]):
            decision = self._agent().run(_make_request(target_file=str(f)), _make_context())
        assert decision.status == "reject"
        assert any("E401" in f["code"] for f in decision.findings)

    def test_warn_when_mypy_finds_errors(self, tmp_path):
        # mypy-only findings → warn (not reject); ruff decides reject
        f = tmp_path / "typed.py"
        f.write_text("x: int = 'oops'\n")
        mypy_hits = [{"code": "MYPY", "line_number": 1, "message": "wrong type", "severity": "warn"}]
        with patch("jemmin.agents.fast_gate._run_ruff", return_value=[]), \
             patch("jemmin.agents.fast_gate._run_mypy", return_value=mypy_hits):
            decision = self._agent().run(_make_request(target_file=str(f)), _make_context())
        assert decision.status == "warn"          # mypy alone → warn, not reject
        assert decision.confidence_score == pytest.approx(0.85)

    def test_missing_file_gracefully_passes(self):
        decision = self._agent().run(
            _make_request(diff_text="+x = 1", target_file="/nonexistent/file.py"),
            _make_context(),
        )
        assert decision.status == "pass"

    def test_agent_name(self):
        assert self._agent().name == "fast_gate"


# ---------------------------------------------------------------------------
# SecurityAgent – pattern scanning
# ---------------------------------------------------------------------------


class TestSecScan:
    def _diff(self, added: str) -> str:
        return "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n" + "\n".join(
            f"+{line}" for line in added.splitlines()
        )

    def test_hardcoded_secret(self):
        findings = sec_scan(self._diff("api_key = 'super_secret_12345'"))
        codes = [f["code"] for f in findings]
        assert "SEC001" in codes

    def test_shell_true(self):
        findings = sec_scan(self._diff("subprocess.run(['ls'], shell=True)"))
        assert any(f["code"] == "SEC002" for f in findings)

    def test_eval(self):
        findings = sec_scan(self._diff("result = eval(user_input)"))
        assert any(f["code"] == "SEC003" for f in findings)

    def test_exec(self):
        findings = sec_scan(self._diff("exec(code_string)"))
        assert any(f["code"] == "SEC003" for f in findings)

    def test_pickle_loads(self):
        findings = sec_scan(self._diff("data = pickle.loads(raw)"))
        assert any(f["code"] == "SEC005" for f in findings)

    def test_yaml_load_no_loader(self):
        findings = sec_scan(self._diff("cfg = yaml.load(stream)"))
        assert any(f["code"] == "SEC006" for f in findings)

    def test_random_use(self):
        findings = sec_scan(self._diff("token = random.choice(chars)"))
        assert any(f["code"] == "SEC007" for f in findings)

    def test_weak_hash_md5(self):
        findings = sec_scan(self._diff("h = hashlib.md5(data)"))
        assert any(f["code"] == "SEC008" for f in findings)

    def test_clean_diff_no_findings(self):
        findings = sec_scan(self._diff("x = 1 + 2"))
        assert findings == []

    def test_only_added_lines_checked(self):
        # removal of a bad pattern should NOT trigger the rule
        diff = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-api_key = 'old_secret'\n+x = 1\n"
        findings = sec_scan(diff)
        assert findings == []


class TestSecurityAgentRun:
    def test_pass_on_clean_diff(self):
        req = _make_request(diff_text="+x = 1\n")
        decision = SecurityAgent().run(req, _make_context())
        assert decision.status == "pass"
        assert decision.agent_name == "security"

    def test_reject_on_eval(self):
        req = _make_request(diff_text="+result = eval(user_input)\n")
        decision = SecurityAgent().run(req, _make_context())
        assert decision.status == "reject"
        assert decision.confidence_score == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# ArchitectureAgent – pattern scanning
# ---------------------------------------------------------------------------


class TestArchScan:
    def _diff(self, added: str) -> str:
        return "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n" + "\n".join(
            f"+{line}" for line in added.splitlines()
        )

    def test_looped_io_flag(self):
        code = textwrap.dedent("""\
            for item in items:
                with open(item) as f:
                    data = f.read()
        """)
        findings = arch_scan(self._diff(code))
        assert any(f["code"] == "ARCH001" for f in findings)

    @pytest.mark.parametrize(
        ("added", "expected_code"),
        [
            ("for item in items:\n    requests.get(item)", "ARCH001"),
            ("for row in rows:\n    while row.pending:\n        cursor.execute(row.sql)", "ARCH001"),
            ("while ready:\n    import optional_module", "ARCH002"),
            ("for item in items:\n    while item.pending:\n        import optional_module", "ARCH002"),
        ],
    )
    def test_loop_rules_detect_top_level_and_nested_body(self, added, expected_code):
        codes = {finding["code"] for finding in arch_scan(self._diff(added))}
        assert expected_code in codes

    @pytest.mark.parametrize(
        "added",
        [
            "if ready:\n    for item in items:\n    requests.get(item)",
            "for item in items:\n    # requests.get(item)",
            "for item in items:\n    # import optional_module",
            "for item in items:\n    from optional_module import value",
        ],
    )
    def test_loop_rules_ignore_sibling_comment_and_from_import(self, added):
        codes = {finding["code"] for finding in arch_scan(self._diff(added))}
        assert "ARCH001" not in codes
        assert "ARCH002" not in codes

    def test_blank_line_keeps_loop_stack(self):
        codes = {
            finding["code"]
            for finding in arch_scan(self._diff("for item in items:\n\n    open(item)"))
        }
        assert "ARCH001" in codes

    @pytest.mark.parametrize(
        ("body", "expected_code"),
        [
            ("\trequests.get(item)", "ARCH001"),
            ("\timport optional_module", "ARCH002"),
        ],
    )
    def test_tab_indented_loop_body(self, body, expected_code):
        codes = {
            finding["code"]
            for finding in arch_scan(self._diff(f"for item in items:\n{body}"))
        }
        assert expected_code in codes

    def test_mixed_indentation_uses_visual_columns_for_same_indent_sibling(self):
        added = "if ready:\n\tfor item in items:\n        requests.get(item)"
        codes = {finding["code"] for finding in arch_scan(self._diff(added))}
        assert "ARCH001" not in codes
        assert "ARCH002" not in codes

    def test_hunk_boundary_resets_loop_stack(self):
        diff = (
            "--- a/file.py\n+++ b/file.py\n"
            "@@ -1 +1 @@\n+for item in items:\n"
            "@@ -20 +20 @@\n+    requests.get(item)\n+    import optional_module\n"
        )
        codes = {finding["code"] for finding in arch_scan(diff)}
        assert "ARCH001" not in codes
        assert "ARCH002" not in codes
        assert self._legacy_loop_codes("for item in items:\n    requests.get(item)\n    import optional_module") == {
            "ARCH001",
            "ARCH002",
        }

    @staticmethod
    def _legacy_loop_codes(added: str) -> set[str]:
        patterns = {
            "ARCH001": re.compile(
                r"^\s*(for|while)\b[^\r\n]*:\s*[\r\n]+"
                r"(?:[^\S\r\n]+[^\r\n]*[\r\n]+)*?"
                r"[^\S\r\n]+[^\r\n]*(open\(|requests\.|httpx\.|aiohttp\.|sqlite3\.|cursor\.execute)",
                re.MULTILINE,
            ),
            "ARCH002": re.compile(
                r"(for|while)\s+.+:\s*[\r\n]+(?:[^\S\r\n]+.*[\r\n]+)*[^\S\r\n]+import\s"
            ),
        }
        return {code for code, pattern in patterns.items() if pattern.search(added)}

    @pytest.mark.parametrize(
        ("name", "added", "expected_new", "relation"),
        [
            ("io_body", "for x in xs:\n    open(x)", {"ARCH001"}, "preserved"),
            ("import_body", "while ready:\n    import optional", {"ARCH002"}, "preserved"),
            ("same_indent", "if ready:\n    for x in xs:\n    requests.get(x)", set(), "intentional_delta"),
            ("comment", "for x in xs:\n    # requests.get(x)", set(), "intentional_delta"),
        ],
    )
    def test_legacy_differential_small_fixtures(self, name, added, expected_new, relation):
        del name
        legacy = self._legacy_loop_codes(added)
        current = {
            finding["code"]
            for finding in arch_scan(self._diff(added))
            if finding["code"] in {"ARCH001", "ARCH002"}
        }
        assert current == expected_new
        if relation == "preserved":
            assert current == legacy
        else:
            assert legacy and not current

    @pytest.mark.parametrize(
        "pattern_source",
        [
            (
                r"^\s*(for|while)\b[^\r\n]*:\s*[\r\n]+"
                r"(?:[^\S\r\n]+[^\r\n]*[\r\n]+)*?"
                r"[^\S\r\n]+[^\r\n]*(open\(|requests\.|httpx\.|aiohttp\.|sqlite3\.|cursor\.execute)"
            ),
            r"(for|while)\s+.+:\s*[\r\n]+(?:[^\S\r\n]+.*[\r\n]+)*[^\S\r\n]+import\s",
        ],
    )
    def test_frozen_legacy_loop_regex_exceeds_hard_cap(self, pattern_source):
        added = "for item in items:\n" + "\n".join(
            f"    value_{index} = item" for index in range(11)
        )
        script = (
            "import re, sys\n"
            "pattern = re.compile(sys.argv[1], re.MULTILINE)\n"
            "pattern.search(sys.argv[2])\n"
        )
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess.run(
                [sys.executable, "-c", script, pattern_source, added],
                check=False,
                timeout=1.0,
            )

    def test_bounded_scanner_finishes_long_clean_fixture_in_child(self):
        added = "for item in items:\n" + "\n".join(
            f"    value_{index} = item" for index in range(11)
        )
        diff = self._diff(added)
        script = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from jemmin.agents.architecture import _scan_diff\n"
            "codes = {finding['code'] for finding in _scan_diff(sys.argv[2])}\n"
            "raise SystemExit(1 if {'ARCH001', 'ARCH002'} & codes else 0)\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script, str(ROOT / "src"), diff],
            cwd=ROOT,
            check=False,
            timeout=2,
        )
        assert completed.returncode == 0

    @pytest.mark.parametrize(
        ("added", "expected_code"),
        [
            ("try:\n    risky()\nexcept Exception:\n    pass", "ARCH003"),
            ("try:\n    risky()\nexcept:\n    pass", "ARCH004"),
            ("print('debug')", "ARCH005"),
            ("CACHE: list = []", "ARCH006"),
            ("def public_api():\n    return 1\n" + "\n".join(f"x_{i} = {i}" for i in range(20)), "ARCH007"),
        ],
    )
    def test_non_loop_architecture_rules_are_preserved(self, added, expected_code):
        codes = {finding["code"] for finding in arch_scan(self._diff(added))}
        assert expected_code in codes

    def test_bare_except_pass(self):
        code = textwrap.dedent("""\
            try:
                risky()
            except:
                pass
        """)
        findings = arch_scan(self._diff(code))
        assert any(f["code"] in ("ARCH003", "ARCH004") for f in findings)

    def test_print_in_added_line(self):
        findings = arch_scan(self._diff("print('debug value')"))
        assert any(f["code"] == "ARCH005" for f in findings)

    def test_clean_diff_no_findings(self):
        code = "x = 1\ndef add(a, b):\n    return a + b\n"
        findings = arch_scan(self._diff(code))
        assert findings == []


class TestArchitectureAgentRun:
    def test_pass_on_clean(self):
        req = _make_request(diff_text="+x = 1\n")
        decision = ArchitectureAgent().run(req, _make_context())
        assert decision.status == "pass"
        assert decision.agent_name == "architecture"

    def test_warn_on_print(self):
        req = _make_request(diff_text="+print('hello')\n")
        decision = ArchitectureAgent().run(req, _make_context())
        assert decision.status == "warn"
        assert decision.confidence_score == pytest.approx(0.75)

    def test_agent_name(self):
        assert ArchitectureAgent().name == "architecture"


# ---------------------------------------------------------------------------
# jemmin_lsp – _review_to_diagnostics
# ---------------------------------------------------------------------------


class TestReviewToDiagnostics:
    def _import(self):
        import importlib, sys
        bin_path = str(ROOT / "bin")
        if bin_path not in sys.path:
            sys.path.insert(0, bin_path)
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "jemmin_lsp", ROOT / "bin" / "jemmin_lsp.py"
        )
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    def test_no_target_file_returns_empty(self):
        mod = self._import()
        uri, diags = mod._review_to_diagnostics({"target_file": "", "details": []})
        assert uri == ""
        assert diags == []

    def test_detail_maps_to_diagnostic(self, tmp_path):
        mod = self._import()
        f = tmp_path / "foo.py"
        f.write_text("x = 1\n")
        review = {
            "target_file": str(f),
            "details": [{"line_number": 3, "issue": "bad code", "severity": "error"}],
        }
        uri, diags = mod._review_to_diagnostics(review)
        assert "foo.py" in uri or uri.endswith("foo.py")
        assert len(diags) == 1
        assert diags[0]["severity"] == 1  # error → 1
        assert diags[0]["range"]["start"]["line"] == 2  # 3 → 0-based index 2

    def test_default_severity_is_warning(self, tmp_path):
        mod = self._import()
        f = tmp_path / "bar.py"
        f.write_text("x = 1\n")
        review = {
            "target_file": str(f),
            "details": [{"line_number": 1, "issue": "minor issue"}],
        }
        _, diags = mod._review_to_diagnostics(review)
        assert diags[0]["severity"] == 2  # warning

    def test_source_is_jemmin(self, tmp_path):
        mod = self._import()
        f = tmp_path / "baz.py"
        f.write_text("x = 1\n")
        review = {
            "target_file": str(f),
            "details": [{"line_number": 1, "issue": "x"}],
        }
        _, diags = mod._review_to_diagnostics(review)
        assert diags[0]["source"] == "jemmin"

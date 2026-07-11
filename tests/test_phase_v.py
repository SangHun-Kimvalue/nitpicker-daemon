"""Phase V: Context Provider 확장 — SymbolProvider, HistoryProvider, PolicyProvider 테스트.

테스트 항목:
  - SymbolProvider: AST 파싱, 심볼 추출, diff fallback, 비-Python 처리
  - HistoryProvider: JSONL 스캔, 파일명 매칭, limit 제한, 빈 로그 처리
  - PolicyProvider: 규칙 로드, 프로필 로드, 캐시 무효화, 크기 제한
  - StaticContextService 통합: 4-tier 동시 수집, project_root 전달
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from jemmin.context.providers.symbol_provider import SymbolProvider
from jemmin.context.providers.history_provider import HistoryProvider
from jemmin.context.providers.policy_provider import PolicyProvider
from jemmin.models import ReviewRequest
from jemmin.services.context_svc import StaticContextService


# ── Fixtures ─────────────────────────────────────────────────

def _make_request(
    target_file: str = "src/example.py",
    diff_text: str = "",
    project_profile: str = "general",
) -> ReviewRequest:
    return ReviewRequest(
        request_id="test-001",
        idempotency_key="idem-001",
        project_id="test-project",
        project_profile=project_profile,
        trigger="cli",
        target_file=target_file,
        diff_text=diff_text,
    )


SAMPLE_PYTHON = textwrap.dedent("""\
    import os
    from pathlib import Path as P

    class FileProcessor(object):
        def __init__(self, root: str):
            self.root = root

        def process(self, name: str) -> bool:
            path = P(self.root) / name
            return path.exists()

    def helper(x, y):
        return os.path.join(x, y)

    result = helper("a", "b")
    FileProcessor(".").process("test.txt")
""")


SAMPLE_DIFF = textwrap.dedent("""\
    --- a/src/example.py
    +++ b/src/example.py
    @@ -1,3 +1,5 @@
    +import subprocess
    +
     import os
     from pathlib import Path
    +subprocess.run(["ls"])
""")


# ═══════════════════════════════════════════════════════════════
# SymbolProvider
# ═══════════════════════════════════════════════════════════════

class TestSymbolProvider:
    def test_collect_from_file(self, tmp_path: Path) -> None:
        """실제 파일에서 심볼을 추출합니다."""
        py_file = tmp_path / "src" / "example.py"
        py_file.parent.mkdir(parents=True)
        py_file.write_text(SAMPLE_PYTHON, encoding="utf-8")

        provider = SymbolProvider(project_root=tmp_path)
        request = _make_request(target_file="src/example.py")
        fragment = provider.collect(request)

        assert fragment.tier == "tier2"
        assert len(fragment.entries) > 0
        assert fragment.metadata["source"] == "symbol"
        assert fragment.metadata["imports"] >= 2   # os, pathlib.Path
        assert fragment.metadata["classes"] >= 1   # FileProcessor
        assert fragment.metadata["functions"] >= 1  # helper
        assert fragment.metadata["calls"] >= 1

    def test_collect_from_diff_fallback(self) -> None:
        """파일이 없으면 diff에서 추가된 라인을 파싱합니다."""
        provider = SymbolProvider()
        request = _make_request(diff_text=SAMPLE_DIFF)
        fragment = provider.collect(request)

        assert fragment.tier == "tier2"
        # diff에서 "import subprocess" 추출
        assert fragment.metadata.get("imports", 0) >= 1

    def test_non_python_file(self) -> None:
        """비-Python 파일은 빈 fragment를 반환합니다."""
        provider = SymbolProvider()
        request = _make_request(target_file="src/main.cpp")
        fragment = provider.collect(request)

        assert fragment.tier == "tier2"
        assert fragment.entries == []
        assert fragment.metadata["reason"] == "not_python"

    def test_syntax_error_graceful(self) -> None:
        """구문 오류가 있는 코드는 빈 fragment를 반환합니다."""
        provider = SymbolProvider()
        request = _make_request(diff_text="+def broken(\n+    pass")
        fragment = provider.collect(request)

        assert fragment.tier == "tier2"
        assert fragment.entries == []
        assert fragment.metadata["reason"] == "syntax_error"

    def test_empty_target(self) -> None:
        """target_file이 없으면 빈 fragment를 반환합니다."""
        provider = SymbolProvider()
        request = _make_request(target_file="")
        fragment = provider.collect(request)

        assert fragment.entries == []

    def test_class_methods_extracted(self, tmp_path: Path) -> None:
        """클래스 내부 메서드가 정확히 추출됩니다."""
        py_file = tmp_path / "cls.py"
        py_file.write_text(SAMPLE_PYTHON, encoding="utf-8")

        provider = SymbolProvider(project_root=tmp_path)
        request = _make_request(target_file="cls.py")
        fragment = provider.collect(request)

        entries_text = "\n".join(fragment.entries)
        assert "FileProcessor" in entries_text
        assert "__init__" in entries_text
        assert "process" in entries_text

    def test_call_frequency_ordering(self, tmp_path: Path) -> None:
        """호출 빈도순으로 상위 호출이 표시됩니다."""
        code = "import os\n" + "\n".join(f"os.path.join('a{i}', 'b')" for i in range(20))
        py_file = tmp_path / "calls.py"
        py_file.write_text(code, encoding="utf-8")

        provider = SymbolProvider(project_root=tmp_path)
        request = _make_request(target_file="calls.py")
        fragment = provider.collect(request)

        entries_text = "\n".join(fragment.entries)
        assert "os.path.join" in entries_text


# ═══════════════════════════════════════════════════════════════
# HistoryProvider
# ═══════════════════════════════════════════════════════════════

class TestHistoryProvider:
    def _write_log(self, path: Path, records: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=True) + "\n")

    def test_collect_matching_reviews(self, tmp_path: Path) -> None:
        """동일 파일에 대한 과거 리뷰를 정확히 수집합니다."""
        log_path = tmp_path / "review_history.jsonl"
        self._write_log(log_path, [
            {"request_id": "r1", "status": "pass", "summary": "LGTM for src/example.py", "confidence_score": 0.9, "timestamp": "2026-03-25T10:00:00"},
            {"request_id": "r2", "status": "rejected", "summary": "Issues in src/other.py", "confidence_score": 0.8, "timestamp": "2026-03-25T11:00:00"},
            {"request_id": "r3", "status": "rejected", "summary": "Bug in src/example.py", "confidence_score": 0.7, "timestamp": "2026-03-25T12:00:00"},
        ])

        provider = HistoryProvider(review_log_path=log_path)
        request = _make_request(target_file="src/example.py")
        fragment = provider.collect(request)

        assert fragment.tier == "tier4"
        assert fragment.metadata["matches"] == 2  # r1 + r3
        assert len(fragment.entries) > 0

    def test_limit_respected(self, tmp_path: Path) -> None:
        """limit 이상의 결과는 반환하지 않습니다."""
        log_path = tmp_path / "log.jsonl"
        records = [
            {"request_id": f"r{i}", "status": "pass", "summary": f"Review src/example.py #{i}", "confidence_score": 0.5, "timestamp": f"2026-03-{i+1:02d}T10:00:00"}
            for i in range(10)
        ]
        self._write_log(log_path, records)

        provider = HistoryProvider(review_log_path=log_path, limit=3)
        request = _make_request(target_file="src/example.py")
        fragment = provider.collect(request)

        assert fragment.metadata["matches"] == 3

    def test_no_log_file(self) -> None:
        """로그 파일이 없으면 빈 fragment를 반환합니다."""
        provider = HistoryProvider(review_log_path="/nonexistent/path.jsonl")
        request = _make_request()
        fragment = provider.collect(request)

        assert fragment.entries == []
        assert fragment.metadata["reason"] == "no_log"

    def test_no_log_path(self) -> None:
        """로그 경로 자체가 None이면 빈 fragment를 반환합니다."""
        provider = HistoryProvider()
        request = _make_request()
        fragment = provider.collect(request)

        assert fragment.entries == []

    def test_reverse_scan_returns_newest_first(self, tmp_path: Path) -> None:
        """역순 스캔으로 최신 리뷰가 먼저 매칭됩니다."""
        log_path = tmp_path / "log.jsonl"
        self._write_log(log_path, [
            {"request_id": "old", "status": "pass", "summary": "Old review src/example.py", "confidence_score": 0.5, "timestamp": "2026-01-01T10:00:00"},
            {"request_id": "new", "status": "rejected", "summary": "New review src/example.py", "confidence_score": 0.9, "timestamp": "2026-03-25T10:00:00"},
        ])

        provider = HistoryProvider(review_log_path=log_path, limit=1)
        request = _make_request(target_file="src/example.py")
        fragment = provider.collect(request)

        # 최신(new)이 먼저 매칭되어야 함
        assert fragment.metadata["matches"] == 1
        entries_text = "\n".join(fragment.entries)
        assert "New review" in entries_text

    def test_malformed_json_lines_skipped(self, tmp_path: Path) -> None:
        """잘못된 JSON 라인은 건너뜁니다."""
        log_path = tmp_path / "log.jsonl"
        log_path.write_text(
            '{"status": "pass", "summary": "src/example.py ok", "confidence_score": 0.9, "timestamp": "2026-01-01"}\n'
            'INVALID JSON LINE\n'
            '{"status": "rejected", "summary": "src/example.py bad", "confidence_score": 0.1, "timestamp": "2026-01-02"}\n',
            encoding="utf-8",
        )

        provider = HistoryProvider(review_log_path=log_path)
        request = _make_request(target_file="src/example.py")
        fragment = provider.collect(request)

        assert fragment.metadata["matches"] == 2


# ═══════════════════════════════════════════════════════════════
# PolicyProvider
# ═══════════════════════════════════════════════════════════════

class TestPolicyProvider:
    def test_collect_rules_and_profile(self, tmp_path: Path) -> None:
        """규칙 파일과 프로필을 동시에 수집합니다."""
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "no_eval.md").write_text("# No eval\nDo not use eval() or exec().", encoding="utf-8")

        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        (profiles_dir / "general.yaml").write_text("name: general\nauto_apply: false\n", encoding="utf-8")

        provider = PolicyProvider(rules_dir=rules_dir, profiles_dir=profiles_dir)
        request = _make_request(project_profile="general")
        fragment = provider.collect(request)

        assert fragment.tier == "tier3"
        assert fragment.metadata.get("rules_count", 0) >= 1
        assert fragment.metadata.get("profile") == "general"

        entries_text = "\n".join(fragment.entries)
        assert "No eval" in entries_text
        assert "general" in entries_text

    def test_no_rules_dir(self) -> None:
        """규칙 디렉토리가 없으면 빈 fragment를 반환합니다."""
        provider = PolicyProvider()
        request = _make_request()
        fragment = provider.collect(request)

        assert fragment.tier == "tier3"
        assert fragment.metadata.get("reason") == "no_rules"

    def test_cache_invalidation(self, tmp_path: Path) -> None:
        """캐시 무효화 후 새 규칙이 반영됩니다."""
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "rule1.md").write_text("Rule 1 content", encoding="utf-8")

        provider = PolicyProvider(rules_dir=rules_dir)
        request = _make_request()

        fragment1 = provider.collect(request)
        assert fragment1.metadata.get("rules_count") == 1

        # 새 규칙 추가
        (rules_dir / "rule2.md").write_text("Rule 2 content", encoding="utf-8")
        provider.invalidate_cache()

        fragment2 = provider.collect(request)
        assert fragment2.metadata.get("rules_count") == 2

    def test_non_rule_files_ignored(self, tmp_path: Path) -> None:
        """.py, .json 등 규칙 파일이 아닌 것은 무시합니다."""
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "rule.md").write_text("Valid rule", encoding="utf-8")
        (rules_dir / "script.py").write_text("print('not a rule')", encoding="utf-8")
        (rules_dir / "data.json").write_text('{"not": "rule"}', encoding="utf-8")

        provider = PolicyProvider(rules_dir=rules_dir)
        request = _make_request()
        fragment = provider.collect(request)

        assert fragment.metadata.get("rules_count") == 1

    def test_profile_not_found(self, tmp_path: Path) -> None:
        """존재하지 않는 프로필은 빈 결과를 반환합니다."""
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()

        provider = PolicyProvider(profiles_dir=profiles_dir)
        request = _make_request(project_profile="nonexistent")
        fragment = provider.collect(request)

        assert "profile" not in fragment.metadata

    def test_large_rule_truncated(self, tmp_path: Path) -> None:
        """8KB 초과 규칙 파일은 잘립니다."""
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "huge.md").write_text("X" * 10000, encoding="utf-8")

        provider = PolicyProvider(rules_dir=rules_dir)
        request = _make_request()
        fragment = provider.collect(request)

        entries_text = "\n".join(fragment.entries)
        assert "truncated" in entries_text


# ═══════════════════════════════════════════════════════════════
# StaticContextService 통합 테스트
# ═══════════════════════════════════════════════════════════════

class TestContextServiceIntegration:
    def test_all_tiers_populated(self, tmp_path: Path) -> None:
        """project_root를 전달하면 tier1~4가 모두 수집됩니다."""
        # Setup: Python file for SymbolProvider
        py_file = tmp_path / "src" / "example.py"
        py_file.parent.mkdir(parents=True)
        py_file.write_text(SAMPLE_PYTHON, encoding="utf-8")

        # Setup: Rules for PolicyProvider
        rules_dir = tmp_path / "config" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "rule1.md").write_text("# Rule 1\nNo eval.", encoding="utf-8")

        # Setup: Profile
        profiles_dir = tmp_path / "config" / "profiles"
        profiles_dir.mkdir(parents=True)
        (profiles_dir / "general.yaml").write_text("name: general\n", encoding="utf-8")

        # Setup: Review log for HistoryProvider
        log_path = tmp_path / "logs" / "review_history.jsonl"
        log_path.parent.mkdir(parents=True)
        log_path.write_text(
            json.dumps({"status": "pass", "summary": "src/example.py ok", "confidence_score": 0.9, "timestamp": "2026-01-01"}) + "\n",
            encoding="utf-8",
        )

        svc = StaticContextService(
            review_log_path=log_path,
            project_root=tmp_path,
        )

        request = _make_request(
            target_file="src/example.py",
            diff_text=SAMPLE_DIFF,
        )
        bundle = svc.build_context(request)

        # tier1: diff (DiffProvider)
        assert len(bundle.tiers["tier1"]) > 0
        # tier2: symbols (SymbolProvider)
        assert len(bundle.tiers["tier2"]) > 0
        # tier3: policy (PolicyProvider)
        assert len(bundle.tiers["tier3"]) > 0
        # tier4: history (HistoryProvider)
        assert len(bundle.tiers["tier4"]) > 0

    def test_without_project_root(self) -> None:
        """project_root 없으면 DiffProvider만 동작합니다 (하위 호환)."""
        svc = StaticContextService()
        request = _make_request(diff_text=SAMPLE_DIFF)
        bundle = svc.build_context(request)

        assert len(bundle.tiers["tier1"]) > 0
        assert bundle.tiers["tier2"] == []
        assert bundle.tiers["tier3"] == []
        assert bundle.tiers["tier4"] == []

    def test_cache_still_works(self, tmp_path: Path) -> None:
        """캐시가 provider 확장 후에도 정상 동작합니다."""
        svc = StaticContextService(project_root=tmp_path)
        request = _make_request(diff_text=SAMPLE_DIFF)

        bundle1 = svc.build_context(request)
        bundle2 = svc.build_context(request)

        assert bundle1.context_hash == bundle2.context_hash
        assert svc.cache_size == 1

    def test_invalidation_clears_cache(self, tmp_path: Path) -> None:
        """캐시 무효화 후 재수집이 발생합니다."""
        svc = StaticContextService(project_root=tmp_path)
        request = _make_request(diff_text=SAMPLE_DIFF)

        svc.build_context(request)
        assert svc.cache_size == 1

        svc.invalidate_for_path("src/example.py")
        assert svc.cache_size == 0

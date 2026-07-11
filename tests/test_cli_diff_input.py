"""jemmin_cli diff 입력 경로 테스트."""
from __future__ import annotations

import argparse
import io
from pathlib import Path

import pytest

from bin import jemmin_cli
from bin.jemmin_cli import _resolve_diff_text


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--diff")
    parser.add_argument("--diff-file")
    parser.add_argument("--diff-stdin", action="store_true")
    return parser


def test_resolve_diff_direct_text() -> None:
    parser = _parser()
    args = parser.parse_args(["--diff", "--- a/x\n+++ b/x\n@@ -1 +1 @@"])
    assert _resolve_diff_text(args, parser).startswith("--- a/x")


def test_resolve_diff_file(tmp_path) -> None:
    diff_file = tmp_path / "change.diff"
    diff_file.write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n", encoding="utf-8")

    parser = _parser()
    args = parser.parse_args(["--diff-file", str(diff_file)])
    assert "+new" in _resolve_diff_text(args, parser)


def test_resolve_diff_file_utf16(tmp_path) -> None:
    diff_file = tmp_path / "change.diff"
    diff_file.write_text("--- a/x\n+++ b/x\n", encoding="utf-16")

    parser = _parser()
    args = parser.parse_args(["--diff-file", str(diff_file)])
    assert _resolve_diff_text(args, parser).startswith("--- a/x")


def test_resolve_diff_file_utf16_le_without_bom(tmp_path) -> None:
    diff_file = tmp_path / "change.diff"
    diff_file.write_bytes("--- a/x\n+++ b/x\n".encode("utf-16-le"))

    parser = _parser()
    args = parser.parse_args(["--diff-file", str(diff_file)])
    assert _resolve_diff_text(args, parser).startswith("--- a/x")


def test_resolve_diff_file_cp949_korean(tmp_path) -> None:
    diff_file = tmp_path / "change.diff"
    diff_file.write_bytes(
        "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-# 이전\n+# 이후\n".encode("cp949")
    )

    parser = _parser()
    args = parser.parse_args(["--diff-file", str(diff_file)])
    diff_text = _resolve_diff_text(args, parser)

    assert "# 이후" in diff_text
    assert "\x00" not in diff_text


def test_resolve_diff_stdin(monkeypatch) -> None:
    parser = _parser()
    args = parser.parse_args(["--diff-stdin"])
    monkeypatch.setattr("sys.stdin", io.StringIO("--- a/x\n+++ b/x\n"))

    assert _resolve_diff_text(args, parser) == "--- a/x\n+++ b/x\n"


def test_rejects_multiple_diff_sources(tmp_path) -> None:
    diff_file = tmp_path / "change.diff"
    diff_file.write_text("diff", encoding="utf-8")

    parser = _parser()
    args = parser.parse_args(["--diff", "diff", "--diff-file", str(diff_file)])

    with pytest.raises(SystemExit):
        _resolve_diff_text(args, parser)


def test_ollama_provider_uses_reviewer_config_over_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "reviewer_config.yaml"
    config_file.write_text(
        "provider:\n"
        "  default: ollama\n"
        "  ollama_model: qwen2.5-coder:7b\n"
        "  ollama_base_url: http://configured:11434\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(jemmin_cli, "_REVIEWER_CONFIG_PATH", config_file)
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:3b")

    created: dict[str, str | None] = {}

    class FakeOllamaProvider:
        def __init__(self, model: str | None = None, base_url: str | None = None) -> None:
            created["model"] = model
            created["base_url"] = base_url
            self._model = model

        def available(self) -> bool:
            return True

    monkeypatch.setattr(jemmin_cli, "OllamaProvider", FakeOllamaProvider)

    jemmin_cli._select_provider("ollama")

    assert created == {
        "model": "qwen2.5-coder:7b",
        "base_url": "http://configured:11434",
    }


def test_ollama_provider_explicit_model_overrides_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "reviewer_config.yaml"
    config_file.write_text(
        "provider:\n  ollama_model: qwen2.5-coder:7b\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(jemmin_cli, "_REVIEWER_CONFIG_PATH", config_file)

    created: dict[str, str | None] = {}

    class FakeOllamaProvider:
        def __init__(self, model: str | None = None, base_url: str | None = None) -> None:
            created["model"] = model
            created["base_url"] = base_url
            self._model = model

        def available(self) -> bool:
            return True

    monkeypatch.setattr(jemmin_cli, "OllamaProvider", FakeOllamaProvider)

    jemmin_cli._select_provider("ollama", model="qwen2.5-coder:3b")

    assert created["model"] == "qwen2.5-coder:3b"


def test_daemon_skipped_when_model_override_is_set() -> None:
    args = argparse.Namespace(
        no_daemon=False,
        use_daemon=True,
        provider="ollama",
        model="qwen2.5-coder:3b",
    )

    assert not jemmin_cli._should_try_daemon(args)


def test_daemon_skipped_for_default_ollama_provider() -> None:
    args = argparse.Namespace(
        no_daemon=False,
        use_daemon=False,
        provider="ollama",
        model=None,
    )

    assert not jemmin_cli._should_try_daemon(args)


def test_daemon_allowed_only_for_explicit_legacy_mock_path() -> None:
    args = argparse.Namespace(
        no_daemon=False,
        use_daemon=True,
        provider="mock",
        model=None,
    )

    assert jemmin_cli._should_try_daemon(args)

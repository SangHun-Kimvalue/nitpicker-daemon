"""Provider 플러그인화(Phase 1) 단위 테스트.

검증 범위:
- (a) Ollama → mini RESPONSE_SCHEMA 어댑트 (HTTP mock으로 Ollama JSON → result_code)
- (b) load_settings provider 선택 + gemini-key-optional (provider=ollama면 키 없어도 OK)
- (c) **silent-PASS 금지(D4)**: provider 미가용/timeout/파싱 실패 → 예외 전파(REVIEW_PASSED 아님)
      + mini_nitpicker exit 2 회귀.

전제: 이 테스트는 실제 Ollama 서버를 호출하지 않는다 (HTTP 계층 mock).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from jemmin import mini_reviewer
from jemmin.mini_reviewer import (
    MiniReviewerSettings,
    generate_review,
    load_settings,
)
from jemmin.providers.ollama import OllamaProvider


ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings(**overrides) -> MiniReviewerSettings:
    defaults = dict(
        gemini_api_key="",
        gemini_model="gemini-test",
        watch_path="src",
        debounce_seconds=0.1,
        file_extensions=(".py",),
        skip=False,
        provider="ollama",
        ollama_model="qwen2.5-coder:7b",
        ollama_base_url="http://localhost:11434",
    )
    defaults.update(overrides)
    return MiniReviewerSettings(**defaults)


_OLLAMA_MINI_JSON = {
    "result_code": "PATCH_PROPOSED",
    "summary": "락 없이 공유 상태를 변경합니다.",
    "confidence_score": 0.8,
    "details": [{"line_number": 12, "issue": "race condition 위험"}],
    "suggested_patch": "--- a\n+++ b\n",
}


# ---------------------------------------------------------------------------
# (a) Ollama → mini schema 어댑트 (HTTP mock)
# ---------------------------------------------------------------------------

class TestOllamaMiniAdapter:
    def _provider(self) -> OllamaProvider:
        return OllamaProvider(model="qwen2.5-coder:7b", base_url="http://localhost:11434")

    def test_generate_mini_returns_parsed_dict(self) -> None:
        provider = self._provider()
        with patch.object(provider, "available", return_value=True):
            with patch.object(
                provider, "_http_post_streaming",
                return_value=json.dumps(_OLLAMA_MINI_JSON),
            ):
                result = provider.generate_mini("sys", "user", mini_reviewer.OLLAMA_RESPONSE_FORMAT)
        assert result["result_code"] == "PATCH_PROPOSED"
        assert result["details"][0]["line_number"] == 12

    def test_generate_mini_format_and_prompt_passed_through(self) -> None:
        provider = self._provider()
        captured: dict = {}

        def _fake_post(url, body):
            captured["url"] = url
            captured["body"] = body
            return json.dumps(_OLLAMA_MINI_JSON)

        with patch.object(provider, "available", return_value=True):
            with patch.object(provider, "_http_post_streaming", side_effect=_fake_post):
                provider.generate_mini("SYSPROMPT", "USERPROMPT", mini_reviewer.OLLAMA_RESPONSE_FORMAT)
        # format이 JSON Schema(dict)로 강제되었는지 + 시스템/유저 프롬프트가 결합됐는지
        assert captured["body"]["format"] == mini_reviewer.OLLAMA_RESPONSE_FORMAT
        assert "SYSPROMPT" in captured["body"]["prompt"]
        assert "USERPROMPT" in captured["body"]["prompt"]
        assert captured["body"]["options"]["temperature"] == 0.0
        assert captured["url"].endswith("/api/generate")

    def test_generate_review_ollama_branch_adapts_to_mini_schema(self) -> None:
        """generate_review가 provider=ollama에서 mini payload(target_file/reviewer 포함)를 산출."""
        settings = _settings()

        def _fake_post(url, body):
            return json.dumps(_OLLAMA_MINI_JSON)

        with patch.object(mini_reviewer, "diff_for", return_value="+some diff"):
            with patch.object(OllamaProvider, "available", return_value=True):
                with patch.object(OllamaProvider, "_http_post_streaming", side_effect=_fake_post):
                    result = generate_review("src/a.py", staged=False, settings=settings)
        assert result is not None
        assert result["result_code"] == "PATCH_PROPOSED"
        assert result["target_file"] == "src/a.py"
        assert result["reviewer"].startswith("Ollama/")
        # mini RESPONSE_SCHEMA 필드 전부 존재
        for key in ("result_code", "summary", "confidence_score", "details", "suggested_patch"):
            assert key in result

    def test_invalid_result_code_from_ollama_raises_not_pass(self) -> None:
        """모델이 enum 밖 result_code를 내면 RuntimeError (PASS 둔갑 금지)."""
        bad = {**_OLLAMA_MINI_JSON, "result_code": "LGTM"}
        settings = _settings()
        with patch.object(mini_reviewer, "diff_for", return_value="+some diff"):
            with patch.object(OllamaProvider, "available", return_value=True):
                with patch.object(
                    OllamaProvider, "_http_post_streaming", return_value=json.dumps(bad)
                ):
                    with pytest.raises(RuntimeError, match="유효하지 않은 result_code"):
                        generate_review("src/a.py", staged=False, settings=settings)


# ---------------------------------------------------------------------------
# (b) load_settings provider 선택 + gemini-key-optional
# ---------------------------------------------------------------------------

class TestLoadSettingsProviderSelection:
    def _cfg(self, tmp_path: Path, **overrides) -> Path:
        data = {
            "gemini_model": "gemini-test",
            "watch_path": "src",
            "debounce_seconds": 1.5,
            "file_extensions": [".py"],
            **overrides,
        }
        cfg = tmp_path / "nitpicker.local.json"
        cfg.write_text(json.dumps(data), encoding="utf-8")
        return cfg

    def test_default_provider_is_ollama(self, tmp_path: Path) -> None:
        cfg = self._cfg(tmp_path)  # provider 미지정
        env = {k: v for k, v in os.environ.items() if k not in ("NITPICKER_PROVIDER", "GEMINI_API_KEY")}
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, env, clear=True):
                settings = load_settings()
        assert settings.provider == "ollama"
        assert settings.ollama_model == "qwen2.5-coder:7b"
        assert settings.ollama_base_url == "http://localhost:11434"

    def test_ollama_provider_without_gemini_key_succeeds(self, tmp_path: Path) -> None:
        """provider=ollama이면 Gemini 키/모델이 없어도 load_settings 성공(D5)."""
        cfg = self._cfg(tmp_path, provider="ollama", gemini_api_key="", gemini_model="")
        env = {k: v for k, v in os.environ.items() if k not in ("NITPICKER_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL")}
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, env, clear=True):
                settings = load_settings()
        assert settings.provider == "ollama"
        assert settings.gemini_api_key == ""

    def test_gemini_provider_still_requires_key(self, tmp_path: Path) -> None:
        cfg = self._cfg(tmp_path, provider="gemini", gemini_api_key="")
        env = {k: v for k, v in os.environ.items() if k not in ("NITPICKER_PROVIDER", "GEMINI_API_KEY")}
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, env, clear=True):
                with pytest.raises(RuntimeError, match="missing Gemini API key"):
                    load_settings()

    def test_env_provider_overrides_config(self, tmp_path: Path) -> None:
        cfg = self._cfg(tmp_path, provider="gemini", gemini_api_key="k")
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, {"NITPICKER_PROVIDER": "ollama"}, clear=False):
                settings = load_settings()
        assert settings.provider == "ollama"

    def test_invalid_provider_raises(self, tmp_path: Path) -> None:
        cfg = self._cfg(tmp_path, provider="openai")
        env = {k: v for k, v in os.environ.items() if k != "NITPICKER_PROVIDER"}
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, env, clear=True):
                with pytest.raises(RuntimeError, match="invalid provider"):
                    load_settings()

    def test_ollama_model_env_override(self, tmp_path: Path) -> None:
        cfg = self._cfg(tmp_path, provider="ollama", ollama_model="qwen2.5-coder:7b")
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, {"OLLAMA_MODEL": "qwen3:8b"}, clear=False):
                settings = load_settings()
        assert settings.ollama_model == "qwen3:8b"

    def test_empty_ollama_model_raises(self, tmp_path: Path) -> None:
        cfg = self._cfg(tmp_path, provider="ollama", ollama_model="bad model")
        env = {k: v for k, v in os.environ.items() if k not in ("NITPICKER_PROVIDER", "OLLAMA_MODEL")}
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, env, clear=True):
                with pytest.raises(RuntimeError, match="invalid ollama_model"):
                    load_settings()


# ---------------------------------------------------------------------------
# (c) silent-PASS 금지(D4) — provider 에러 → 예외 전파(REVIEW_PASSED 아님) + exit2
# ---------------------------------------------------------------------------

class TestNoSilentPass:
    def test_ollama_unavailable_raises_not_pass(self) -> None:
        settings = _settings()
        with patch.object(mini_reviewer, "diff_for", return_value="+some diff"):
            with patch.object(OllamaProvider, "available", return_value=False):
                with pytest.raises(RuntimeError, match="미가용"):
                    generate_review("src/a.py", staged=False, settings=settings)

    def test_ollama_timeout_propagates_not_pass(self) -> None:
        settings = _settings()
        with patch.object(mini_reviewer, "diff_for", return_value="+some diff"):
            with patch.object(OllamaProvider, "available", return_value=True):
                with patch.object(
                    OllamaProvider, "_http_post_streaming",
                    side_effect=TimeoutError("deadline 초과"),
                ):
                    with pytest.raises(TimeoutError):
                        generate_review("src/a.py", staged=False, settings=settings)

    def test_ollama_bad_json_raises_not_pass(self) -> None:
        settings = _settings()
        with patch.object(mini_reviewer, "diff_for", return_value="+some diff"):
            with patch.object(OllamaProvider, "available", return_value=True):
                with patch.object(
                    OllamaProvider, "_http_post_streaming",
                    return_value="not valid json {{{",
                ):
                    with pytest.raises(RuntimeError, match="JSON 파싱 실패"):
                        generate_review("src/a.py", staged=False, settings=settings)

    def test_generate_mini_never_calls_fallback_response(self) -> None:
        """generate_mini 경로가 _fallback_response(status=PASS)를 절대 호출하지 않음을 보장."""
        provider = OllamaProvider(model="m", base_url="http://localhost:11434")
        with patch.object(provider, "available", return_value=False):
            with patch.object(provider, "_fallback_response") as fallback:
                with pytest.raises(RuntimeError):
                    provider.generate_mini("s", "u", mini_reviewer.OLLAMA_RESPONSE_FORMAT)
        fallback.assert_not_called()

    def test_mini_nitpicker_main_returns_exit2_on_provider_error(self) -> None:
        """CLI 배선 회귀: review 중 provider 에러(예외) → mini_nitpicker.main() exit 2.

        bin/mini_nitpicker.py를 import해 main()을 직접 호출하고, review_targets가
        RuntimeError(Ollama 미가용 등)를 던질 때 exit code가 **2**(에러)임을 확인한다.
        REVIEW_PASSED(=exit 0)로 둔갑하지 않는다 = 게이트 false-green 차단.
        """
        bin_dir = ROOT / "bin"
        if str(bin_dir) not in sys.path:
            sys.path.insert(0, str(bin_dir))
        import mini_nitpicker as cli  # type: ignore

        argv = ["mini_nitpicker.py", "src/a.py"]
        with patch.object(sys, "argv", argv):
            with patch.object(cli, "load_settings", return_value=_settings()):
                with patch.object(
                    cli, "review_targets",
                    side_effect=RuntimeError("Ollama 서버 미가용 또는 모델 미설치"),
                ):
                    exit_code = cli.main()
        assert exit_code == 2

    def test_mini_nitpicker_main_passed_review_exit0(self) -> None:
        """대조군: 모든 리뷰가 REVIEW_PASSED면 exit 0."""
        bin_dir = ROOT / "bin"
        if str(bin_dir) not in sys.path:
            sys.path.insert(0, str(bin_dir))
        import mini_nitpicker as cli  # type: ignore

        passed = {
            "result_code": "REVIEW_PASSED",
            "summary": "ok",
            "confidence_score": 1.0,
            "details": [],
            "suggested_patch": None,
            "target_file": "src/a.py",
        }
        argv = ["mini_nitpicker.py", "src/a.py"]
        with patch.object(sys, "argv", argv):
            with patch.object(cli, "load_settings", return_value=_settings()):
                with patch.object(cli, "review_targets", return_value=[passed]):
                    exit_code = cli.main()
        assert exit_code == 0

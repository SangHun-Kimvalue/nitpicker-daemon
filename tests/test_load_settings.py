"""Edge-case unit tests for load_settings() in mini_reviewer.py.

Covers:
- Missing config file: no env var → RuntimeError
- Missing config file: GEMINI_API_KEY env var → succeeds
- Missing config file: NITPICKER_SKIP=1 → bypasses key/model validation
- Empty api_key in config, no env var → RuntimeError
- Model name: empty string → RuntimeError
- Model name: contains internal spaces → RuntimeError
- Model name: leading/trailing spaces → stripped and accepted
- NITPICKER_SKIP=1: bypasses both key and model validation
- GEMINI_API_KEY env var overrides config value
- GEMINI_MODEL env var overrides config value
- file_extensions are normalized to lowercase
- debounce_seconds is cast to float
- Default values when config exists but omits optional keys
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from jemmin import mini_reviewer
from jemmin.mini_reviewer import load_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_config(tmp_path: Path, **overrides) -> Path:
    """Write a minimal valid JSON config to tmp_path and return its Path."""
    data = {
        "gemini_api_key": "test-key",
        "gemini_model": "gemini-test",
        "watch_path": "src",
        "debounce_seconds": 1.5,
        "file_extensions": [".py"],
        **overrides,
    }
    cfg = tmp_path / "nitpicker.local.json"
    cfg.write_text(json.dumps(data), encoding="utf-8")
    return cfg


# ---------------------------------------------------------------------------
# Missing config file
# ---------------------------------------------------------------------------

class TestLoadSettingsMissingConfig:
    def test_missing_config_without_env_key_raises(self, tmp_path: Path) -> None:
        # provider=gemini일 때만 Gemini 키가 필수다(D5). 기본 ollama에선 완화되므로
        # 이 검증을 트리거하려면 NITPICKER_PROVIDER=gemini를 명시한다.
        nonexistent = tmp_path / "does_not_exist.json"
        env = {k: v for k, v in os.environ.items() if k not in ("GEMINI_API_KEY", "GEMINI_MODEL", "NITPICKER_SKIP")}
        env["NITPICKER_PROVIDER"] = "gemini"
        with patch.object(mini_reviewer, "CONFIG_PATH", nonexistent):
            with patch.dict(os.environ, env, clear=True):
                with pytest.raises(RuntimeError, match="missing Gemini API key"):
                    load_settings()

    def test_missing_config_with_env_key_succeeds(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "does_not_exist.json"
        env_override = {"GEMINI_API_KEY": "env-key"}
        with patch.object(mini_reviewer, "CONFIG_PATH", nonexistent):
            with patch.dict(os.environ, env_override, clear=False):
                settings = load_settings()
        assert settings.gemini_api_key == "env-key"
        assert settings.gemini_model == "gemini-3.1-pro-preview"
        assert settings.gemini_fallback_model == "gemini-2.0-flash"
        assert settings.skip is False

    def test_missing_config_skip_1_bypasses_key_check(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "does_not_exist.json"
        with patch.object(mini_reviewer, "CONFIG_PATH", nonexistent):
            with patch.dict(os.environ, {"NITPICKER_SKIP": "1"}, clear=False):
                settings = load_settings()
        assert settings.skip is True
        assert settings.gemini_api_key == ""


# ---------------------------------------------------------------------------
# API key validation
# ---------------------------------------------------------------------------

class TestLoadSettingsApiKeyValidation:
    def test_empty_api_key_in_config_raises(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path, gemini_api_key="", provider="gemini")
        env = {k: v for k, v in os.environ.items() if k not in ("GEMINI_API_KEY", "NITPICKER_SKIP")}
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, env, clear=True):
                with pytest.raises(RuntimeError, match="missing Gemini API key"):
                    load_settings()

    def test_env_key_overrides_empty_config_key(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path, gemini_api_key="")
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "env-override"}, clear=False):
                settings = load_settings()
        assert settings.gemini_api_key == "env-override"


# ---------------------------------------------------------------------------
# Model name validation
# ---------------------------------------------------------------------------

class TestLoadSettingsModelValidation:
    def test_empty_model_name_raises(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path, gemini_model="", provider="gemini")
        env = {k: v for k, v in os.environ.items() if k not in ("GEMINI_MODEL", "NITPICKER_SKIP")}
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, env, clear=True):
                with pytest.raises(RuntimeError, match="invalid gemini_model"):
                    load_settings()

    def test_model_with_internal_spaces_raises(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path, gemini_model="gemini 2.0 flash", provider="gemini")
        env = {k: v for k, v in os.environ.items() if k not in ("GEMINI_MODEL", "NITPICKER_SKIP")}
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, env, clear=True):
                with pytest.raises(RuntimeError, match="invalid gemini_model"):
                    load_settings()

    def test_model_with_leading_trailing_spaces_is_stripped_and_accepted(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path, gemini_model="  gemini-test  ")
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            settings = load_settings()
        assert settings.gemini_model == "gemini-test"

    def test_nitpicker_skip_bypasses_empty_model_validation(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path, gemini_model="")
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, {"NITPICKER_SKIP": "1"}, clear=False):
                settings = load_settings()
        assert settings.skip is True

    def test_nitpicker_skip_bypasses_spaced_model_validation(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path, gemini_model="bad model name")
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, {"NITPICKER_SKIP": "1"}, clear=False):
                settings = load_settings()
        assert settings.skip is True

    def test_env_model_overrides_invalid_config_model(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path, gemini_model="")
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, {"GEMINI_MODEL": "gemini-env-model"}, clear=False):
                settings = load_settings()
        assert settings.gemini_model == "gemini-env-model"

    def test_env_fallback_model_overrides_config_value(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path, gemini_fallback_model="gemini-config-fallback")
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, {"GEMINI_FALLBACK_MODEL": "gemini-env-fallback"}, clear=False):
                settings = load_settings()
        assert settings.gemini_fallback_model == "gemini-env-fallback"


# ---------------------------------------------------------------------------
# Env var overrides
# ---------------------------------------------------------------------------

class TestLoadSettingsEnvVarOverrides:
    def test_gemini_api_key_env_takes_priority_over_config(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path, gemini_api_key="config-key")
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "env-key"}, clear=False):
                settings = load_settings()
        assert settings.gemini_api_key == "env-key"

    def test_gemini_model_env_takes_priority_over_config(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path, gemini_model="config-model")
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, {"GEMINI_MODEL": "env-model"}, clear=False):
                settings = load_settings()
        assert settings.gemini_model == "env-model"

    def test_nitpicker_skip_0_does_not_set_skip(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path)
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, {"NITPICKER_SKIP": "0"}, clear=False):
                settings = load_settings()
        assert settings.skip is False

    def test_nitpicker_skip_1_sets_skip(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path)
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, {"NITPICKER_SKIP": "1"}, clear=False):
                settings = load_settings()
        assert settings.skip is True


# ---------------------------------------------------------------------------
# Defaults and normalization
# ---------------------------------------------------------------------------

class TestLoadSettingsDefaultsAndNormalization:
    def test_file_extensions_are_lowercased(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path, file_extensions=[".PY", ".CPP", ".H"])
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            settings = load_settings()
        assert settings.file_extensions == (".py", ".cpp", ".h")

    def test_debounce_seconds_cast_to_float(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path, debounce_seconds=5)
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            settings = load_settings()
        assert isinstance(settings.debounce_seconds, float)
        assert settings.debounce_seconds == 5.0

    def test_default_watch_path_is_src(self, tmp_path: Path) -> None:
        data = {"gemini_api_key": "k", "gemini_model": "m"}
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps(data), encoding="utf-8")
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            settings = load_settings()
        assert settings.watch_path == "src"

    def test_default_file_extensions_when_omitted(self, tmp_path: Path) -> None:
        data = {"gemini_api_key": "k", "gemini_model": "m"}
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps(data), encoding="utf-8")
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            settings = load_settings()
        assert ".py" in settings.file_extensions
        assert ".cpp" in settings.file_extensions

    def test_default_fallback_model_when_omitted(self, tmp_path: Path) -> None:
        data = {"gemini_api_key": "k", "gemini_model": "m"}
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps(data), encoding="utf-8")
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            settings = load_settings()
        assert settings.gemini_fallback_model == "gemini-2.0-flash"

    def test_duplicate_fallback_model_is_deduplicated(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path, gemini_model="gemini-same", gemini_fallback_model="gemini-same")
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            settings = load_settings()
        assert settings.gemini_fallback_model == ""


# ---------------------------------------------------------------------------
# JSON skip field
# ---------------------------------------------------------------------------

class TestLoadSettingsJsonSkip:
    def test_json_skip_true_sets_skip(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path, skip=True)
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, {}, clear=False):
                settings = load_settings()
        assert settings.skip is True

    def test_json_skip_false_does_not_skip(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path, skip=False)
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            settings = load_settings()
        assert settings.skip is False

    def test_json_skip_true_bypasses_key_validation(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path, gemini_api_key="", skip=True)
        env = {k: v for k, v in os.environ.items() if k not in ("GEMINI_API_KEY", "NITPICKER_SKIP")}
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, env, clear=True):
                settings = load_settings()
        assert settings.skip is True

    def test_env_skip_overrides_json_skip_false(self, tmp_path: Path) -> None:
        """NITPICKER_SKIP=1 env var forces skip even if config says false."""
        cfg = _minimal_config(tmp_path, skip=False)
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, {"NITPICKER_SKIP": "1"}, clear=False):
                settings = load_settings()
        assert settings.skip is True

    def test_json_skip_true_overrides_no_env_var(self, tmp_path: Path) -> None:
        """JSON skip:true activates skip even without NITPICKER_SKIP env var."""
        cfg = _minimal_config(tmp_path, skip=True)
        env = {k: v for k, v in os.environ.items() if k != "NITPICKER_SKIP"}
        with patch.object(mini_reviewer, "CONFIG_PATH", cfg):
            with patch.dict(os.environ, env, clear=True):
                settings = load_settings()
        assert settings.skip is True

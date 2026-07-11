"""Edge-case unit tests for generate_review() in mini_reviewer.py.

Covers:
- empty diff → returns None (no Gemini call)
- skip setting → returns None immediately  
- google-genai not installed → RuntimeError propagated
- Gemini API errors: NOT_FOUND, quota exhausted, auth failure, generic
- Response parsing: parsed dict, model_dump(), raw JSON text, empty response
- target_file injected into payload regardless of Gemini response content
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from jemmin import mini_reviewer
from jemmin.mini_reviewer import MiniReviewerSettings, generate_review


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings(**overrides) -> MiniReviewerSettings:
    defaults = dict(
        gemini_api_key="demo-key",
        gemini_model="gemini-test",
        watch_path="src",
        debounce_seconds=0.1,
        file_extensions=(".py",),
        skip=False,
        # 이 모듈은 Gemini 경로(genai client)를 검증한다. 기본 provider가
        # ollama로 바뀌었으므로(D2) 명시적으로 gemini 경로를 선택한다.
        provider="gemini",
    )
    defaults.update(overrides)
    return MiniReviewerSettings(**defaults)


def _fake_response(*, parsed=None, text: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(parsed=parsed, text=text)


_VALID_PAYLOAD = {
    "result_code": "REVIEW_PASSED",
    "summary": "All good",
    "confidence_score": 1.0,
    "details": [],
    "suggested_patch": None,
}


# ---------------------------------------------------------------------------
# Early-exit paths (no Gemini call)
# ---------------------------------------------------------------------------

class TestGenerateReviewEarlyExit:
    def test_returns_none_when_diff_is_empty(self) -> None:
        with patch.object(mini_reviewer, "diff_for", return_value=""):
            result = generate_review("src/a.py", staged=False, settings=_settings())
        assert result is None

    def test_returns_none_when_skip_is_enabled(self) -> None:
        with patch.object(mini_reviewer, "diff_for", return_value="+some diff"):
            result = generate_review("src/a.py", staged=False, settings=_settings(skip=True))
        assert result is None

    def test_diff_whitespace_only_proceeds_to_api(self) -> None:
        """Only a truly empty string short-circuits; whitespace-only diffs reach the API."""
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = _fake_response(parsed=_VALID_PAYLOAD)
        with patch.object(mini_reviewer, "diff_for", return_value="   \n  "):
            with patch.object(mini_reviewer, "_build_client", return_value=mock_client):
                result = generate_review("src/a.py", staged=False, settings=_settings())
        mock_client.models.generate_content.assert_called_once()
        assert result is not None


# ---------------------------------------------------------------------------
# Module not installed
# ---------------------------------------------------------------------------

class TestGenerateReviewMissingDependency:
    def test_raises_when_google_genai_not_installed(self) -> None:
        with patch.object(mini_reviewer, "diff_for", return_value="+some diff"):
            with patch.object(mini_reviewer, "_HAS_GENAI", False):
                with pytest.raises(RuntimeError, match="google-genai is not installed"):
                    generate_review("src/a.py", staged=False, settings=_settings())

    def test_build_client_raises_when_has_genai_false(self) -> None:
        with patch.object(mini_reviewer, "_HAS_GENAI", False):
            with pytest.raises(RuntimeError, match="google-genai is not installed"):
                mini_reviewer._build_client("any-key")


# ---------------------------------------------------------------------------
# Gemini API errors
# ---------------------------------------------------------------------------

class TestGenerateReviewApiErrors:
    def _call(self, exc: Exception) -> None:
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = exc
        with patch.object(mini_reviewer, "diff_for", return_value="+some diff"):
            with patch.object(mini_reviewer, "_build_client", return_value=mock_client):
                generate_review("src/a.py", staged=False, settings=_settings())

    def test_not_found_model_raises_runtime_error(self) -> None:
        with pytest.raises(RuntimeError, match="not available for generateContent"):
            self._call(Exception("Model NOT_FOUND"))

    def test_not_found_primary_model_retries_with_fallback(self) -> None:
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = [
            Exception("Model NOT_FOUND"),
            _fake_response(parsed=_VALID_PAYLOAD),
        ]
        settings = _settings(gemini_model="gemini-preview", gemini_fallback_model="gemini-stable")
        with patch.object(mini_reviewer, "diff_for", return_value="+some diff"):
            with patch.object(mini_reviewer, "_build_client", return_value=mock_client):
                result = generate_review("src/a.py", staged=False, settings=settings)
        assert result is not None
        assert result["target_file"] == "src/a.py"
        assert mock_client.models.generate_content.call_count == 2
        assert mock_client.models.generate_content.call_args_list[0].kwargs["model"] == "gemini-preview"
        assert mock_client.models.generate_content.call_args_list[1].kwargs["model"] == "gemini-stable"

    def test_quota_exhausted_raises_runtime_error(self) -> None:
        with pytest.raises(RuntimeError, match="quota exhausted"):
            self._call(Exception("RESOURCE_EXHAUSTED quota exceeded"))

    def test_auth_failure_raises_runtime_error(self) -> None:
        with pytest.raises(RuntimeError, match="authentication failed"):
            self._call(Exception("Invalid API key authentication"))

    def test_generic_error_propagates_unchanged(self) -> None:
        with pytest.raises(Exception, match="some unexpected error"):
            self._call(Exception("some unexpected error"))


# ---------------------------------------------------------------------------
# Response parsing via _normalize_response_payload
# ---------------------------------------------------------------------------

class TestNormalizeResponsePayload:
    def _normalize(self, response) -> dict:
        return mini_reviewer._normalize_response_payload(response)

    def test_parsed_dict_returned_directly(self) -> None:
        response = _fake_response(parsed=_VALID_PAYLOAD)
        result = self._normalize(response)
        assert result == _VALID_PAYLOAD

    def test_parsed_model_dump_object_is_expanded(self) -> None:
        pydantic_obj = MagicMock()
        pydantic_obj.model_dump.return_value = _VALID_PAYLOAD
        response = _fake_response(parsed=pydantic_obj)
        result = self._normalize(response)
        assert result == _VALID_PAYLOAD

    def test_raw_json_text_is_parsed(self) -> None:
        response = _fake_response(text=json.dumps(_VALID_PAYLOAD))
        result = self._normalize(response)
        assert result == _VALID_PAYLOAD

    def test_whitespace_only_text_raises(self) -> None:
        response = _fake_response(text="   ")
        with pytest.raises(RuntimeError, match="empty response body"):
            self._normalize(response)

    def test_empty_parsed_and_no_text_raises(self) -> None:
        response = _fake_response(parsed=None, text=None)
        with pytest.raises(RuntimeError, match="empty response body"):
            self._normalize(response)

    def test_invalid_json_text_raises_json_decode_error(self) -> None:
        response = _fake_response(text="not valid json {{{")
        with pytest.raises(json.JSONDecodeError):
            self._normalize(response)


# ---------------------------------------------------------------------------
# Full generate_review flow: target_file injection
# ---------------------------------------------------------------------------

class TestGenerateReviewTargetFileInjection:
    def test_target_file_key_is_always_set(self) -> None:
        payload_without_target = dict(_VALID_PAYLOAD)
        payload_without_target.pop("result_code")  # partial payload still ok

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = _fake_response(parsed=payload_without_target)

        with patch.object(mini_reviewer, "diff_for", return_value="+some diff"):
            with patch.object(mini_reviewer, "_build_client", return_value=mock_client):
                result = generate_review("src/target.py", staged=False, settings=_settings())

        assert result is not None
        assert result["target_file"] == "src/target.py"

    def test_target_file_overrides_gemini_response_value(self) -> None:
        payload_with_wrong_target = {**_VALID_PAYLOAD, "target_file": "wrong/path.py"}

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = _fake_response(parsed=payload_with_wrong_target)

        with patch.object(mini_reviewer, "diff_for", return_value="+some diff"):
            with patch.object(mini_reviewer, "_build_client", return_value=mock_client):
                result = generate_review("src/correct.py", staged=False, settings=_settings())

        assert result is not None
        assert result["target_file"] == "src/correct.py"

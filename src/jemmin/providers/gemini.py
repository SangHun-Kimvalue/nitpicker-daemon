"""Gemini LLM 공급자 — google-genai SDK와 Context Cache API를 사용합니다.

google-genai가 설치되지 않은 환경에서는 MockLocalLLMProvider로 폴백합니다.
"""
from __future__ import annotations

import os
from typing import Any

try:
    from google import genai
    from google.genai import types as genai_types
    _HAS_GENAI = True
except ModuleNotFoundError:  # pragma: no cover
    _HAS_GENAI = False

from jemmin.providers.base import ProviderRequest
from jemmin.providers.local_llm import MockLocalLLMProvider

_DEFAULT_MODEL = "gemini-3.1-pro-preview"
_DEFAULT_FALLBACK_MODEL = "gemini-2.0-flash"


def _load_config() -> dict[str, Any]:
    try:
        import yaml
        config_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "config", "reviewer_config.yaml")
        config_path = os.path.normpath(config_path)
        if os.path.exists(config_path):
            with open(config_path, encoding="utf-8") as file_obj:
                return yaml.safe_load(file_obj) or {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _resolve_model(explicit: str | None) -> str:
    """모델명 결정 우선순위: 명시적 인자 > JEMMIN_MODEL 환경변수 > config > 기본값."""
    if explicit:
        return explicit
    env_model = os.environ.get("JEMMIN_MODEL", "").strip()
    if env_model:
        return env_model
    cfg = _load_config()
    provider_cfg = cfg.get("provider") or {}
    if cfg.get("model"):
        return str(cfg["model"]).strip()
    if provider_cfg.get("gemini_model"):
        return str(provider_cfg["gemini_model"]).strip()
    return _DEFAULT_MODEL


def _resolve_fallback_model(explicit: str | None, primary_model: str) -> str:
    if explicit:
        return "" if explicit == primary_model else explicit
    env_model = os.environ.get("JEMMIN_FALLBACK_MODEL", "").strip()
    if env_model:
        return "" if env_model == primary_model else env_model
    cfg = _load_config()
    provider_cfg = cfg.get("provider") or {}
    configured = str(
        cfg.get("model_fallback")
        or provider_cfg.get("gemini_fallback_model")
        or _DEFAULT_FALLBACK_MODEL
    ).strip()
    return "" if configured == primary_model else configured


def _is_missing_model_error(message: str) -> bool:
    lowered = message.lower()
    return "not_found" in lowered or "is not found" in lowered or "listmodels" in lowered


class GeminiProvider(MockLocalLLMProvider):
    """google-genai SDK 기반 Gemini LLM 공급자.

    모델명 우선순위: 명시적 인자 > JEMMIN_MODEL 환경변수 > config/reviewer_config.yaml > 기본값.
    API 키는 환경변수 GEMINI_API_KEY 또는 생성자 인자로 전달합니다.
    google-genai가 없으면 MockLocalLLMProvider의 동작으로 폴백합니다.
    """

    name = "gemini"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self._model = _resolve_model(model)
        self._fallback_model = _resolve_fallback_model(fallback_model, self._model)

    def available(self) -> bool:
        return _HAS_GENAI and bool(self._api_key)

    def generate(self, request: ProviderRequest) -> dict[str, Any]:
        if not self.available():
            return super().generate(request)
        client = self._client()
        try:
            response = self._with_model_fallback(
                lambda model_name: client.models.generate_content(
                    model=model_name,
                    contents=request.user_prompt,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=request.system_prompt,
                        response_mime_type="application/json",
                        temperature=0.0,
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001
            return {"status": "ERROR", "reason": str(exc), "patch_code": ""}
        raw = response.text or "{}"
        import json
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return {"status": "PASS", "reason": raw, "patch_code": ""}

    def generate_with_cache(
        self,
        cache_id: str,
        tier1_prompt: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """캐시된 Tier2/3 컨텍스트 + Tier1 diff만 전송해 토큰 비용을 절감합니다."""
        if not self.available():
            return super().generate_with_cache(cache_id, tier1_prompt, metadata)
        client = self._client()
        try:
            response = self._with_model_fallback(
                lambda model_name: client.models.generate_content(
                    model=model_name,
                    contents=tier1_prompt,
                    config=genai_types.GenerateContentConfig(
                        cached_content=cache_id,
                        response_mime_type="application/json",
                        temperature=0.0,
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001
            return {"status": "ERROR", "reason": str(exc), "patch_code": ""}
        raw = response.text or "{}"
        import json
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return {"status": "PASS", "reason": raw, "patch_code": ""}

    def create_context_cache(
        self,
        system_prompt: str,
        static_context: str,
        ttl_seconds: int = 3600,
    ) -> str:
        """Tier2/3 컨텍스트를 Gemini Context Cache API에 업로드하고 cache_id를 반환합니다."""
        if not self.available():
            return super().create_context_cache(system_prompt, static_context, ttl_seconds)
        client = self._client()
        cache = self._with_model_fallback(
            lambda model_name: client.caches.create(
                model=model_name,
                config=genai_types.CreateCachedContentConfig(
                    system_instruction=system_prompt,
                    contents=[static_context],
                    ttl=f"{ttl_seconds}s",
                ),
            )
        )
        return cache.name  # e.g. "cachedContents/abc123"

    def delete_context_cache(self, cache_id: str) -> None:
        if not self.available():
            return super().delete_context_cache(cache_id)
        client = self._client()
        try:
            client.caches.delete(name=cache_id)
        except Exception:  # noqa: BLE001
            pass  # 이미 만료된 캐시는 무시

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _client(self) -> Any:
        if not _HAS_GENAI:  # pragma: no cover
            raise RuntimeError("google-genai is not installed")
        return genai.Client(api_key=self._api_key)

    def _with_model_fallback(self, operation):
        last_error: Exception | None = None
        candidate_models = [self._model]
        if self._fallback_model and self._fallback_model not in candidate_models:
            candidate_models.append(self._fallback_model)
        for index, model_name in enumerate(candidate_models):
            try:
                return operation(model_name)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if _is_missing_model_error(str(exc)) and index + 1 < len(candidate_models):
                    continue
                raise
        assert last_error is not None
        raise last_error

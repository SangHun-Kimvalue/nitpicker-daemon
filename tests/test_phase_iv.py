"""Phase IV 테스트 — OllamaProvider, Tiered Routing, Provider Config.

OllamaProvider는 서버 미실행 시에도 안전하게 동작해야 합니다.
Tiered Routing은 cost_class별로 올바른 provider를 분배해야 합니다.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from jemmin.providers.base import ProviderRequest
from jemmin.providers.local_llm import MockLocalLLMProvider
from jemmin.providers.ollama import OllamaProvider
from jemmin.registry import (
    create_default_registry,
    create_tiered_registry,
    resolve_providers,
)
from jemmin.registry.agent_registry import AgentManifest


# ═══════════════════════════════════════════════════════════════════
# §1. OllamaProvider 단위 테스트
# ═══════════════════════════════════════════════════════════════════

class TestOllamaProvider(unittest.TestCase):
    """OllamaProvider — 서버 미실행 환경에서의 안전한 동작."""

    def test_default_model_and_url(self):
        p = OllamaProvider()
        self.assertEqual(p._model, "qwen2.5-coder:7b")
        self.assertEqual(p._base_url, "http://localhost:11434")

    def test_env_var_overrides(self):
        with patch.dict(os.environ, {
            "OLLAMA_MODEL": "llama3.1:8b",
            "OLLAMA_BASE_URL": "http://gpu-server:11434",
        }):
            p = OllamaProvider()
            self.assertEqual(p._model, "llama3.1:8b")
            self.assertEqual(p._base_url, "http://gpu-server:11434")

    def test_explicit_args_take_priority(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "should-be-ignored"}):
            p = OllamaProvider(model="deepseek-coder-v2", base_url="http://custom:1234")
            self.assertEqual(p._model, "deepseek-coder-v2")
            self.assertEqual(p._base_url, "http://custom:1234")

    def test_not_available_when_server_down(self):
        """서버 미실행 시 available()은 False."""
        p = OllamaProvider(base_url="http://localhost:99999")
        self.assertFalse(p.available())

    def test_fallback_response_when_unavailable(self):
        """서버 미실행 시 generate()는 fallback 응답 반환."""
        p = OllamaProvider(base_url="http://localhost:99999")
        req = ProviderRequest(
            prompt_pack_version="1.0",
            system_prompt="test",
            user_prompt="test",
            response_schema={},
        )
        result = p.generate(req)
        self.assertEqual(result["status"], "PASS")
        self.assertIn("ollama", result["reason"])

    def test_name_is_ollama(self):
        self.assertEqual(OllamaProvider.name, "ollama")

    def test_create_context_cache_returns_pseudo_id(self):
        p = OllamaProvider()
        cache_id = p.create_context_cache("sys", "ctx")
        self.assertTrue(cache_id.startswith("ollama-cache-"))

    def test_delete_context_cache_no_error(self):
        p = OllamaProvider()
        p.delete_context_cache("nonexistent")  # should not raise

    def test_reset_availability(self):
        p = OllamaProvider(base_url="http://localhost:99999")
        self.assertFalse(p.available())
        p.reset_availability()
        self.assertIsNone(p._available)

    def test_generate_with_cache_fallback(self):
        """generate_with_cache도 서버 다운 시 fallback."""
        p = OllamaProvider(base_url="http://localhost:99999")
        result = p.generate_with_cache("cache-1", "diff text")
        self.assertEqual(result["status"], "PASS")

    def test_build_prompt_format(self):
        p = OllamaProvider()
        prompt = p._build_prompt("system instruction", "user question")
        self.assertIn("[SYSTEM]", prompt)
        self.assertIn("[USER]", prompt)
        self.assertIn("system instruction", prompt)
        self.assertIn("user question", prompt)


# ═══════════════════════════════════════════════════════════════════
# §2. OllamaProvider Mock 서버 응답 테스트
# ═══════════════════════════════════════════════════════════════════

class TestOllamaProviderWithMockServer(unittest.TestCase):
    """Mock HTTP 응답을 사용한 OllamaProvider 테스트."""

    def _mock_available_provider(self):
        """available()이 True인 OllamaProvider 생성."""
        p = OllamaProvider()
        p._available = True  # force available
        return p

    @patch("jemmin.providers.ollama.urlopen")
    def test_generate_parses_json_response(self, mock_urlopen):
        """스트리밍 JSON 응답을 올바르게 파싱."""
        # Ollama stream=True 형식: 줄 단위 JSON 청크
        inner_json = json.dumps({
            "status": "FAIL",
            "reason": "security issue found",
            "patch_code": "fix = True",
        })
        lines = [
            json.dumps({"response": inner_json, "done": False}).encode("utf-8") + b"\n",
            json.dumps({"response": "", "done": True}).encode("utf-8") + b"\n",
        ]
        mock_response = MagicMock()
        mock_response.readline = MagicMock(side_effect=lines + [b""])
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        p = self._mock_available_provider()
        req = ProviderRequest(
            prompt_pack_version="1.0",
            system_prompt="You are a code reviewer.",
            user_prompt="Review this code.",
            response_schema={},
        )
        result = p.generate(req)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["reason"], "security issue found")

    @patch("jemmin.providers.ollama.urlopen")
    def test_generate_handles_non_json_response(self, mock_urlopen):
        """비-JSON 스트리밍 응답도 graceful 처리."""
        lines = [
            json.dumps({"response": "This is just plain text analysis", "done": False}).encode("utf-8") + b"\n",
            json.dumps({"response": "", "done": True}).encode("utf-8") + b"\n",
        ]
        mock_response = MagicMock()
        mock_response.readline = MagicMock(side_effect=lines + [b""])
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        p = self._mock_available_provider()
        req = ProviderRequest(
            prompt_pack_version="1.0",
            system_prompt="test",
            user_prompt="test",
            response_schema={},
        )
        result = p.generate(req)
        self.assertEqual(result["status"], "PASS")
        self.assertIn("plain text", result["reason"])

    @patch("jemmin.providers.ollama.urlopen")
    def test_generate_total_timeout_raises(self, mock_urlopen):
        """전체 데드라인 초과 시 TimeoutError → fallback PASS."""
        # total_timeout=0 으로 설정하면 첫 데드라인 체크에서 즉시 초과
        # (time.monotonic()은 실제 호출해도 0보다 항상 큼)
        mock_response = MagicMock()
        mock_response.readline = MagicMock(return_value=b"")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        p = self._mock_available_provider()
        p._total_timeout = 0  # 즉시 타임아웃
        req = ProviderRequest(
            prompt_pack_version="1.0",
            system_prompt="test",
            user_prompt="test",
            response_schema={},
        )
        result = p.generate(req)
        # TimeoutError가 catch되어 fallback PASS
        self.assertEqual(result["status"], "PASS")
        self.assertIn("timeout", result["reason"])

    @patch("jemmin.providers.ollama.urlopen")
    def test_streaming_collects_multiple_tokens(self, mock_urlopen):
        """스트리밍에서 여러 토큰이 올바르게 결합되는지 확인."""
        inner_json = json.dumps({
            "status": "PASS",
            "reason": "clean code",
            "patch_code": "",
        })
        # 토큰이 여러 청크로 나뉘어 수신
        tokens = list(inner_json)  # 한 글자씩
        lines = []
        for t in tokens:
            lines.append(
                json.dumps({"response": t, "done": False}).encode("utf-8") + b"\n"
            )
        lines.append(
            json.dumps({"response": "", "done": True}).encode("utf-8") + b"\n"
        )

        mock_response = MagicMock()
        mock_response.readline = MagicMock(side_effect=lines + [b""])
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        p = self._mock_available_provider()
        req = ProviderRequest(
            prompt_pack_version="1.0",
            system_prompt="test",
            user_prompt="test",
            response_schema={},
        )
        result = p.generate(req)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["reason"], "clean code")


# ═══════════════════════════════════════════════════════════════════
# §3. Tiered Routing
# ═══════════════════════════════════════════════════════════════════

class TestTieredRouting(unittest.TestCase):
    """create_tiered_registry()로 cost_class별 provider 분배."""

    def test_tiered_registry_assigns_different_providers(self):
        mock_provider = MockLocalLLMProvider()
        ollama_provider = MagicMock(name="ollama")

        registry = create_tiered_registry(
            providers={"free": None, "medium": ollama_provider},
            fallback_provider=mock_provider,
        )

        all_agents = registry.select(project_profile="general", trigger_intent="active_intent")
        self.assertEqual(len(all_agents), 10)

        # free 에이전트는 provider=None
        free_entry = registry.get("fast_gate")
        self.assertIsNone(free_entry.agent._provider)

        # medium 에이전트는 ollama_provider
        medium_entry = registry.get("patch")
        self.assertIs(medium_entry.agent._provider, ollama_provider)

    def test_tiered_registry_fallback_provider(self):
        """providers에 없는 cost_class는 fallback_provider를 사용."""
        fallback = MockLocalLLMProvider()
        registry = create_tiered_registry(
            providers={"free": None},
            fallback_provider=fallback,
        )

        # medium은 providers에 없으므로 fallback
        medium_entry = registry.get("patch")
        self.assertIs(medium_entry.agent._provider, fallback)

    def test_default_registry_still_works(self):
        """기존 create_default_registry 하위 호환."""
        registry = create_default_registry()
        all_agents = registry.select(project_profile="general", trigger_intent="active_intent")
        self.assertEqual(len(all_agents), 10)


# ═══════════════════════════════════════════════════════════════════
# §4. Provider Config Resolution
# ═══════════════════════════════════════════════════════════════════

class TestResolveProviders(unittest.TestCase):
    """resolve_providers()로 config에서 provider 인스턴스 생성."""

    def test_empty_config_returns_mock_default(self):
        result = resolve_providers({})
        self.assertIn("_default", result)
        self.assertIsInstance(result["_default"], MockLocalLLMProvider)

    def test_null_tier_returns_none(self):
        config = {
            "provider": {
                "default": "mock",
                "tiers": {"free": "null"},
            }
        }
        result = resolve_providers(config)
        self.assertIsNone(result["free"])

    def test_ollama_tier_creates_ollama_provider(self):
        config = {
            "provider": {
                "default": "mock",
                "ollama_model": "llama3.1:8b",
                "tiers": {"medium": "ollama"},
            }
        }
        result = resolve_providers(config)
        self.assertIsInstance(result["medium"], OllamaProvider)
        self.assertEqual(result["medium"]._model, "llama3.1:8b")

    def test_unknown_provider_falls_back_to_mock(self):
        config = {
            "provider": {
                "default": "mock",
                "tiers": {"high": "unknown_provider"},
            }
        }
        result = resolve_providers(config)
        self.assertIsInstance(result["high"], MockLocalLLMProvider)


# ═══════════════════════════════════════════════════════════════════
# §5. LLMProvider Protocol 호환성
# ═══════════════════════════════════════════════════════════════════

class TestProviderProtocolCompliance(unittest.TestCase):
    """모든 provider가 LLMProvider Protocol의 메서드를 구현하는지 확인."""

    _REQUIRED_METHODS = ["generate", "generate_with_cache", "create_context_cache",
                         "delete_context_cache", "available"]

    def test_ollama_provider_has_all_protocol_methods(self):
        p = OllamaProvider()
        for method in self._REQUIRED_METHODS:
            self.assertTrue(hasattr(p, method), f"OllamaProvider missing {method}")
            self.assertTrue(callable(getattr(p, method)), f"OllamaProvider.{method} not callable")
        self.assertTrue(hasattr(p, "name"))

    def test_mock_provider_has_all_protocol_methods(self):
        p = MockLocalLLMProvider()
        for method in self._REQUIRED_METHODS:
            self.assertTrue(hasattr(p, method), f"MockLocalLLMProvider missing {method}")

    def test_gemini_provider_has_all_protocol_methods(self):
        from jemmin.providers.gemini import GeminiProvider
        p = GeminiProvider()
        for method in self._REQUIRED_METHODS:
            self.assertTrue(hasattr(p, method), f"GeminiProvider missing {method}")


if __name__ == "__main__":
    unittest.main()

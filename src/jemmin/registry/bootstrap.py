"""Agent Registry Bootstrap — 모든 에이전트를 manifest와 함께 등록합니다.

create_default_registry(provider): 단일 provider로 모든 에이전트 등록.
create_tiered_registry(providers): cost_class별로 다른 provider를 분배하여 등록.

Tiered Routing 전략:
  - cost_class="free"   → 로컬 전용 (regex/AST/tool 기반, provider 미사용)
  - cost_class="low"    → 경량 로컬 LLM (Ollama: llama3.1:8b)
  - cost_class="medium" → 중간 로컬 LLM (Ollama: qwen2.5-coder:7b)
  - cost_class="high"   → 클라우드 LLM (Gemini Pro)
"""
from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)

from jemmin.agents.architecture import ArchitectureAgent
from jemmin.agents.context_agent import ContextAgent
from jemmin.agents.domain_rule import DomainRuleAgent
from jemmin.agents.fast_gate import FastGateAgent
from jemmin.agents.incident_triage import IncidentTriageAgent
from jemmin.agents.patch_agent import PatchAgent
from jemmin.agents.performance import PerformanceAgent
from jemmin.agents.security import SecurityAgent
from jemmin.agents.verification_agent import VerificationAgent
from jemmin.analyzers.ast_security import AstSecurityAnalyzer
from jemmin.registry.agent_registry import AgentManifest, AgentRegistry

# ── Manifest 정의 ──────────────────────────────────────────────

_MANIFESTS: list[tuple[AgentManifest, type]] = [
    (
        AgentManifest(
            name="fast_gate",
            capabilities={"lint", "type_check", "fast_path"},
            supported_profiles={"*"},
            supported_intents={"active_intent", "passive_save"},
            cost_class="free",
            blocking_level="gate",
        ),
        FastGateAgent,
    ),
    (
        AgentManifest(
            name="context",
            capabilities={"diff_quality", "context_check"},
            supported_profiles={"*"},
            supported_intents={"active_intent"},
            cost_class="free",
            blocking_level="warn",
        ),
        ContextAgent,
    ),
    (
        AgentManifest(
            name="domain_rule",
            capabilities={"domain_rule", "profile_rule"},
            supported_profiles={"*"},
            supported_intents={"active_intent"},
            cost_class="free",
            blocking_level="warn",
        ),
        DomainRuleAgent,
    ),
    (
        AgentManifest(
            name="architecture",
            capabilities={"architecture", "pattern_check"},
            supported_profiles={"*"},
            supported_intents={"active_intent", "passive_save"},
            cost_class="free",
            blocking_level="warn",
        ),
        ArchitectureAgent,
    ),
    (
        AgentManifest(
            name="security",
            capabilities={"security", "secret_scan"},
            supported_profiles={"*"},
            supported_intents={"active_intent", "passive_save"},
            cost_class="free",
            blocking_level="block",
        ),
        SecurityAgent,
    ),
    (
        AgentManifest(
            name="performance",
            capabilities={"performance", "anti_pattern"},
            supported_profiles={"*"},
            supported_intents={"active_intent"},
            cost_class="free",
            blocking_level="warn",
        ),
        PerformanceAgent,
    ),
    (
        AgentManifest(
            name="patch",
            capabilities={"patch_generation"},
            supported_profiles={"*"},
            supported_intents={"active_intent"},
            cost_class="medium",
            blocking_level="info",
        ),
        PatchAgent,
    ),
    (
        AgentManifest(
            name="verification",
            capabilities={"patch_verification"},
            supported_profiles={"*"},
            supported_intents={"active_intent"},
            cost_class="medium",
            blocking_level="info",
        ),
        VerificationAgent,
    ),
    (
        AgentManifest(
            name="incident_triage",
            capabilities={"incident", "triage"},
            supported_profiles={"*"},
            supported_intents={"active_intent"},
            cost_class="free",
            blocking_level="warn",
        ),
        IncidentTriageAgent,
    ),
    (
        AgentManifest(
            name="ast_security",
            capabilities={"security", "ast_analysis"},
            supported_profiles={"*"},
            supported_intents={"active_intent", "passive_save"},
            cost_class="free",
            blocking_level="block",
        ),
        AstSecurityAnalyzer,
    ),
]


def create_default_registry(provider: Any = None) -> AgentRegistry:
    """단일 provider로 모든 에이전트를 등록한 AgentRegistry를 반환합니다."""
    registry = AgentRegistry()
    for manifest, agent_cls in _MANIFESTS:
        registry.register(manifest, agent_cls(provider=provider))
    return registry


def create_tiered_registry(
    providers: dict[str, Any] | None = None,
    fallback_provider: Any = None,
) -> AgentRegistry:
    """cost_class별로 다른 provider를 분배하여 에이전트를 등록합니다.

    Args:
        providers: cost_class → provider 매핑.
            예: {"free": None, "low": ollama_light, "medium": ollama_heavy, "high": gemini}
        fallback_provider: providers에 매칭되는 키가 없을 때 사용할 기본 provider.

    Tiered Routing 전략:
        - free 에이전트 (fast_gate, security, architecture 등): regex/AST 기반 → provider 불필요
        - medium 에이전트 (patch, verification): LLM 추론 필요 → Ollama 또는 Gemini
        - high 에이전트 (향후 추가): 클라우드 전용

    Returns:
        cost_class별로 적절한 provider가 주입된 AgentRegistry.
    """
    providers = providers or {}
    registry = AgentRegistry()

    for manifest, agent_cls in _MANIFESTS:
        provider = providers.get(manifest.cost_class, fallback_provider)
        registry.register(manifest, agent_cls(provider=provider))
        if provider is not None:
            _logger.debug(
                "Agent '%s' (cost=%s) → provider '%s'",
                manifest.name,
                manifest.cost_class,
                getattr(provider, "name", type(provider).__name__),
            )

    return registry


def resolve_providers(
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """config에서 cost_class별 provider 인스턴스를 생성합니다.

    Config 예시 (reviewer_config.yaml):
        provider:
          default: ollama
          ollama_model: qwen2.5-coder:7b
          gemini_model: gemini-2.0-flash
          tiers:
            free: null
            medium: ollama
            high: gemini

    Returns:
        cost_class → provider 인스턴스 매핑.
    """
    from jemmin.providers.local_llm import MockLocalLLMProvider

    config = config or {}
    provider_config = config.get("provider", {})
    tier_config = provider_config.get("tiers", {})
    default_name = provider_config.get("default", "mock")

    # 필요한 provider만 사전 import (Gemini 리뷰 피드백: lazy import를 루프 밖으로)
    needed_providers = set(tier_config.values()) | {default_name}
    _OllamaProvider = None
    _GeminiProvider = None
    if "ollama" in needed_providers:
        from jemmin.providers.ollama import OllamaProvider as _OllamaProvider
    if "gemini" in needed_providers:
        from jemmin.providers.gemini import GeminiProvider as _GeminiProvider

    def _make_provider(name: str | None) -> Any:
        if name is None or name == "null":
            return None
        if name == "mock":
            return MockLocalLLMProvider()
        if name == "ollama" and _OllamaProvider:
            return _OllamaProvider(model=provider_config.get("ollama_model"))
        if name == "gemini" and _GeminiProvider:
            return _GeminiProvider(model=provider_config.get("gemini_model"))
        _logger.warning("Unknown provider '%s', falling back to mock", name)
        return MockLocalLLMProvider()

    result: dict[str, Any] = {}
    for cost_class, provider_name in tier_config.items():
        result[cost_class] = _make_provider(provider_name)

    # fallback_provider 용 default
    result.setdefault("_default", _make_provider(default_name))

    return result

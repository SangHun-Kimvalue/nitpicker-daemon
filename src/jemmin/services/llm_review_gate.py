"""L3 LLM Review Gate — L2 사전필터 통과 코드에 대한 LLM 최종 리뷰.

L2 sub-agents(AST/regex/tool)가 PASS한 코드에 한해서만 LLM을 호출하여
비즈니스 로직, 설계 결함, 엣지케이스 등을 검토합니다.

L2 REJECT → LLM 호출 생략 (비용 절감, 안티패턴 방지)
L2 PASS   → 이 게이트를 통해 LLM 최종 리뷰
"""
from __future__ import annotations

import json
import logging
from typing import Any

from jemmin.models import ContextBundle, ReviewRequest
from jemmin.prompts import PromptLoader
from jemmin.providers.base import ProviderRequest

_logger = logging.getLogger(__name__)

__all__ = ["LlmReviewGate"]

# L3 프롬프트 크기 제한 (7B 로컬 모델에서 180초 내 완료를 위해)
_MAX_DIFF_CHARS = 4000       # diff 최대 문자수
_MAX_CONTEXT_CHARS = 2000    # tier2~4 각 tier 최대 문자수
_MAX_TOTAL_PROMPT_CHARS = 8000  # user_prompt 전체 최대 문자수


class LlmReviewGate:
    """L3 LLM 최종 리뷰 게이트.

    사용법::

        gate = LlmReviewGate(provider=ollama_provider)
        llm_result = gate.review(request, context, agent_findings)
        # llm_result: {"result_code": "REVIEW_PASSED", "summary": "...", ...}
    """

    def __init__(self, provider: Any, prompt_loader: PromptLoader | None = None) -> None:
        self._provider = provider
        self._prompt_loader = prompt_loader or PromptLoader()

    def review(
        self,
        request: ReviewRequest,
        context: ContextBundle,
        agent_findings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """LLM에게 최종 리뷰를 요청합니다.

        Returns:
            LLM JSON 응답 dict. 파싱 실패 시 PASS fallback.
        """
        system_prompt = self._prompt_loader.get_system_prompt()
        user_prompt = self._build_user_prompt(request, context, agent_findings)

        provider_request = ProviderRequest(
            prompt_pack_version="1.0",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema={},
            metadata={"request_id": request.request_id, "stage": "l3_review"},
        )

        try:
            raw = self._provider.generate(provider_request)
        except (TimeoutError, ConnectionError, OSError) as exc:
            _logger.warning("[L3] LLM 호출 실패 (graceful degradation): %s", exc)
            return self._fallback_pass("LLM 호출 실패 — L2 PASS 결과를 유지합니다")
        except (ValueError, KeyError, RuntimeError, json.JSONDecodeError) as exc:
            _logger.error("[L3] LLM 응답 처리 오류: %s", exc, exc_info=True)
            return self._fallback_pass("LLM 응답 오류 — L2 PASS 결과를 유지합니다")

        return self._parse_response(raw)

    def _build_user_prompt(
        self,
        request: ReviewRequest,
        context: ContextBundle,
        agent_findings: list[dict[str, Any]] | None,
    ) -> str:
        """user prompt를 구성합니다. 토큰 절약을 위해 크기를 제한합니다."""
        parts: list[str] = []

        parts.append(f"[Target File]\n{request.target_file}\n")

        # diff는 핵심 — 크기 제한 적용
        diff = request.diff_text or ""
        if len(diff) > _MAX_DIFF_CHARS:
            diff = diff[:_MAX_DIFF_CHARS] + f"\n... (truncated, {len(request.diff_text)} chars total)"
        parts.append(f"[Git Diff]\n{diff}\n")

        # tier1~4 Context — 각 tier 크기 제한
        for tier_name in sorted(context.tiers.keys()):
            entries = context.tiers[tier_name]
            if entries:
                text = "\n".join(entries)
                if len(text) > _MAX_CONTEXT_CHARS:
                    text = text[:_MAX_CONTEXT_CHARS] + "\n... (truncated)"
                parts.append(f"[Context: {tier_name}]\n{text}\n")

        # L2 agent findings 동봉 (PASS지만 warn 1개 정도 있을 수 있음)
        if agent_findings:
            findings_text = "\n".join(
                f"- [{f.get('code', '?')}] {f.get('message', f.get('issue', ''))}"
                for f in agent_findings
            )
            parts.append(f"[Pre-filter Agent Findings]\n{findings_text}\n")

        result = "\n".join(parts)

        # 전체 프롬프트 크기 최종 제한
        if len(result) > _MAX_TOTAL_PROMPT_CHARS:
            result = result[:_MAX_TOTAL_PROMPT_CHARS] + "\n... (prompt truncated)"
            _logger.info(
                "[L3] 프롬프트 크기 제한 적용: %d → %d chars",
                len("\n".join(parts)), _MAX_TOTAL_PROMPT_CHARS,
            )

        return result

    def _parse_response(self, raw: dict[str, Any]) -> dict[str, Any]:
        """LLM 응답을 파싱합니다. JSON 문자열이면 파싱, dict면 그대로."""
        # provider가 이미 dict로 반환하는 경우
        if isinstance(raw, dict) and "result_code" in raw:
            return self._normalize_response(raw)

        # provider가 {"text": "..."} 등으로 반환하는 경우
        text = raw.get("text") or raw.get("reason") or ""
        if isinstance(text, str):
            text = text.strip()
            if text.startswith("{"):
                try:
                    return self._normalize_response(json.loads(text))
                except json.JSONDecodeError:
                    _logger.warning("[L3] LLM JSON 파싱 실패, fallback PASS")

        return self._fallback_pass("LLM 응답 파싱 불가 — L2 PASS 결과를 유지합니다")

    def _normalize_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Downgrade non-actionable L3 rejections to advisory pass.

        L3 is allowed to be noisy, but a blocking result must carry concrete
        details or a patch. Summary-only objections are treated as nitpicks.
        """
        result_code = payload.get("result_code")
        if result_code not in {"REVIEW_REJECTED", "PATCH_PROPOSED"}:
            return payload

        details = payload.get("details")
        has_details = isinstance(details, list) and len(details) > 0
        has_patch = bool(payload.get("suggested_patch"))
        if has_details or has_patch:
            return payload

        summary = str(payload.get("summary") or "").strip()
        return {
            "result_code": "REVIEW_PASSED",
            "summary": f"잔소리(advisory): {summary or '근거 없는 L3 차단 의견'}",
            "confidence_score": min(float(payload.get("confidence_score") or 0.5), 0.5),
            "details": [],
            "suggested_patch": None,
        }

    @staticmethod
    def _fallback_pass(reason: str) -> dict[str, Any]:
        return {
            "result_code": "REVIEW_PASSED",
            "summary": reason,
            "confidence_score": 0.5,
            "details": [],
            "suggested_patch": None,
        }

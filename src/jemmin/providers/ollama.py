"""Ollama LLM Provider — 로컬 Ollama 서버를 통한 무료 LLM 추론.

Ollama HTTP API (http://localhost:11434)를 사용하여 로컬 LLM 모델로
코드 리뷰를 수행합니다. API 비용 0원, 네트워크 지연 없음.

추천 모델:
  - qwen2.5-coder:7b  — 코딩 리뷰 기본값, 빠름 (VRAM 8GB)
  - qwen3:8b           — 범용 추론 균형형, 교차 검증 후보 (VRAM 8GB)
  - qwen2.5-coder:32b — 코딩 특화, 정확함 (VRAM 24GB)
  - deepseek-coder-v2  — 코드 분석 최상위 (VRAM 16GB+)
  - llama3.1:8b        — 범용, 가볍고 빠름 (VRAM 8GB)
  - codellama:13b      — Meta 코딩 모델 (VRAM 12GB)

사용법:
  1. Ollama 설치: https://ollama.ai
  2. 모델 다운로드: ollama pull qwen2.5-coder:7b
  3. 환경변수 또는 config 설정:
     - OLLAMA_MODEL=qwen2.5-coder:7b
     - OLLAMA_BASE_URL=http://localhost:11434 (기본값)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from jemmin.prompts import PromptLoader
from jemmin.providers.base import ProviderRequest

_prompt_loader = PromptLoader()

_logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_MODEL = "qwen2.5-coder:7b"
_DEFAULT_SOCKET_TIMEOUT = 30  # 소켓 읽기 타임아웃 (각 read 호출당)
_DEFAULT_TOTAL_TIMEOUT = 180  # 전체 응답 생성 데드라인 (초)


class OllamaProvider:
    """Ollama HTTP API 기반 로컬 LLM Provider.

    LLMProvider Protocol을 완전히 구현합니다.
    Ollama 서버가 실행 중이지 않으면 available()이 False를 반환합니다.

    모델 우선순위: 명시적 인자 > OLLAMA_MODEL 환경변수 > 기본값(qwen2.5-coder:7b)
    """

    name = "ollama"

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        timeout: int = _DEFAULT_SOCKET_TIMEOUT,
        total_timeout: int = _DEFAULT_TOTAL_TIMEOUT,
    ) -> None:
        self._model = (
            model
            or os.environ.get("OLLAMA_MODEL", "").strip()
            or _DEFAULT_MODEL
        )
        self._base_url = (
            base_url
            or os.environ.get("OLLAMA_BASE_URL", "").strip()
            or _DEFAULT_BASE_URL
        ).rstrip("/")
        self._socket_timeout = timeout
        self._total_timeout = total_timeout
        self._available: bool | None = None  # lazy check
        self._lock = threading.Lock()

    def available(self) -> bool:
        """Ollama 서버가 실행 중이고 모델이 설치되어 있는지 확인."""
        if self._available is not None:
            return self._available
        with self._lock:
            if self._available is not None:
                return self._available
            try:
                resp = self._http_get(f"{self._base_url}/api/tags")
                data = json.loads(resp)
                model_names = {
                    m.get("name", "") for m in data.get("models", [])
                }
                # 정확한 매치 또는 접두사 매치 (태그 생략 시)
                self._available = any(
                    self._model == name or name.startswith(self._model + ":")
                    or self._model.startswith(name)
                    for name in model_names
                ) if model_names else False
                if not self._available:
                    _logger.warning(
                        "Ollama model '%s' not found. Available: %s",
                        self._model,
                        sorted(model_names),
                    )
            except (URLError, OSError, json.JSONDecodeError) as exc:
                _logger.info("Ollama server not reachable: %s", exc)
                self._available = False
            return self._available

    def generate(self, request: ProviderRequest) -> dict[str, Any]:
        """Ollama /api/generate — 스트리밍 + 전체 데드라인으로 호출.

        stream=True로 토큰을 하나씩 수신하면서 전체 경과시간을 체크합니다.
        데드라인(total_timeout)을 초과하면 즉시 중단하고 fallback 반환합니다.
        """
        if not self.available():
            return self._fallback_response("ollama not available")

        prompt = self._build_prompt(request.system_prompt, request.user_prompt)

        try:
            body = {
                "model": self._model,
                "prompt": prompt,
                "stream": True,
                "format": "json",
                "options": {
                    "temperature": 0.0,
                    "num_predict": 4096,
                },
            }
            response_text = self._http_post_streaming(
                f"{self._base_url}/api/generate", body,
            )

            # JSON 응답 파싱 시도
            try:
                result = json.loads(response_text)
                # 최소 필수 필드 보장
                result.setdefault("status", "PASS")
                result.setdefault("reason", "")
                result.setdefault("patch_code", "")
                return result
            except (json.JSONDecodeError, ValueError):
                return {
                    "status": "PASS",
                    "reason": response_text,
                    "patch_code": "",
                }

        except TimeoutError as exc:
            _logger.warning("Ollama total timeout (%ds): %s", self._total_timeout, exc)
            return self._fallback_response(f"total timeout {self._total_timeout}s 초과")
        except (URLError, OSError, json.JSONDecodeError) as exc:
            _logger.error("Ollama generate failed: %s", exc)
            return self._fallback_response(str(exc))

    def generate_with_cache(
        self,
        cache_id: str,
        tier1_prompt: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """캐시 기반 생성 — Ollama는 자체 KV 캐시를 사용하므로 system prompt를 재전송."""
        # Ollama는 서버 레벨에서 KV 캐시를 자동 관리함
        # cache_id에서 system_prompt를 복원할 수 없으므로 tier1만으로 생성
        request = ProviderRequest(
            prompt_pack_version="1.0",
            system_prompt=_prompt_loader.get_system_prompt(),
            user_prompt=tier1_prompt,
            response_schema={},
        )
        return self.generate(request)

    def create_context_cache(
        self,
        system_prompt: str,
        static_context: str,
        ttl_seconds: int = 3600,
    ) -> str:
        """Ollama는 별도 캐시 API가 없음 — 더미 cache_id 반환.

        Ollama 서버는 자체적으로 KV 캐시를 관리하므로
        동일 system prompt 재전송 시 내부적으로 캐시됩니다.
        """
        cache_id = f"ollama-cache-{uuid.uuid4().hex[:12]}"
        _logger.debug("Ollama pseudo cache created: %s", cache_id)
        return cache_id

    def delete_context_cache(self, cache_id: str) -> None:
        """Ollama 자체 KV 캐시 관리 — 명시적 삭제 불필요."""
        _logger.debug("Ollama pseudo cache deleted: %s", cache_id)

    # ── Internal helpers ───────────────────────────────────────────

    def _build_prompt(self, system_prompt: str, user_prompt: str) -> str:
        """system + user 프롬프트를 단일 문자열로 결합."""
        parts: list[str] = []
        if system_prompt:
            parts.append(f"[SYSTEM]\n{system_prompt}\n")
        parts.append(f"[USER]\n{user_prompt}")
        return "\n".join(parts)

    def _http_post_streaming(self, url: str, body: dict) -> str:
        """스트리밍 HTTP POST — 전체 데드라인(total_timeout) 적용.

        Ollama stream=True 응답은 줄 단위 JSON:
          {"response": "토큰", "done": false}
          {"response": "",     "done": true, "total_duration": ...}

        각 토큰 수신 후 전체 경과시간을 체크하여 데드라인 초과 시
        TimeoutError를 발생시킵니다.
        """
        if self._total_timeout <= 0:
            raise TimeoutError(f"Ollama 응답 생성 {self._total_timeout}s 초과")

        data = json.dumps(body).encode("utf-8")
        req = Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        deadline = time.monotonic() + self._total_timeout
        tokens: list[str] = []

        with urlopen(req, timeout=self._socket_timeout) as resp:
            while True:
                # 데드라인 체크 (토큰 수신 전)
                if time.monotonic() > deadline:
                    elapsed = self._total_timeout
                    raise TimeoutError(
                        f"Ollama 응답 생성 {elapsed}s 초과 "
                        f"(수신 토큰 {len(tokens)}개)"
                    )

                line = resp.readline()
                if not line:
                    break

                try:
                    chunk = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue

                token = chunk.get("response", "")
                if token:
                    tokens.append(token)

                if chunk.get("done", False):
                    elapsed = self._total_timeout - (deadline - time.monotonic())
                    _logger.info(
                        "Ollama 응답 완료: %.1fs, %d토큰",
                        elapsed, len(tokens),
                    )
                    break

        return "".join(tokens)

    def _http_post(self, url: str, body: dict) -> str:
        """urllib 기반 HTTP POST (비스트리밍) — available() 체크 등에 사용."""
        data = json.dumps(body).encode("utf-8")
        req = Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=self._socket_timeout) as resp:
            return resp.read().decode("utf-8")

    def _http_get(self, url: str) -> str:
        """urllib 기반 HTTP GET."""
        req = Request(url, method="GET")
        with urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")

    def _fallback_response(self, reason: str) -> dict[str, Any]:
        return {"status": "PASS", "reason": f"ollama fallback: {reason}", "patch_code": ""}

    def reset_availability(self) -> None:
        """가용성 캐시를 리셋합니다. 서버 재시작 후 호출."""
        with self._lock:
            self._available = None

    # ── Layer-1 (mini_reviewer) 어댑터 ──────────────────────────────
    #
    # 전제: mini_reviewer.generate_review()는 mini RESPONSE_SCHEMA
    #   (result_code/summary/confidence_score/details/suggested_patch)를
    #   요구한다. Layer-2 generate()는 status/reason/patch_code shape라 부적합.
    # 후조건: 성공 시 dict(JSON 파싱 완료, 최소 result_code 포함)를 반환한다.
    # **silent-PASS 절대 금지(D4)**: 서버 미가용/timeout/JSON 파싱 실패는
    #   _fallback_response(status=PASS)를 호출하지 않고 예외를 전파한다.
    #   호출자(mini_nitpicker)가 exit 2로 처리하게 한다 = 게이트 false-green 차단.

    @property
    def model(self) -> str:
        return self._model

    def generate_mini(self, system_prompt: str, user_prompt: str, response_format: dict) -> dict[str, Any]:
        """Layer-1 mini 스키마용 생성. 실패 시 예외 전파(silent-PASS 금지).

        Args:
            system_prompt: nitpicker 시스템 프롬프트(4대 규칙).
            user_prompt: [Target File] + [Git Diff] 텍스트.
            response_format: Ollama `/api/generate` format에 넘길 JSON Schema.

        Returns:
            Ollama가 산출한 JSON을 파싱한 dict. 최소 result_code 키 포함을 보장하지 않으며
            구조 검증은 호출자(mini_reviewer)가 담당한다.

        Raises:
            RuntimeError: Ollama 서버 미가용 / 빈 응답 / JSON 파싱 실패.
            TimeoutError: 전체 데드라인(total_timeout) 초과.
            URLError, OSError: HTTP 전송 실패(예외 그대로 전파).
        """
        if not self.available():
            raise RuntimeError(
                f"Ollama 서버 미가용 또는 모델 '{self._model}' 미설치 "
                f"(base_url={self._base_url}). silent-PASS 금지 → 리뷰 차단."
            )

        prompt = self._build_prompt(system_prompt, user_prompt)
        body = {
            "model": self._model,
            "prompt": prompt,
            "stream": True,
            "format": response_format,
            "options": {
                "temperature": 0.0,
                "num_predict": 4096,
            },
        }
        # 예외(TimeoutError/URLError/OSError)는 의도적으로 잡지 않고 전파한다.
        response_text = self._http_post_streaming(
            f"{self._base_url}/api/generate", body,
        )
        if not response_text or not response_text.strip():
            raise RuntimeError("Ollama가 빈 응답을 반환했습니다 (mini 리뷰 불가)")
        try:
            return json.loads(response_text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(
                f"Ollama 응답 JSON 파싱 실패: {exc}; 원문 앞부분={response_text[:200]!r}"
            ) from exc

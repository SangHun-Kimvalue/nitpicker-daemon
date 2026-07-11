from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import Any

_KST = timezone(timedelta(hours=9), name="KST")

try:
    from google import genai
    from google.genai import types as genai_types
    _HAS_GENAI = True
except ModuleNotFoundError:
    _HAS_GENAI = False


from jemmin.prompts import PromptLoader
from jemmin.providers.ollama import OllamaProvider

_prompt_loader = PromptLoader()

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "nitpicker.local.json"
EXAMPLE_CONFIG_PATH = ROOT / "config" / "nitpicker.local.example.json"
LOG_PATH = ROOT / ".jemmin" / "logs" / "mini_reviews.jsonl"
LATEST_JSON_PATH = ROOT / ".jemmin" / "logs" / "latest_review.json"
LATEST_TEXT_PATH = ROOT / ".jemmin" / "logs" / "latest_review.txt"
DEFAULT_GEMINI_MODEL = "gemini-3.1-pro-preview"
DEFAULT_GEMINI_FALLBACK_MODEL = "gemini-2.0-flash"
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "result_code": {"type": "STRING"},
        "summary": {"type": "STRING"},
        "confidence_score": {"type": "NUMBER"},
        "details": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "line_number": {"type": "INTEGER", "nullable": True},
                    "issue": {"type": "STRING"}
                },
                "required": ["line_number", "issue"]
            }
        },
        "suggested_patch": {"type": "STRING", "nullable": True}
    },
    "required": ["result_code", "summary", "confidence_score", "details", "suggested_patch"]
}


DEFAULT_PROVIDER = "ollama"
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:7b"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
_VALID_RESULT_CODES = ("REVIEW_PASSED", "PATCH_PROPOSED", "REVIEW_REJECTED")

# Ollama `/api/generate` format용 표준 JSON Schema.
# mini RESPONSE_SCHEMA(Gemini flavored: "OBJECT"/"STRING")와 의미는 동일하되,
# Ollama가 이해하는 표준 JSON Schema 타입/enum으로 표현해 동일 payload를 강제한다.
OLLAMA_RESPONSE_FORMAT = {
    "type": "object",
    "properties": {
        "result_code": {"type": "string", "enum": list(_VALID_RESULT_CODES)},
        "summary": {"type": "string"},
        "confidence_score": {"type": "number"},
        "details": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line_number": {"type": ["integer", "null"]},
                    "issue": {"type": "string"},
                },
                "required": ["line_number", "issue"],
            },
        },
        "suggested_patch": {"type": ["string", "null"]},
    },
    "required": ["result_code", "summary", "confidence_score", "details", "suggested_patch"],
}


@dataclass(slots=True)
class MiniReviewerSettings:
    gemini_api_key: str
    gemini_model: str
    watch_path: str
    debounce_seconds: float
    file_extensions: tuple[str, ...]
    skip: bool = False
    auto_apply_patches: bool = False
    gemini_fallback_model: str = DEFAULT_GEMINI_FALLBACK_MODEL
    provider: str = DEFAULT_PROVIDER
    ollama_model: str = DEFAULT_OLLAMA_MODEL
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL


def _load_jsonc(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    result: list[str] = []
    in_string = False
    escape = False
    line_comment = False
    block_comment = False
    index = 0

    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""

        if line_comment:
            if char == "\n":
                line_comment = False
                result.append(char)
            index += 1
            continue

        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 2
                continue
            if char == "\n":
                result.append(char)
            index += 1
            continue

        if in_string:
            result.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue

        if char == "/" and next_char == "/":
            line_comment = True
            index += 2
            continue

        if char == "/" and next_char == "*":
            block_comment = True
            index += 2
            continue

        result.append(char)
        index += 1

    return json.loads("".join(result))


def load_settings() -> MiniReviewerSettings:
    data: dict = {}
    if CONFIG_PATH.exists():
        data = _load_jsonc(CONFIG_PATH)
    skip = (os.environ.get("NITPICKER_SKIP") == "1") or bool(data.get("skip", False))
    api_key = os.environ.get("GEMINI_API_KEY") or data.get("gemini_api_key", "")
    gemini_model = (os.environ.get("GEMINI_MODEL") or data.get("gemini_model", DEFAULT_GEMINI_MODEL)).strip()
    gemini_fallback_model = (
        os.environ.get("GEMINI_FALLBACK_MODEL")
        or data.get("gemini_fallback_model", DEFAULT_GEMINI_FALLBACK_MODEL)
    ).strip()
    auto_apply: bool = (
        os.environ.get("NITPICKER_AUTO_APPLY") == "1"
        or bool(data.get("auto_apply_patches", False))
    )
    provider = (
        os.environ.get("NITPICKER_PROVIDER")
        or data.get("provider", DEFAULT_PROVIDER)
    ).strip().lower() or DEFAULT_PROVIDER
    ollama_model = (
        os.environ.get("OLLAMA_MODEL")
        or data.get("ollama_model", DEFAULT_OLLAMA_MODEL)
    ).strip() or DEFAULT_OLLAMA_MODEL
    ollama_base_url = (
        os.environ.get("OLLAMA_BASE_URL")
        or data.get("ollama_base_url", DEFAULT_OLLAMA_BASE_URL)
    ).strip() or DEFAULT_OLLAMA_BASE_URL
    settings = MiniReviewerSettings(
        gemini_api_key=api_key,
        gemini_model=gemini_model,
        watch_path=data.get("watch_path", "src"),
        debounce_seconds=float(data.get("debounce_seconds", 2.0)),
        file_extensions=tuple(ext.lower() for ext in data.get("file_extensions", [".py", ".cpp", ".h", ".hpp"])),
        skip=skip,
        auto_apply_patches=auto_apply,
        gemini_fallback_model="" if gemini_fallback_model == gemini_model else gemini_fallback_model,
        provider=provider,
        ollama_model=ollama_model,
        ollama_base_url=ollama_base_url,
    )
    if settings.provider not in ("ollama", "gemini"):
        raise RuntimeError(
            f"invalid provider '{settings.provider}' in local config; use 'ollama' or 'gemini'"
        )
    # Gemini API 키/모델 검증은 provider=gemini일 때만 필수(D5).
    # provider=ollama이면 Gemini 크레딧 만료 상태에서도 동작해야 하므로 완화한다.
    _gemini_required = settings.provider == "gemini"
    if _gemini_required and not settings.skip and not settings.gemini_api_key:
        raise RuntimeError(
            f"missing Gemini API key; copy {EXAMPLE_CONFIG_PATH} to {CONFIG_PATH} or set GEMINI_API_KEY"
        )
    if (
        _gemini_required
        and not settings.skip
        and (not settings.gemini_model or any(char.isspace() for char in settings.gemini_model))
    ):
        raise RuntimeError(
            "invalid gemini_model in local config; use a valid model id such as gemini-3.1-pro-preview"
        )
    if (
        _gemini_required
        and not settings.skip
        and settings.gemini_fallback_model
        and any(char.isspace() for char in settings.gemini_fallback_model)
    ):
        raise RuntimeError(
            "invalid gemini_fallback_model in local config; use a valid model id such as gemini-2.0-flash"
        )
    if settings.provider == "ollama" and not settings.skip:
        if not settings.ollama_model or any(char.isspace() for char in settings.ollama_model):
            raise RuntimeError(
                "invalid ollama_model in local config; use a valid model id such as qwen2.5-coder:7b"
            )
    return settings


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def normalize_target(path: str) -> str:
    target = Path(path)
    if target.is_absolute():
        try:
            return target.resolve().relative_to(ROOT).as_posix()
        except ValueError:
            return target.resolve().as_posix()
    return target.as_posix()


def targets_from_args(paths: list[str], staged: bool) -> list[str]:
    if staged:
        return [path for path in git("diff", "--cached", "--name-only", "--diff-filter=ACM").splitlines() if path]
    return [normalize_target(path) for path in paths]


def diff_for(path: str, staged: bool) -> str:
    args = ["diff", "--unified=3"] + (["--cached"] if staged else ["HEAD"]) + ["--", path]
    return git(*args)


def _model_candidates(settings: MiniReviewerSettings) -> tuple[str, ...]:
    candidates: list[str] = []
    for model in (settings.gemini_model, settings.gemini_fallback_model):
        if model and model not in candidates:
            candidates.append(model)
    return tuple(candidates)


def _is_missing_model_error(message: str) -> bool:
    lowered = message.lower()
    return "not_found" in lowered or "is not found" in lowered or "listmodels" in lowered


def _build_client(api_key: str) -> Any:
    if not _HAS_GENAI:
        raise RuntimeError("google-genai is not installed in the active Python environment")
    return genai.Client(api_key=api_key)


def _normalize_response_payload(response: Any) -> dict:
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, dict):
        return parsed
    if parsed is not None and hasattr(parsed, "model_dump"):
        return parsed.model_dump()
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return json.loads(text)
    raise RuntimeError("Gemini returned an empty response body")


def _format_review_text(payload: dict) -> str:
    kst_now = datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S KST")
    lines = [
        f"리뷰 시각: {kst_now}",
        f"대상 파일: {payload.get('target_file', '')}",
        f"리뷰어: {payload.get('reviewer', '')}",
        f"결과 코드: {payload.get('result_code', '')}",
        f"요약: {payload.get('summary', '')}",
        f"신뢰도: {payload.get('confidence_score', '')}",
        "세부 사항:",
    ]
    details = payload.get("details") or []
    if not details:
        lines.append("- 없음")
    else:
        for detail in details:
            line_number = detail.get("line_number")
            issue = detail.get("issue", "")
            label = f"{line_number}번째 줄" if line_number is not None else "줄 미지정"
            lines.append(f"- {label}: {issue}")
    lines.append("제안 패치:")
    lines.append(payload.get("suggested_patch") or "(없음)")
    return "\n".join(lines) + "\n"


def format_review_summary(payload: dict) -> str:
    return (
        f"[{payload.get('result_code', 'UNKNOWN')}] "
        f"{payload.get('target_file', '')}: {payload.get('summary', '')} "
        f"(confidence={payload.get('confidence_score', '')})"
    )


def append_review_logs(payloads: list[dict]) -> Path:
    if not payloads:
        return LOG_PATH
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for payload in payloads:
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            **payload,
        }
        lines.append(json.dumps(record, ensure_ascii=False) + "\n")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write("".join(lines))
    return LOG_PATH


def write_latest_review_files(payloads: list[dict]) -> tuple[Path, Path] | None:
    if not payloads:
        return None
    latest = payloads[-1]
    LATEST_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    process_id = os.getpid()
    json_tmp = LATEST_JSON_PATH.with_name(f"{LATEST_JSON_PATH.name}.{process_id}.tmp")
    json_tmp.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    json_tmp.replace(LATEST_JSON_PATH)
    text_tmp = LATEST_TEXT_PATH.with_name(f"{LATEST_TEXT_PATH.name}.{process_id}.tmp")
    text_tmp.write_text(_format_review_text(latest), encoding="utf-8")
    text_tmp.replace(LATEST_TEXT_PATH)
    return LATEST_JSON_PATH, LATEST_TEXT_PATH


# ---------------------------------------------------------------------------
# Safe Auto-Fix: git apply 기반 원자적 패치 적용
# ---------------------------------------------------------------------------

_GIT_APPLY_TIMEOUT: int = 10  # seconds


def _apply_patch_safely(patch_text: str) -> bool:
    """LLM이 제안한 unified diff 패치를 git apply로 원자적(Atomic)으로 적용합니다.

    **절대 금지**: 파이썬 코드가 대상 파일을 open('w')로 직접 덮어쓰는 행위.
    반드시 git apply를 통해서만 적용합니다.

    동작 순서:
      1. tempfile로 임시 .diff 파일 생성
      2. git apply --check 로 dry-run 검증
      3. 검증 통과 시에만 git apply 로 실제 적용
      4. 임시 파일은 반드시 삭제 (finally)

    Returns:
        True  — 패치 적용 성공
        False — dry-run 검증 실패 (패치 충돌, 형식 오류 등)

    Raises:
        FileNotFoundError — git 바이너리가 없는 환경
        subprocess.TimeoutExpired — git 실행 타임아웃
        OSError — 예상치 못한 OS 에러 (Fail-Fast 전파)
    """
    if not patch_text or not patch_text.strip():
        return False

    fd: int = -1
    temp_path: str = ""
    try:
        # 1. 임시 .diff 파일 생성
        fd, temp_path = tempfile.mkstemp(suffix=".diff", prefix="nitpicker_")
        with os.fdopen(fd, "wb") as f:
            fd = -1  # os.fdopen이 fd 소유권을 가져감 — 즉시 마킹하여 finally 이중 close 방지
            f.write(patch_text.encode("utf-8"))

        # 2. git apply --check (dry-run 검증)
        check_result: subprocess.CompletedProcess[str] = subprocess.run(
            ["git", "apply", "--check", temp_path],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_APPLY_TIMEOUT,
        )
        if check_result.returncode != 0:
            reason: str = check_result.stderr.strip() or check_result.stdout.strip() or "unknown"
            logging.warning(
                "[AutoFix] git apply --check 실패: %s",
                reason,
            )
            return False

        # 3. 검증 통과 → 실제 적용
        apply_result: subprocess.CompletedProcess[str] = subprocess.run(
            ["git", "apply", temp_path],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_APPLY_TIMEOUT,
        )
        if apply_result.returncode != 0:
            reason = apply_result.stderr.strip() or apply_result.stdout.strip() or "unknown"
            logging.warning(
                "[AutoFix] git apply 적용 실패: %s",
                reason,
            )
            return False

        logging.info("[AutoFix] 패치 적용 성공")
        return True

    finally:
        # 4. 임시 파일 정리 (fd가 아직 열려있으면 닫기)
        if fd >= 0:
            os.close(fd)
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)


def _build_user_prompt(path: str, diff_text: str) -> str:
    return f"[Target File]\n{path}\n\n[Git Diff]\n{diff_text}\n"


def _coerce_ollama_payload(raw: dict) -> dict:
    """Ollama가 반환한 dict를 mini RESPONSE_SCHEMA 형태로 정규화/검증한다.

    Ollama `format`(JSON Schema)으로 구조를 강제하지만, 모델이 result_code를
    스키마 enum 밖 값으로 낼 가능성에 대비해 한 번 더 검증한다.

    **silent-PASS 금지(D4)**: 미가용/timeout/파싱 실패는 generate_mini가 이미
    예외로 처리한다. 여기서는 구조가 깨진 응답을 PASS로 둔갑시키지 않는다 —
    필수 키 누락 시 RuntimeError를 던져 exit 2로 흐르게 한다.
    """
    if not isinstance(raw, dict):
        raise RuntimeError(f"Ollama mini 응답이 객체가 아님: {type(raw).__name__}")
    result_code = raw.get("result_code")
    if result_code not in _VALID_RESULT_CODES:
        raise RuntimeError(
            f"Ollama가 유효하지 않은 result_code를 반환: {result_code!r} "
            f"(허용: {_VALID_RESULT_CODES}). silent-PASS 금지 → 리뷰 차단."
        )
    details = raw.get("details")
    if not isinstance(details, list):
        details = []
    normalized_details: list[dict] = []
    for detail in details:
        if isinstance(detail, dict):
            normalized_details.append(
                {
                    "line_number": detail.get("line_number"),
                    "issue": str(detail.get("issue", "")),
                }
            )
    payload: dict = {
        "result_code": result_code,
        "summary": str(raw.get("summary", "")),
        "confidence_score": raw.get("confidence_score", 0.0),
        "details": normalized_details,
        "suggested_patch": raw.get("suggested_patch"),
    }
    return payload


def _generate_review_ollama(path: str, diff_text: str, settings: MiniReviewerSettings) -> dict:
    """provider=ollama 경로 — OllamaProvider Layer-1 어댑터로 mini 스키마 산출.

    silent-PASS 금지: provider.generate_mini가 미가용/timeout/파싱 실패 시 예외를
    전파하며, 호출자(review_targets → mini_nitpicker)가 exit 2로 처리한다.
    """
    provider = OllamaProvider(
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
    )
    raw = provider.generate_mini(
        system_prompt=_prompt_loader.get_system_prompt(),
        user_prompt=_build_user_prompt(path, diff_text),
        response_format=OLLAMA_RESPONSE_FORMAT,
    )
    payload = _coerce_ollama_payload(raw)
    payload["target_file"] = path
    payload["reviewer"] = f"Ollama/{provider.model}"
    return payload


def generate_review(path: str, staged: bool, settings: MiniReviewerSettings) -> dict | None:
    if settings.skip:
        return None
    diff_text = diff_for(path, staged)
    if not diff_text:
        return None
    if settings.provider == "ollama":
        return _generate_review_ollama(path, diff_text, settings)
    # provider == "gemini" (보존 경로)
    if not _HAS_GENAI:
        raise RuntimeError("google-genai is not installed in the active Python environment")
    client = _build_client(settings.gemini_api_key)
    prompt = _build_user_prompt(path, diff_text)
    model_candidates = _model_candidates(settings)
    for index, model_name in enumerate(model_candidates):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    system_instruction=_prompt_loader.get_system_prompt(),
                    response_mime_type="application/json",
                    response_schema=RESPONSE_SCHEMA,
                    temperature=0.0,
                ),
            )
            break
        except Exception as error:
            message = str(error)
            if _is_missing_model_error(message):
                if index + 1 < len(model_candidates):
                    logging.warning(
                        "[mini_nitpicker] Gemini model unavailable: %s; retrying with fallback model %s",
                        model_name,
                        model_candidates[index + 1],
                    )
                    continue
                raise RuntimeError(
                    "Gemini model is not available for generateContent in this API path; "
                    f"tried: {', '.join(model_candidates)}"
                ) from error
            if "RESOURCE_EXHAUSTED" in message or "quota" in message.lower():
                raise RuntimeError(
                    "Gemini API quota exhausted; check billing/quota or switch to a key/project with available capacity"
                ) from error
            if "API key" in message or "authentication" in message.lower() or "permission" in message.lower():
                raise RuntimeError("Gemini API authentication failed; verify gemini_api_key in the local config") from error
            raise
    payload = _normalize_response_payload(response)
    payload["target_file"] = path
    payload["reviewer"] = f"Gemini/{model_name}"
    return payload


def review_targets(paths: list[str], staged: bool, settings: MiniReviewerSettings) -> list[dict]:
    if settings.skip:
        print("[mini_nitpicker] skipped by NITPICKER_SKIP=1")
        return []
    targets = targets_from_args(paths, staged)
    if not targets:
        print("[mini_nitpicker] no changed files to review")
        return []
    payloads: list[dict] = []
    for path in targets:
        payload = generate_review(path, staged, settings)
        if payload is not None:
            # Safe Auto-Fix: PATCH_PROPOSED + auto_apply 활성화 시 패치 자동 적용
            if (
                payload.get("result_code") == "PATCH_PROPOSED"
                and settings.auto_apply_patches
            ):
                patch_text: str = payload.get("suggested_patch") or ""
                if _apply_patch_safely(patch_text):
                    payload["result_code"] = "PATCH_APPLIED"
                    payload["summary"] = f"[Auto-Fixed] {payload.get('summary', '')}"
                    logging.info(
                        "[AutoFix] %s: 패치 자동 적용 완료 → PATCH_APPLIED",
                        path,
                    )
                else:
                    logging.info(
                        "[AutoFix] %s: 패치 적용 실패, PATCH_PROPOSED 유지",
                        path,
                    )
            payloads.append(payload)
    append_review_logs(payloads)
    write_latest_review_files(payloads)
    return payloads
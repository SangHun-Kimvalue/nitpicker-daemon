from __future__ import annotations

# ruff: noqa: E402

import argparse
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from jemmin.ipc.offload_gateway import StubOffloadGateway
from jemmin.ipc.zmq_client import ZmqClient
from jemmin.models import ReviewRequest, ReviewResult
from jemmin.orchestrator.consensus import DefaultConsensusEngine
from jemmin.orchestrator.controller import ReviewOrchestrator
from jemmin.orchestrator.policy_engine import DefaultPolicyEngine
from jemmin.orchestrator.resource_mgr import DefaultResourceManager
from jemmin.providers.local_llm import MockLocalLLMProvider
from jemmin.providers.ollama import OllamaProvider
from jemmin.registry import create_default_registry
from jemmin.services.context_svc import StaticContextService
from jemmin.services.feedback_svc import FileFeedbackService
from jemmin.services.patch_svc import PatchService
from jemmin.services.review_logger import JsonlReviewLogger
from jemmin.services.verification_svc import VerificationService
from jemmin.state.sqlite_spooler import SQLiteJobStore
from jemmin.services.cache_mgr import ContextCacheManager
from jemmin.triggers import CliTriggerAdapter, TriggerEvent
from jemmin.services.llm_review_gate import LlmReviewGate
from jemmin.services.notifier import NotificationService
from jemmin.utils.duckdb_logger import DuckDbLogger

DAEMON_ADDRESS = "tcp://127.0.0.1:5555"
_LOCAL_CONFIG_PATH = ROOT / "config" / "nitpicker.local.json"
_REVIEWER_CONFIG_PATH = ROOT / "config" / "reviewer_config.yaml"

_cli_adapter = CliTriggerAdapter()


def _exit_code_from_result_code(result_code: str | None) -> int:
    """Map the stable machine result code to CLI exit code.

    Only REVIEW_PASSED is allowed to pass the gate. Unknown, missing, and newly
    added result codes must fail closed so the review gate cannot go false-green.
    """
    return 0 if result_code == "REVIEW_PASSED" else 1


def _emit_result_code(result_code: str | None) -> str:
    """Stable result code를 downstream용 단일 machine token으로 출력한다."""
    code = result_code if result_code else "UNKNOWN"
    token = f"JEMMIN_RESULT_CODE={code}"
    print(token)
    return token


def _exit_code_from_result(result: ReviewResult) -> int:
    return _exit_code_from_result_code(result.result_code)


def _direct_runtime_dir() -> Path:
    configured = os.environ.get("JEMMIN_RUNTIME_DIR")
    return Path(configured) if configured else ROOT / ".jemmin"


def _configure_stdio() -> None:
    """Windows 콘솔에서도 UTF-8 출력이 깨지지 않도록 설정."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _resolve_diff_text(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    """CLI 인자에서 diff 텍스트를 안정적으로 읽는다.

    긴 multi-line diff는 shell quoting에 취약하므로 --diff-file 또는
    --diff-stdin을 우선 사용한다.
    """
    sources = [
        bool(args.diff),
        bool(args.diff_file),
        bool(args.diff_stdin),
    ]
    if sum(sources) > 1:
        parser.error("--diff, --diff-file, --diff-stdin 중 하나만 사용할 수 있습니다.")

    if args.diff_file:
        try:
            return _read_diff_file(Path(args.diff_file))
        except OSError as exc:
            parser.error(f"--diff-file 읽기 실패: {exc}")

    if args.diff_stdin:
        return sys.stdin.read()

    return args.diff or ""


def _read_diff_file(path: Path) -> str:
    """PowerShell/CMD 리다이렉션 결과를 포함해 diff 파일을 읽는다."""
    raw = path.read_bytes()

    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")

    nul_ratio = raw.count(b"\x00") / max(len(raw), 1)
    if nul_ratio > 0.2:
        for encoding in ("utf-16-le", "utf-16-be"):
            try:
                return raw.decode(encoding)
            except UnicodeError:
                continue

    for encoding in ("utf-8", "cp949"):
        try:
            return raw.decode(encoding)
        except UnicodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _load_local_config() -> dict[str, Any]:
    """config/nitpicker.local.json 을 읽어 dict로 반환. 없으면 빈 dict."""
    if _LOCAL_CONFIG_PATH.is_file():
        import json
        try:
            return json.loads(_LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _load_reviewer_provider_config() -> dict[str, str]:
    """config/reviewer_config.yaml 의 provider 1-depth 설정을 읽는다.

    PyYAML 의존성을 CLI 경로에 추가하지 않기 위해 이 파일에서 필요한
    단순 key/value만 파싱한다.
    """
    if not _REVIEWER_CONFIG_PATH.is_file():
        return {}

    try:
        lines = _REVIEWER_CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    provider_indent: int | None = None
    values: dict[str, str] = {}
    for raw_line in lines:
        stripped = raw_line.split("#", 1)[0].rstrip()
        if not stripped:
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))

        if provider_indent is None:
            if stripped == "provider:":
                provider_indent = indent
            continue

        if indent <= provider_indent:
            break
        relative = indent - provider_indent
        if relative != 2 or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        value = value.strip().strip("\"'")
        if value:
            values[key.strip()] = value

    return values


def build_request(file_path: str, diff_text: str) -> ReviewRequest:
    """통합 CliTriggerAdapter를 통한 요청 생성 — 입력 계약 통일."""
    event = TriggerEvent(
        source="cli",
        payload={"target_file": str(Path(file_path)), "diff_text": diff_text},
    )
    return _cli_adapter.build_request(event)


def _run_via_daemon(file_path: str, diff_text: str) -> int:
    """데몬이 실행 중이면 ZmqClient로 요청 전송. 타임아웃 시 fallback 안내."""
    request: ReviewRequest = build_request(file_path, diff_text)
    payload = {
        "schema_version": "1.0",
        "message_type": "review.request",
        "request_id": request.request_id,
        "target_file": request.target_file,
        "diff_text": request.diff_text,
        "project_id": request.project_id,
        "project_profile": request.project_profile,
        "trigger": request.trigger,
        "trigger_intent": request.trigger_intent,
        "git_revision": request.git_revision,
    }
    try:
        with ZmqClient(server_address=DAEMON_ADDRESS, timeout_ms=5000) as client:
            response = client.send(payload)
        if response.get("status") == "success":
            result = response["response"]
            print(f"{result.get('state')} {result.get('status')} {result.get('summary')}")
            result_code = result.get("result_code")
            _emit_result_code(result_code)
            return _exit_code_from_result_code(result_code)
        print(f"[jemmin_cli] 데몬 오류: {response.get('error_message')}", file=sys.stderr)
        return 1
    except (TimeoutError, ConnectionError) as error:
        print(f"[jemmin_cli] 데몬 연결 실패 ({error}), 직접 실행 모드로 폴백", file=sys.stderr)
        return None  # type: ignore[return-value]


def _select_provider(name: str = "mock", *, model: str | None = None) -> Any:
    """--provider 인자에 따라 LLM Provider를 선택합니다."""
    if name == "ollama":
        provider_cfg = _load_reviewer_provider_config()
        p = OllamaProvider(
            model=model or provider_cfg.get("ollama_model"),
            base_url=provider_cfg.get("ollama_base_url"),
        )
        if p.available():
            print(f"[jemmin_cli] Ollama provider active (model={p._model})")
            return p
        print("[jemmin_cli] Ollama not available, falling back to mock", file=sys.stderr)
        return MockLocalLLMProvider()
    if name == "gemini":
        from jemmin.providers.gemini import GeminiProvider
        local_cfg = _load_local_config()
        gemini_provider = GeminiProvider(
            api_key=local_cfg.get("gemini_api_key"),
            model=local_cfg.get("gemini_model"),
            fallback_model=local_cfg.get("gemini_fallback_model"),
        )
        if gemini_provider.available():
            print(f"[jemmin_cli] Gemini provider active (model={gemini_provider._model})")
            return gemini_provider
        print("[jemmin_cli] Gemini not available (no API key?), falling back to mock", file=sys.stderr)
        return MockLocalLLMProvider()
    return MockLocalLLMProvider()


def _run_direct(
    file_path: str,
    diff_text: str,
    provider_name: str = "mock",
    *,
    model: str | None = None,
) -> int:
    """데몬 없이 ReviewOrchestrator를 직접 실행."""
    runtime_dir = _direct_runtime_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "logs").mkdir(parents=True, exist_ok=True)
    (runtime_dir / "patches").mkdir(parents=True, exist_ok=True)
    provider = _select_provider(provider_name, model=model)
    reviewer_label = f"{provider.__class__.__name__}/{getattr(provider, '_model', 'unknown')}"
    # 알림 설정 로드
    local_cfg = _load_local_config()
    notifier = NotificationService(
        toast_enabled=local_cfg.get("toast_enabled", False),
        webhook_url=local_cfg.get("webhook_url", ""),
        webhook_format=local_cfg.get("webhook_format", "slack"),
        slack_webhook_url=local_cfg.get("slack_webhook_url", ""),  # 하위 호환
    )
    registry = create_default_registry(provider=provider)
    cache_mgr: ContextCacheManager = ContextCacheManager(provider=provider, ttl_seconds=3600, max_entries=64)
    analytics_logger: DuckDbLogger | None = DuckDbLogger(
        db_path=runtime_dir / "analytics.duckdb"
    )
    try:
        orchestrator = ReviewOrchestrator(
            job_store=SQLiteJobStore(runtime_dir / "spool.db"),
            policy_engine=DefaultPolicyEngine(max_file_size_bytes=1_048_576),
            resource_manager=DefaultResourceManager(token_budget=200_000),
            context_service=StaticContextService(
                review_log_path=runtime_dir / "logs" / "review_history.jsonl",
            ),
            agent_registry=registry,
            consensus_engine=DefaultConsensusEngine(),
            feedback_service=FileFeedbackService(runtime_dir / "logs" / "LATEST_REVIEW.txt"),
            review_logger=JsonlReviewLogger(runtime_dir / "logs" / "review_history.jsonl"),
            patch_service=PatchService(patches_dir=runtime_dir / "patches", project_root=ROOT),
            verification_service=VerificationService(project_root=ROOT),
            analytics_logger=analytics_logger,
            context_cache_manager=cache_mgr,
            offload_gateway=StubOffloadGateway(),
            llm_review_gate=LlmReviewGate(provider=provider),
            notifier=notifier if notifier.enabled else None,
        )
        request = build_request(file_path, diff_text)
        request.metadata["reviewer"] = reviewer_label
        result = orchestrator.run_once(request)
        print(result.state.value, result.status, result.summary)
        _emit_result_code(result.result_code)
        return _exit_code_from_result(result)
    finally:
        if analytics_logger is not None:
            analytics_logger.close()


def _run_staged(provider_name: str = "mock", *, model: str | None = None) -> int:
    """git staged 파일 전체를 순차 리뷰 — multi-file PR 리뷰."""
    import subprocess as _sp

    # staged 파일 목록
    try:
        out = _sp.check_output(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            cwd=str(ROOT), text=True, stderr=_sp.DEVNULL,
        ).strip()
    except (_sp.CalledProcessError, FileNotFoundError):
        print("[jemmin_cli] git diff --cached 실행 실패", file=sys.stderr)
        return 1

    if not out:
        print("[jemmin_cli] staged 파일이 없습니다.")
        return 0

    files = out.splitlines()
    print(f"[jemmin_cli] staged 파일 {len(files)}개 리뷰 시작 (provider={provider_name})")

    results: list[tuple[str, str, str]] = []  # (file, status, summary)
    exit_code = 0

    for f in files:
        # 파일별 diff
        try:
            diff = _sp.check_output(
                ["git", "diff", "--cached", "--", f],
                cwd=str(ROOT), text=True, stderr=_sp.DEVNULL,
            ).strip()
        except _sp.CalledProcessError:
            diff = ""
        if not diff:
            results.append((f, "skipped", "diff 없음"))
            continue

        # spool.db 초기화 (idempotency 충돌 방지)
        spool = ROOT / ".jemmin" / "spool.db"
        spool.unlink(missing_ok=True)

        rc = _run_direct(f, diff, provider_name=provider_name, model=model)
        # 마지막 print 출력에서 결과 추출 (간이)
        latest = ROOT / ".jemmin" / "logs" / "LATEST_REVIEW.txt"
        if latest.is_file():
            lines = latest.read_text(encoding="utf-8").splitlines()
            code = next((line.split(":", 1)[1].strip() for line in lines if line.startswith("결과 코드:")), "?")
            summary = next((line.split(":", 1)[1].strip() for line in lines if line.startswith("요약:")), "")
            results.append((f, code, summary[:80]))
        else:
            results.append((f, "?", "결과 파일 없음"))
        if rc != 0:
            exit_code = 1

    # PR 요약 출력
    print()
    print("=" * 60)
    print(f"  PR Review Summary ({len(files)} files)")
    print("=" * 60)
    passed = sum(1 for _, code, _ in results if code == "REVIEW_PASSED")
    rejected = len(results) - passed
    for f, code, summary in results:
        icon = "\u2705" if code == "REVIEW_PASSED" else "\u274c"
        print(f"  {icon} {f}")
        if summary:
            print(f"     {summary}")
    print()
    print(f"  PASSED: {passed}  |  REJECTED/OTHER: {rejected}  |  TOTAL: {len(results)}")
    print("=" * 60)

    return exit_code


def _should_try_daemon(args: argparse.Namespace) -> bool:
    """Provider/model override가 데몬에서 무시되지 않도록 직접 실행을 선택한다."""
    return (
        bool(args.use_daemon)
        and not args.no_daemon
        and not args.model
        and args.provider == "mock"
    )


def _show_report(html_path: str | None = None) -> int:
    """DuckDB analytics 기반 리뷰 리포트를 출력합니다."""
    from jemmin.services.report_generator import ReportGenerator

    db_path = ROOT / ".jemmin" / "analytics.duckdb"
    if not db_path.exists():
        print("[jemmin report] analytics.duckdb 파일이 없습니다. 먼저 리뷰를 실행하세요.")
        return 1

    logger = DuckDbLogger(db_path=db_path)
    report = ReportGenerator(logger)

    if html_path:
        out = report.html_report(html_path)
        print(f"[jemmin report] HTML 리포트 생성: {out}")
    else:
        print(report.text_report())

    logger.close()
    return 0


def _show_stats() -> int:
    """DuckDB analytics.duckdb 에서 이벤트 요약을 출력합니다."""
    db_path = ROOT / ".jemmin" / "analytics.duckdb"
    if not db_path.exists():
        print(
            "[jemmin stats] analytics.duckdb 파일이 없습니다. "
            "먼저 리뷰를 실행하세요 (jemmin_cli.py --file ... --diff ...)."
        )
        return 1

    logger = DuckDbLogger(db_path=db_path)
    summary = logger.cost_summary()

    if not summary:
        print("[jemmin stats] 기록된 이벤트가 없습니다.")
        logger.close()
        return 0

    header = f"{'event_type':<28} {'events':>7} {'input_tok':>10} {'output_tok':>11} {'cost_usd':>10}"
    sep = "-" * len(header)
    print()
    print("=== Nitpicker Analytics Summary ===")
    print(header)
    print(sep)
    for row in summary:
        print(
            f"{row.get('event_type', ''):<28} "
            f"{row.get('events', 0):>7} "
            f"{(row.get('total_input_tokens') or 0):>10} "
            f"{(row.get('total_output_tokens') or 0):>11} "
            f"{(row.get('total_cost_usd') or 0.0):>10.4f}"
        )
    print()

    # 추가: 전체 이벤트 수 및 최근 10건
    all_rows = logger.query(
        "SELECT recorded_at, event_type, status, result_code "
        "FROM review_events ORDER BY id DESC LIMIT 10"
    )
    if all_rows:
        print("=== 최근 10건 ===")
        for r in all_rows:
            ts = (r.get("recorded_at") or "")[:19]
            print(
                f"  {ts}  {r.get('event_type', ''):<25} "
                f"{r.get('status', ''):<12} {r.get('result_code', '') or ''}"
            )
        print()

    logger.close()
    return 0


def main() -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(
        description="Nitpicker Daemon CLI — 코드 리뷰 요청 및 분석 통계 조회"
    )
    parser.add_argument("--file", help="리뷰 대상 파일 경로")
    parser.add_argument("--diff", help="리뷰할 unified diff 텍스트 (짧은 diff용)")
    parser.add_argument("--diff-file", help="파일에서 unified diff 읽기 (UTF-8/UTF-16 자동 감지)")
    parser.add_argument("--diff-stdin", action="store_true", help="stdin에서 unified diff 읽기")
    parser.add_argument("--staged", action="store_true", help="git staged 파일 전체 리뷰 (multi-file PR)")
    parser.add_argument("--no-daemon", action="store_true", help="데몬 연결 시도 없이 직접 실행")
    parser.add_argument(
        "--use-daemon",
        action="store_true",
        help="legacy mock 데몬 경로 사용 (--provider mock 전용)",
    )
    parser.add_argument("--stats", action="store_true", help="DuckDB 분석 요약 출력")
    parser.add_argument("--report", action="store_true", help="리뷰 리포트 출력 (텍스트)")
    parser.add_argument("--report-html", metavar="PATH", help="HTML 리포트 파일 생성")
    parser.add_argument(
        "--provider",
        choices=["mock", "ollama", "gemini"],
        default="ollama",
        help="LLM Provider 선택 (기본: ollama, 테스트: mock, 클라우드: gemini)",
    )
    parser.add_argument(
        "--model",
        help="Ollama 모델 명시 override (예: qwen2.5-coder:3b)",
    )
    args = parser.parse_args()

    if args.stats:
        return _show_stats()

    if args.report or args.report_html:
        return _show_report(html_path=args.report_html)

    if args.staged:
        return _run_staged(provider_name=args.provider, model=args.model)

    diff_text = _resolve_diff_text(args, parser)
    if not args.file or not diff_text:
        parser.error(
            "--stats / --staged / --report 없이 사용할 경우 --file 과 "
            "--diff/--diff-file/--diff-stdin 중 하나가 필요합니다."
        )

    if _should_try_daemon(args):
        result = _run_via_daemon(args.file, diff_text)
        if result is not None:
            return result

    return _run_direct(args.file, diff_text, provider_name=args.provider, model=args.model)


if __name__ == "__main__":
    raise SystemExit(main())

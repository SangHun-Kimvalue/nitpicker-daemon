from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from jemmin.ipc.offload_gateway import StubOffloadGateway
from jemmin.ipc.zmq_router import ZmqRouter
from jemmin.models import ReviewRequest
from jemmin.orchestrator.consensus import DefaultConsensusEngine
from jemmin.orchestrator.controller import ReviewOrchestrator
from jemmin.orchestrator.policy_engine import DefaultPolicyEngine
from jemmin.orchestrator.resource_mgr import DefaultResourceManager
from jemmin.providers.local_llm import MockLocalLLMProvider
from jemmin.registry import create_default_registry
from jemmin.services.context_svc import StaticContextService
from jemmin.services.feedback_svc import FileFeedbackService
from jemmin.services.patch_svc import PatchService
from jemmin.services.review_logger import JsonlReviewLogger
from jemmin.services.verification_svc import VerificationService
from jemmin.state.sqlite_spooler import SQLiteJobStore
from jemmin.services.cache_mgr import ContextCacheManager
from jemmin.triggers import DaemonTriggerAdapter, TriggerEvent
from jemmin.utils.duckdb_logger import DuckDbLogger

RUNTIME_DIR = ROOT / ".jemmin"
BIND_ADDRESS = "tcp://127.0.0.1:5555"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


def _build_orchestrator() -> ReviewOrchestrator:
    RUNTIME_DIR.mkdir(exist_ok=True)
    (RUNTIME_DIR / "logs").mkdir(exist_ok=True)
    (RUNTIME_DIR / "patches").mkdir(exist_ok=True)
    provider = MockLocalLLMProvider()
    registry = create_default_registry(provider=provider)
    cache_mgr: ContextCacheManager = ContextCacheManager(provider=provider, ttl_seconds=3600, max_entries=64)
    return ReviewOrchestrator(
        job_store=SQLiteJobStore(RUNTIME_DIR / "spool.db"),
        policy_engine=DefaultPolicyEngine(max_file_size_bytes=1_048_576),
        resource_manager=DefaultResourceManager(token_budget=200_000),
        context_service=StaticContextService(
            review_log_path=RUNTIME_DIR / "logs" / "review_history.jsonl",
        ),
        agent_registry=registry,
        consensus_engine=DefaultConsensusEngine(),
        feedback_service=FileFeedbackService(RUNTIME_DIR / "logs" / "LATEST_REVIEW.txt"),
        review_logger=JsonlReviewLogger(RUNTIME_DIR / "logs" / "review_history.jsonl"),
        patch_service=PatchService(patches_dir=RUNTIME_DIR / "patches", project_root=ROOT),
        verification_service=VerificationService(project_root=ROOT),
        analytics_logger=DuckDbLogger(db_path=RUNTIME_DIR / "analytics.duckdb"),
        context_cache_manager=cache_mgr,
        offload_gateway=StubOffloadGateway(),
    )


_daemon_adapter = DaemonTriggerAdapter()


def _build_request(payload: dict[str, Any]) -> ReviewRequest:
    """통합 DaemonTriggerAdapter를 통한 요청 생성 — 입력 계약 통일."""
    event = TriggerEvent(source="daemon", payload=payload)
    return _daemon_adapter.build_request(event)


async def main() -> int:
    orchestrator: ReviewOrchestrator = _build_orchestrator()
    router: ZmqRouter = ZmqRouter(
        bind_address=BIND_ADDRESS,
        max_concurrent_jobs=10,
        poll_timeout_ms=250,
    )

    async def handle_review_request(payload: dict[str, Any]) -> dict[str, Any]:
        request: ReviewRequest = _build_request(payload)
        result = await asyncio.to_thread(orchestrator.run_once, request)
        return {
            "request_id": result.request_id,
            "state": result.state.value,
            "status": result.status,
            "summary": result.summary,
            "confidence_score": result.confidence_score,
            "result_code": result.result_code,
        }

    router.register_handler("review.request", handle_review_request)

    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()

    router_task: asyncio.Task = loop.create_task(router.start())

    def _on_signal() -> None:
        logging.info("[jemmin_daemon] 종료 시그널 수신")
        router_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass  # Windows: SIGTERM not supported via add_signal_handler

    logging.info(f"[jemmin_daemon] ReviewOrchestrator 준비 완료")
    logging.info(f"[jemmin_daemon] IPC 주소: {BIND_ADDRESS}")
    logging.info("[jemmin_daemon] Ctrl+C 로 종료")

    try:
        await router_task
    except asyncio.CancelledError:
        pass

    logging.info("[jemmin_daemon] 종료 완료")
    return 0


if __name__ == "__main__":
    # Windows: pyzmq async는 ProactorEventLoop을 지원하지 않으므로
    # SelectorEventLoop 정책을 강제합니다.
    import sys as _sys
    if _sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(main()))

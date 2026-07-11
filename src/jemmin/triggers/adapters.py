"""Concrete trigger adapters — CLI, Daemon(ZMQ), LSP, Git Hook.

모든 채널의 ReviewRequest 생성 로직을 한 곳에 통합하여
project_id, idempotency_key, trigger_intent 등의
입력 계약 drift를 방지합니다.
"""
from __future__ import annotations

import hashlib
from typing import Any

from jemmin.models import ReviewRequest

from .base import TriggerEvent

# ---------------------------------------------------------------------------
# 공용 해시 유틸
# ---------------------------------------------------------------------------

_DEFAULT_PROJECT_ID = "auto_research_review"
_DEFAULT_PROFILE = "general"


def _digest(target_file: str, diff_text: str) -> str:
    """target_file + diff_text 기반 SHA256 다이제스트 (통일된 입력 계약)."""
    return hashlib.sha256(
        (target_file + diff_text).encode("utf-8")
    ).hexdigest()


def _build_common(
    *,
    target_file: str,
    diff_text: str,
    trigger: str,
    trigger_intent: str = "active_intent",
    request_id: str | None = None,
    project_id: str | None = None,
    project_profile: str | None = None,
    git_revision: str = "workspace",
    metadata: dict[str, Any] | None = None,
) -> ReviewRequest:
    """모든 채널이 공유하는 ReviewRequest 생성 로직."""
    digest = _digest(target_file, diff_text)
    return ReviewRequest(
        request_id=request_id or f"req_{digest[:12]}",
        idempotency_key=f"idemp_{digest[:16]}",
        project_id=project_id or _DEFAULT_PROJECT_ID,
        project_profile=project_profile or _DEFAULT_PROFILE,
        trigger=trigger,
        trigger_intent=trigger_intent,
        target_file=target_file,
        git_revision=git_revision,
        base_file_hash=digest,
        diff_text=diff_text,
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# CLI Adapter
# ---------------------------------------------------------------------------


class CliTriggerAdapter:
    """CLI(bin/jemmin_cli.py) 전용 트리거 어댑터."""

    name: str = "cli"

    def supports(self, event: TriggerEvent) -> bool:
        return event.source == "cli"

    def build_request(self, event: TriggerEvent) -> ReviewRequest:
        p = event.payload
        return _build_common(
            target_file=p.get("target_file", ""),
            diff_text=p.get("diff_text", ""),
            trigger="cli",
            trigger_intent=p.get("trigger_intent", "active_intent"),
            project_id=p.get("project_id"),
            project_profile=p.get("project_profile"),
            git_revision=p.get("git_revision", "workspace"),
            metadata=event.metadata,
        )


# ---------------------------------------------------------------------------
# Daemon (ZMQ) Adapter
# ---------------------------------------------------------------------------


class DaemonTriggerAdapter:
    """Daemon ZMQ(bin/jemmin_daemon.py) 전용 트리거 어댑터."""

    name: str = "daemon"

    def supports(self, event: TriggerEvent) -> bool:
        return event.source in ("daemon", "zmq")

    def build_request(self, event: TriggerEvent) -> ReviewRequest:
        p = event.payload
        return _build_common(
            target_file=p.get("target_file", ""),
            diff_text=p.get("diff_text", ""),
            trigger=p.get("trigger", "cli"),
            trigger_intent=p.get("trigger_intent", "active_intent"),
            request_id=p.get("request_id"),
            project_id=p.get("project_id"),
            project_profile=p.get("project_profile"),
            git_revision=p.get("git_revision", "workspace"),
            metadata=event.metadata,
        )


# ---------------------------------------------------------------------------
# LSP Adapter (향후 확장)
# ---------------------------------------------------------------------------


class LspTriggerAdapter:
    """LSP 서버 트리거 어댑터 — 파일 저장 이벤트로부터 요청 생성."""

    name: str = "lsp"

    def supports(self, event: TriggerEvent) -> bool:
        return event.source == "lsp"

    def build_request(self, event: TriggerEvent) -> ReviewRequest:
        p = event.payload
        return _build_common(
            target_file=p.get("target_file", ""),
            diff_text=p.get("diff_text", ""),
            trigger="lsp",
            trigger_intent="passive_save",
            git_revision=p.get("git_revision", "workspace"),
            metadata=event.metadata,
        )


# ---------------------------------------------------------------------------
# Git Hook Adapter (향후 확장)
# ---------------------------------------------------------------------------


class GitHookTriggerAdapter:
    """Git pre-commit/post-commit hook 트리거 어댑터."""

    name: str = "git_hook"

    def supports(self, event: TriggerEvent) -> bool:
        return event.source == "git_hook"

    def build_request(self, event: TriggerEvent) -> ReviewRequest:
        p = event.payload
        return _build_common(
            target_file=p.get("target_file", ""),
            diff_text=p.get("diff_text", ""),
            trigger="git_hook",
            trigger_intent="active_intent",
            git_revision=p.get("git_revision", "HEAD"),
            metadata=event.metadata,
        )

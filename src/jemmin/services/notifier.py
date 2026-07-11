"""NotificationService — 리뷰 결과를 능동적으로 알려주는 알림 채널.

데몬이 백그라운드에서 돌 때, LATEST_REVIEW.txt를 직접 열어보지 않아도
리뷰 완료를 알 수 있도록 알림을 보냅니다.

지원 채널:
  - Windows 토스트 알림 (PowerShell 기반, 추가 의존성 없음)
  - Webhook (Slack / Discord / Generic) — URL + format만 설정하면 어디든 전송

Webhook format 종류:
  - "slack"   : Slack Incoming Webhook (attachments 형식)
  - "discord" : Discord Webhook (embeds 형식)
  - "generic" : 범용 JSON POST (title, body, status 평문)
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

__all__ = ["NotificationService"]

_logger = logging.getLogger(__name__)

_STATUS_ICON: dict[str, str] = {
    "pass": "\u2705",       # ✅
    "rejected": "\u274c",   # ❌
    "degraded": "\u26a0",   # ⚠
    "failed": "\U0001f6a8",  # 🚨
    "ignored": "\u23ed",    # ⏭
}


# ---------------------------------------------------------------------------
# Webhook payload builders — format별 분리
# ---------------------------------------------------------------------------

def _build_slack_payload(title: str, body: str, detail: str, status: str) -> dict[str, Any]:
    color = "#36a64f" if status == "pass" else "#dc3545"
    return {
        "attachments": [{
            "color": color,
            "title": title,
            "text": body,
            "footer": detail or "Nitpicker Daemon",
            "mrkdwn_in": ["text"],
        }]
    }


def _build_discord_payload(title: str, body: str, detail: str, status: str) -> dict[str, Any]:
    color = 0x36A64F if status == "pass" else 0xDC3545
    embed: dict[str, Any] = {
        "title": title,
        "description": body,
        "color": color,
    }
    if detail:
        embed["footer"] = {"text": detail}
    return {"embeds": [embed]}


def _build_generic_payload(title: str, body: str, detail: str, status: str) -> dict[str, Any]:
    return {
        "title": title,
        "body": body,
        "detail": detail,
        "status": status,
        "source": "Nitpicker Daemon",
    }


_WEBHOOK_BUILDERS: dict[str, Any] = {
    "slack": _build_slack_payload,
    "discord": _build_discord_payload,
    "generic": _build_generic_payload,
}


# ---------------------------------------------------------------------------
# NotificationService
# ---------------------------------------------------------------------------

class NotificationService:
    """리뷰 결과를 알림으로 전달합니다.

    사용법::

        notifier = NotificationService(
            toast_enabled=True,
            webhook_url="https://hooks.slack.com/services/...",
            webhook_format="slack",
        )
        notifier.notify(result, target_file="src/main.py")

    webhook_format: "slack" | "discord" | "generic" (기본: "slack")
    """

    def __init__(
        self,
        *,
        toast_enabled: bool = False,
        webhook_url: str = "",
        webhook_format: str = "slack",
        # 하위 호환: slack_webhook_url도 지원
        slack_webhook_url: str = "",
    ) -> None:
        self._toast_enabled = toast_enabled and sys.platform == "win32"
        self._webhook_url = (webhook_url or slack_webhook_url).strip()
        self._webhook_format = webhook_format if webhook_url else "slack"

    @property
    def enabled(self) -> bool:
        return self._toast_enabled or bool(self._webhook_url)

    def notify(self, result: Any, *, target_file: str = "") -> None:
        """리뷰 결과를 설정된 모든 채널로 알림."""
        status = getattr(result, "status", "")
        summary = getattr(result, "summary", "")
        result_code = getattr(result, "result_code", "") or ""
        confidence = getattr(result, "confidence_score", 0.0)
        reviewer = getattr(result, "reviewer", "")

        icon = _STATUS_ICON.get(status, "")
        title = f"{icon} Nitpicker: {result_code}"
        body = summary[:200] if summary else "(no summary)"
        detail = f"{target_file} | {reviewer} | {confidence:.0%}" if target_file else ""

        if self._toast_enabled:
            self._send_toast(title, body, detail)
        if self._webhook_url:
            self._send_webhook(title, body, detail, status)

    def _send_toast(self, title: str, body: str, detail: str) -> None:
        """Windows 토스트 알림 — PowerShell AppToast (추가 의존성 없음)."""
        try:
            text_lines = body
            if detail:
                text_lines += f"\n{detail}"
            ps_script = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                "ContentType = WindowsRuntime] > $null; "
                "$template = [Windows.UI.Notifications.ToastNotificationManager]"
                "::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                "$text = $template.GetElementsByTagName('text'); "
                f"$text.Item(0).AppendChild($template.CreateTextNode('{_ps_escape(title)}')) > $null; "
                f"$text.Item(1).AppendChild($template.CreateTextNode('{_ps_escape(text_lines)}')) > $null; "
                "$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
                "[Windows.UI.Notifications.ToastNotificationManager]"
                "::CreateToastNotifier('Nitpicker Daemon').Show($toast)"
            )
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", ps_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            _logger.debug("[Notifier] Toast failed: %s", exc)

    def _send_webhook(self, title: str, body: str, detail: str, status: str) -> None:
        """Webhook 전송 — format에 따라 payload를 빌드."""
        builder = _WEBHOOK_BUILDERS.get(self._webhook_format, _build_generic_payload)
        payload = builder(title, body, detail, status)
        try:
            req = Request(
                self._webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=5) as resp:
                if resp.status not in (200, 204):
                    _logger.warning("[Notifier] Webhook returned %d", resp.status)
        except (URLError, OSError, ValueError) as exc:
            _logger.debug("[Notifier] Webhook failed: %s", exc)


def _ps_escape(text: str) -> str:
    """PowerShell 문자열 안의 작은따옴표를 이스케이프."""
    return text.replace("'", "''").replace("\n", "`n")

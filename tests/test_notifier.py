"""Phase VI-B: NotificationService 테스트.

§1 NotificationService 단위 테스트           (7 tests)
§2 Webhook format별 테스트 (Slack/Discord/Generic) (3 tests)
§3 ArtifactPublisher 알림 통합 테스트        (3 tests)
Total: 15 tests
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from jemmin.models import ReviewResult, ReviewState
from jemmin.services.artifact_publisher import ArtifactPublisher
from jemmin.services.notifier import (
    NotificationService,
    _build_discord_payload,
    _build_generic_payload,
    _build_slack_payload,
    _ps_escape,
)


def _result(status: str = "pass", code: str = "REVIEW_PASSED") -> ReviewResult:
    return ReviewResult(
        request_id="notif-test",
        state=ReviewState.DELIVERED,
        status=status,
        summary="테스트 리뷰 요약",
        confidence_score=0.95,
        result_code=code,
        reviewer="OllamaProvider/qwen2.5-coder:7b",
    )


# ---------------------------------------------------------------------------
# §1 NotificationService 단위 테스트
# ---------------------------------------------------------------------------


class TestNotificationService:
    def test_disabled_by_default(self):
        ns = NotificationService()
        assert not ns.enabled

    def test_toast_enabled(self):
        ns = NotificationService(toast_enabled=True)
        # Windows가 아니면 False, Windows이면 True
        import sys
        assert ns.enabled == (sys.platform == "win32")

    def test_slack_enabled(self):
        ns = NotificationService(slack_webhook_url="https://hooks.slack.com/test")
        assert ns.enabled

    def test_both_enabled(self):
        ns = NotificationService(toast_enabled=True, slack_webhook_url="https://hooks.slack.com/test")
        assert ns.enabled

    @patch("jemmin.services.notifier.subprocess.Popen")
    def test_toast_calls_powershell(self, mock_popen):
        """토스트 알림이 PowerShell을 호출하는지 확인."""
        ns = NotificationService(toast_enabled=True)
        ns._toast_enabled = True  # force enable regardless of platform
        ns.notify(_result(), target_file="src/main.py")
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert args[0] == "powershell"
        assert "Nitpicker" in args[-1]

    @patch("jemmin.services.notifier.urlopen")
    def test_slack_sends_webhook(self, mock_urlopen):
        """Slack webhook이 호출되는지 확인."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        ns = NotificationService(slack_webhook_url="https://hooks.slack.com/test")
        ns.notify(_result(status="rejected", code="REVIEW_REJECTED"), target_file="src/main.py")
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        import json
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["attachments"][0]["color"] == "#dc3545"  # red for reject
        assert "REVIEW_REJECTED" in payload["attachments"][0]["title"]

    def test_notify_no_error_when_disabled(self):
        """비활성 상태에서 notify 호출해도 오류 없음."""
        ns = NotificationService()
        ns.notify(_result())  # no exception

    @patch("jemmin.services.notifier.urlopen")
    def test_discord_webhook(self, mock_urlopen):
        """Discord webhook format 검증."""
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        ns = NotificationService(
            webhook_url="https://discord.com/api/webhooks/123/abc",
            webhook_format="discord",
        )
        ns.notify(_result(status="pass"), target_file="src/main.py")
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        import json
        payload = json.loads(req.data.decode("utf-8"))
        assert "embeds" in payload
        assert payload["embeds"][0]["color"] == 0x36A64F

    @patch("jemmin.services.notifier.urlopen")
    def test_generic_webhook(self, mock_urlopen):
        """Generic webhook format 검증."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        ns = NotificationService(
            webhook_url="https://my-server.com/hook",
            webhook_format="generic",
        )
        ns.notify(_result(), target_file="src/main.py")
        req = mock_urlopen.call_args[0][0]
        import json
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["source"] == "Nitpicker Daemon"
        assert payload["status"] == "pass"

    def test_backward_compat_slack_webhook_url(self):
        """하위 호환: slack_webhook_url 파라미터도 동작."""
        ns = NotificationService(slack_webhook_url="https://hooks.slack.com/x")
        assert ns.enabled
        assert ns._webhook_format == "slack"


# ---------------------------------------------------------------------------
# §2 Webhook payload builders
# ---------------------------------------------------------------------------


class TestWebhookBuilders:
    def test_slack_payload_structure(self):
        p = _build_slack_payload("title", "body", "detail", "pass")
        assert "attachments" in p
        assert p["attachments"][0]["color"] == "#36a64f"

    def test_discord_payload_structure(self):
        p = _build_discord_payload("title", "body", "detail", "rejected")
        assert "embeds" in p
        assert p["embeds"][0]["color"] == 0xDC3545
        assert p["embeds"][0]["footer"]["text"] == "detail"

    def test_generic_payload_structure(self):
        p = _build_generic_payload("title", "body", "detail", "pass")
        assert p["source"] == "Nitpicker Daemon"
        assert p["title"] == "title"


class TestPsEscape:
    def test_single_quote_escaped(self):
        assert _ps_escape("it's") == "it''s"

    def test_newline_escaped(self):
        assert _ps_escape("a\nb") == "a`nb"


# ---------------------------------------------------------------------------
# §2 ArtifactPublisher 알림 통합 테스트
# ---------------------------------------------------------------------------


class TestArtifactPublisherNotification:
    def test_publish_result_sends_notification(self):
        notifier = MagicMock()
        pub = ArtifactPublisher(notifier=notifier)
        result = _result()
        pub.publish_result(result, target_file="src/main.py")
        notifier.notify.assert_called_once_with(result, target_file="src/main.py")

    def test_notification_failure_isolated(self):
        """알림 실패가 다른 채널에 영향을 주지 않아야 합니다."""
        notifier = MagicMock()
        notifier.notify.side_effect = OSError("network error")
        logger = MagicMock()
        pub = ArtifactPublisher(review_logger=logger, notifier=notifier)
        result = _result()
        pub.publish_result(result)
        logger.log_result.assert_called_once()  # 로거는 정상 호출

    def test_no_notifier_no_error(self):
        """notifier가 None이면 오류 없이 건너뜀."""
        pub = ArtifactPublisher()
        pub.publish_result(_result())  # no exception

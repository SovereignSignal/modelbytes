"""monitor.py mirrors each published digest to Slack (multi-channel publish).

Dormant unless SLACK_BOT_TOKEN + MODELBYTES_SLACK_CHANNEL_ID are set, so a
Telegram-only deploy is unaffected."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


@pytest.fixture(autouse=True)
def _disable_idempotency_db(monkeypatch):
    monkeypatch.setattr(monitor, "init_posted_digest_store", lambda: False)


# ── HTML → Slack mrkdwn conversion ──

def test_html_to_mrkdwn_bold_italic_links():
    html = '<b>Model X</b> — <i>fast</i>. <a href="https://x.ai/m">→ Source</a>'
    md = monitor._telegram_html_to_slack_mrkdwn(html)
    assert "*Model X*" in md
    assert "_fast_" in md
    assert "<https://x.ai/m|→ Source>" in md
    assert "<a href" not in md and "</b>" not in md


# ── send_slack_post ──

def test_slack_post_dormant_without_config(monkeypatch):
    monkeypatch.setattr(monitor, "SLACK_BOT_TOKEN", "")
    monkeypatch.setattr(monitor, "MODELBYTES_SLACK_CHANNEL_ID", "")
    with patch.object(monitor.requests, "post") as post:
        assert monitor.send_slack_post("<b>hi</b>") is False
        post.assert_not_called()


def test_slack_post_sends_converted_text_to_channel(monkeypatch):
    monkeypatch.setattr(monitor, "SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(monitor, "MODELBYTES_SLACK_CHANNEL_ID", "C0XXXXXXXXX")
    resp = MagicMock()
    resp.json.return_value = {"ok": True}
    with patch.object(monitor.requests, "post", return_value=resp) as post:
        assert monitor.send_slack_post("<b>ModelBytes</b>") is True
        _, kwargs = post.call_args
        assert "chat.postMessage" in post.call_args[0][0]
        assert kwargs["json"]["channel"] == "C0XXXXXXXXX"
        assert "*ModelBytes*" in kwargs["json"]["text"]


def test_slack_post_returns_false_on_api_error(monkeypatch):
    monkeypatch.setattr(monitor, "SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(monitor, "MODELBYTES_SLACK_CHANNEL_ID", "C0XXXXXXXXX")
    resp = MagicMock()
    resp.json.return_value = {"ok": False, "error": "channel_not_found"}
    with patch.object(monitor.requests, "post", return_value=resp):
        assert monitor.send_slack_post("<b>hi</b>") is False


# ── wiring: the curated publish path mirrors to Slack ──

def test_curated_path_mirrors_to_slack(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pending").mkdir()
    today = monitor.datetime.now(monitor.timezone.utc).strftime("%Y-%m-%d")
    body = "🤖 ModelBytes Digest\n\nTotal: 3 models tracked today"
    (tmp_path / "pending" / f"{today}.txt").write_text(body)

    monkeypatch.setattr(monitor, "has_posted_digest", lambda d: False)
    monkeypatch.setattr(monitor, "mark_posted_digest", lambda *a: True)
    monkeypatch.setattr(monitor, "send_telegram_post", lambda m: True)
    slack_calls = []
    monkeypatch.setattr(monitor, "send_slack_post", lambda m: slack_calls.append(m) or True)

    assert monitor.try_post_pending_curated() is True
    assert slack_calls == [body]

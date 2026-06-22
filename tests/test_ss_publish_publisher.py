"""Tests for the Publisher and its text helpers (publisher.py).

The contract these pin: never raise on the publish path, truncate oversize
bundles, retry transient 429/5xx honoring Retry-After, mirror only on success,
and route ops alerts Telegram-then-Slack in isolated try-blocks.
"""
import requests

from ss_publish import (
    Publisher,
    redact_secrets,
    retry_delay,
    truncate_for_telegram,
)


# --- inlined fakes (kept here so the vendored test is self-contained) --------


class FakeResponse:
    """Minimal stand-in for requests.Response used by the Publisher."""
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.text = text
        self.headers = headers or {}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


class FakePost:
    """A callable fake for requests.post. Scriptable per-call responses."""
    def __init__(self, responses=None, by_url=None):
        self._responses = responses
        self._by_url = by_url or {}
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._by_url:
            for fragment, factory in self._by_url.items():
                if fragment in url:
                    return factory(url, **kwargs)
        if callable(self._responses) and not isinstance(self._responses, (list, FakeResponse)):
            return self._responses(url, **kwargs)
        if isinstance(self._responses, list):
            if not self._responses:
                return FakeResponse(status_code=500, payload={"ok": False})
            return self._responses.pop(0)
        if isinstance(self._responses, FakeResponse):
            return self._responses
        return FakeResponse(payload={"ok": True, "result": {"message_id": 42}})


import pytest


@pytest.fixture
def no_sleep(monkeypatch):
    """The Publisher retries with backoff; pin sleep so tests are instant."""
    import ss_publish.publisher as pub
    monkeypatch.setattr(pub.time, "sleep", lambda *a, **k: None)


# --- truncate_for_telegram ---------------------------------------------------

def test_truncate_passthrough_short():
    assert truncate_for_telegram("short") == "short"


def test_truncate_caps_and_marks():
    big = "line\n" * 4000
    out = truncate_for_telegram(big)
    assert len(out) <= 4096
    assert out.endswith("…[truncated]")


def test_truncate_custom_limit():
    out = truncate_for_telegram("a" * 1000, limit=100)
    assert len(out) <= 100 and out.endswith("…[truncated]")


# --- redact_secrets ----------------------------------------------------------

def test_redact_scrubs_each_value():
    assert redact_secrets("tok-abc db=x", ("tok-abc", "x")) == "<redacted> db=<redacted>"


def test_redact_ignores_empty_secrets():
    # an unset env var ('') must not blank a log line
    assert redact_secrets("clean", ("", "  ")) == "clean"


def test_redact_noop_no_secrets():
    assert redact_secrets("clean") == "clean"


# --- retry_delay -------------------------------------------------------------

def test_retry_delay_retry_after_header_honored():
    resp = FakeResponse(headers={"Retry-After": "5"})
    assert retry_delay(resp, 1) == 5.0


def test_retry_delay_retry_after_capped():
    resp = FakeResponse(headers={"Retry-After": "999"})
    assert retry_delay(resp, 1) == 30.0


def test_retry_delay_linear_backoff():
    resp = FakeResponse()  # no Retry-After
    assert retry_delay(resp, 1, base=2.0) == 2.0
    assert retry_delay(resp, 3, base=2.0) == 6.0
    assert retry_delay(resp, 99, base=2.0) == 30.0  # capped


def test_retry_delay_non_numeric_retry_after():
    resp = FakeResponse(headers={"Retry-After": "soon"})
    assert retry_delay(resp, 2, base=1.0) == 2.0  # falls back to linear


def test_retry_delay_none_response():
    assert retry_delay(None, 2, base=1.0) == 2.0


# --- Publisher.send_telegram -------------------------------------------------

def _pub(monkeypatch, post, **kw):
    p = Publisher(
        telegram_token="tok",
        telegram_channel_id="-100",
        _post=post,
        **kw,
    )
    return p


def test_send_not_configured():
    p = Publisher()  # no creds
    r = p.send_telegram("hi")
    assert r.ok is False and "not configured" in r.error


def test_send_success_returns_message_id(no_sleep):
    post = FakePost()  # default success with message_id 42
    p = _pub(no_sleep, post)
    r = p.send_telegram("hi")
    assert r.ok is True and r.message_id == 42 and r.truncated is False


def test_send_truncates_oversize(no_sleep):
    sent = {}

    def factory(url, **kwargs):
        sent["text"] = kwargs["json"]["text"]
        return FakeResponse(payload={"ok": True, "result": {"message_id": 1}})

    post = FakePost(responses=factory)
    p = _pub(no_sleep, post)
    r = p.send_telegram("w\n" * 3000)
    assert r.ok is True and r.truncated is True
    assert len(sent["text"]) <= 4096 and sent["text"].endswith("…[truncated]")


def test_send_retries_429_then_succeeds(no_sleep):
    post = FakePost(responses=[
        FakeResponse(status_code=429, payload={"ok": False}, headers={"Retry-After": "0"}),
        FakeResponse(payload={"ok": True, "result": {"message_id": 7}}),
    ])
    p = _pub(no_sleep, post)
    r = p.send_telegram("hi")
    assert r.ok is True and r.message_id == 7 and len(post.calls) == 2


def test_send_persistent_500_gives_up(no_sleep):
    post = FakePost(responses=FakeResponse(status_code=500, payload={"ok": False}))
    p = _pub(no_sleep, post, send_attempts=3)
    r = p.send_telegram("hi")
    assert r.ok is False and len(post.calls) == 3  # retried up to the cap


def test_send_ok_false_is_not_retried(no_sleep):
    # ok:false is a content problem; retrying wastes budget and never helps.
    post = FakePost(responses=FakeResponse(
        status_code=400, payload={"ok": False, "description": "Bad Request: can't parse entities"}))
    p = _pub(no_sleep, post, send_attempts=3)
    r = p.send_telegram("hi")
    assert r.ok is False and "can't parse" in r.error and len(post.calls) == 1


def test_send_network_error_retries_then_false(no_sleep):
    def boom(url, **kwargs):
        raise requests.ConnectionError("down")
    post = FakePost(responses=boom)
    p = _pub(no_sleep, post, send_attempts=3)
    r = p.send_telegram("hi")
    assert r.ok is False and "network" in r.error and len(post.calls) == 3


def test_send_never_raises_on_unexpected(monkeypatch):
    # The publish-path contract: never raise, even on a non-network error
    # (a bug, an unexpected raise). Surface it as ok=False instead of aborting
    # the caller's whole loop. Both modelbytes and clawbytes catch broad
    # Exception as the final safety net; the shared core does too.
    def boom(url, **kwargs):
        raise RuntimeError("something weird")
    post = FakePost(responses=boom)
    p = _pub(monkeypatch, post, send_attempts=1)
    r = p.send_telegram("hi")
    assert r.ok is False and "unexpected" in r.error


# --- Publisher.mirror_to_slack -----------------------------------------------

def test_mirror_not_configured():
    p = Publisher()
    assert p.mirror_to_slack("hi") is False


def test_mirror_success():
    post = FakePost(responses=FakeResponse(payload={"ok": True}))
    p = Publisher(slack_token="xoxb", slack_channel_id="C1", _post=post)
    assert p.mirror_to_slack("<b>hi</b>") is True
    url, kwargs = post.calls[0]
    assert "slack.com" in url and kwargs["json"]["text"] == "*hi*"


def test_mirror_failure_returns_false():
    post = FakePost(responses=FakeResponse(payload={"ok": False, "error": "no_auth"}))
    p = Publisher(slack_token="xoxb", slack_channel_id="C1", _post=post)
    assert p.mirror_to_slack("hi") is False


def test_mirror_never_raises():
    def boom(url, **kwargs):
        raise OSError("slack gone")
    post = FakePost(responses=boom)
    p = Publisher(slack_token="xoxb", slack_channel_id="C1", _post=post)
    assert p.mirror_to_slack("hi") is False


# --- Publisher.send_ops_alert ------------------------------------------------

def test_ops_not_configured_returns_false():
    p = Publisher()
    assert p.send_ops_alert("hi") is False


def test_ops_telegram_first():
    post = FakePost(responses=FakeResponse(payload={"ok": True}))
    p = Publisher(telegram_token="tok", ops_telegram_chat_id="123", _post=post,
                  ops_banner="🔧 OPS")
    assert p.send_ops_alert("break") is True
    assert "api.telegram.org" in post.calls[0][0]
    assert "🔧 OPS" in post.calls[0][1]["json"]["text"]


def test_ops_falls_back_to_slack_when_telegram_fails():
    def by_url(url, **kwargs):
        if "api.telegram.org" in url:
            return FakeResponse(status_code=503, payload={"ok": False})
        return FakeResponse(payload={"ok": True})
    post = FakePost(responses=by_url)
    p = Publisher(telegram_token="tok", ops_telegram_chat_id="123",
                  slack_token="xoxb", ops_slack_channel_id="Cops", _post=post)
    assert p.send_ops_alert("break") is True
    assert any("slack.com" in c[0] for c in post.calls)


def test_ops_never_raises_when_both_fail():
    def boom(url, **kwargs):
        raise OSError("all down")
    post = FakePost(responses=boom)
    p = Publisher(telegram_token="tok", ops_telegram_chat_id="123",
                  slack_token="xoxb", ops_slack_channel_id="Cops", _post=post)
    assert p.send_ops_alert("break") is False  # not raise


def test_ops_redacts_secrets():
    seen = {}

    def factory(url, **kwargs):
        seen["text"] = kwargs["json"]["text"]
        return FakeResponse(payload={"ok": True})
    post = FakePost(responses=factory)
    p = Publisher(telegram_token="tok", ops_telegram_chat_id="123", _post=post,
                  secret_values=("SUPERSECRET",))
    p.send_ops_alert("error with SUPERSECRET inside")
    assert "SUPERSECRET" not in seen["text"]
    assert "<redacted>" in seen["text"]


def test_ops_no_slack_when_slack_unconfigured_and_telegram_down():
    def by_url(url, **kwargs):
        return FakeResponse(status_code=503, payload={"ok": False})
    post = FakePost(responses=by_url)
    p = Publisher(telegram_token="tok", ops_telegram_chat_id="123", _post=post)
    # no slack creds → nothing to fall back to, but must not raise
    assert p.send_ops_alert("break") is False


# --- disable_preview is configurable (the modelbytes vs clawbytes difference)

def test_disable_preview_default_true():
    post = FakePost()
    p = _pub(None, post) if False else Publisher(telegram_token="t", telegram_channel_id="c", _post=post)
    p.send_telegram("hi")
    assert post.calls[0][1]["json"]["disable_web_page_preview"] is True


def test_disable_preview_can_be_disabled():
    post = FakePost()
    p = Publisher(telegram_token="t", telegram_channel_id="c", disable_preview=False, _post=post)
    p.send_telegram("hi")
    assert post.calls[0][1]["json"]["disable_web_page_preview"] is False

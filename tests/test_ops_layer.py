"""Ops layer: run records, admin alerts, heartbeat, secret redaction.

Design-pass finding (2026-06-12, critical): every failure and degradation path
was silent. The ops layer's contract: it NEVER raises (a broken alert must not
break publishing), it never logs secrets, and every run — posted, blocked,
failed, skipped, no-models — leaves a publish_runs row when a DB is configured.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


# ── send_ops_alert ──

def test_ops_alert_noop_without_destinations(monkeypatch):
    monkeypatch.setattr(monitor, "ADMIN_CHAT_ID", "")
    monkeypatch.setattr(monitor, "OPS_SLACK_CHANNEL_ID", "")
    calls = []
    monkeypatch.setattr(monitor.requests, "post", lambda *a, **k: calls.append(a) or MagicMock())
    assert monitor.send_ops_alert("something broke") is False
    assert calls == []


def test_ops_alert_routes_to_telegram_admin(monkeypatch):
    monkeypatch.setattr(monitor, "ADMIN_CHAT_ID", "12345")
    monkeypatch.setattr(monitor, "OPS_SLACK_CHANNEL_ID", "")
    monkeypatch.setattr(monitor, "TELEGRAM_BOT_TOKEN", "tok")
    sent = []
    fake = MagicMock(); fake.ok = True
    monkeypatch.setattr(monitor.requests, "post",
                        lambda url, **k: sent.append((url, k)) or fake)
    assert monitor.send_ops_alert("something broke") is True
    url, kwargs = sent[0]
    assert "sendMessage" in url
    assert kwargs["json"]["chat_id"] == "12345"
    assert "something broke" in kwargs["json"]["text"]


def test_ops_alert_falls_back_to_slack(monkeypatch):
    monkeypatch.setattr(monitor, "ADMIN_CHAT_ID", "")
    monkeypatch.setattr(monitor, "OPS_SLACK_CHANNEL_ID", "C0OPS")
    monkeypatch.setattr(monitor, "SLACK_BOT_TOKEN", "xoxb-test")
    sent = []
    fake = MagicMock(); fake.ok = True; fake.json.return_value = {"ok": True}
    monkeypatch.setattr(monitor.requests, "post",
                        lambda url, **k: sent.append((url, k)) or fake)
    assert monitor.send_ops_alert("oops") is True
    url, kwargs = sent[0]
    assert "chat.postMessage" in url
    assert kwargs["json"]["channel"] == "C0OPS"


def test_ops_alert_never_raises(monkeypatch):
    monkeypatch.setattr(monitor, "ADMIN_CHAT_ID", "12345")
    monkeypatch.setattr(monitor, "TELEGRAM_BOT_TOKEN", "tok")
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(monitor.requests, "post", boom)
    assert monitor.send_ops_alert("x") is False  # swallowed, not raised


# ── secret redaction ──

def test_redact_secrets_strips_bot_token(monkeypatch):
    monkeypatch.setattr(monitor, "TELEGRAM_BOT_TOKEN", "12345:AAsecretsecret")
    out = monitor._redact_secrets(
        "error at https://api.telegram.org/bot12345:AAsecretsecret/sendMessage")
    assert "AAsecretsecret" not in out
    assert "<token>" in out


def test_telegram_send_error_is_redacted(monkeypatch, capsys):
    monkeypatch.setattr(monitor, "TELEGRAM_BOT_TOKEN", "12345:AAsecretsecret")
    monkeypatch.setattr(monitor, "TELEGRAM_CHANNEL_ID", "-100123")
    def boom(url, **k):
        raise RuntimeError(f"failed for {url}")
    monkeypatch.setattr(monitor.requests, "post", boom)
    assert monitor.send_telegram_post("hi") is False
    assert "AAsecretsecret" not in capsys.readouterr().err


# ── heartbeat ──

def test_heartbeat_noop_without_url(monkeypatch):
    monkeypatch.setattr(monitor, "HEARTBEAT_URL", "")
    calls = []
    monkeypatch.setattr(monitor.requests, "post", lambda *a, **k: calls.append(a))
    monitor.ping_heartbeat(True)
    assert calls == []


def test_heartbeat_pings_fail_endpoint_on_failure(monkeypatch):
    monkeypatch.setattr(monitor, "HEARTBEAT_URL", "https://hc.example/abc")
    calls = []
    monkeypatch.setattr(monitor.requests, "post",
                        lambda url, **k: calls.append(url) or MagicMock())
    monitor.ping_heartbeat(False, "QA blocked")
    assert calls == ["https://hc.example/abc/fail"]


def test_heartbeat_never_raises(monkeypatch):
    monkeypatch.setattr(monitor, "HEARTBEAT_URL", "https://hc.example/abc")
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(monitor.requests, "post", boom)
    monitor.ping_heartbeat(True)  # must not raise


# ── publish_runs ──

def test_record_publish_run_noop_without_db(monkeypatch):
    monkeypatch.setattr(monitor, "DATABASE_URL", "")
    assert monitor.record_publish_run("2026-06-12", "curated", "posted") is False


def test_fallback_streak_zero_without_db(monkeypatch):
    monkeypatch.setattr(monitor, "DATABASE_URL", "")
    assert monitor.fallback_streak() == 0


# ── telegram message_id capture + no unfurl ──

def test_send_telegram_captures_message_id_and_disables_preview(monkeypatch):
    monkeypatch.setattr(monitor, "TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr(monitor, "TELEGRAM_CHANNEL_ID", "-100123")
    fake = MagicMock(); fake.ok = True
    fake.json.return_value = {"ok": True, "result": {"message_id": 222}}
    captured = {}
    def post(url, **k):
        captured.update(k)
        return fake
    monkeypatch.setattr(monitor.requests, "post", post)
    assert monitor.send_telegram_post("hello") is True
    assert monitor.LAST_TELEGRAM_MESSAGE_ID == 222
    assert captured["json"]["disable_web_page_preview"] is True


# ── live-mode guards in main() ──

def test_main_fatal_when_live_without_database(monkeypatch, capsys):
    monkeypatch.setattr(monitor, "TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr(monitor, "TELEGRAM_CHANNEL_ID", "-100123")
    monkeypatch.setattr(monitor, "DATABASE_URL", "")
    alerts = []
    monkeypatch.setattr(monitor, "send_ops_alert", lambda t: alerts.append(t) or True)
    monkeypatch.setattr(monitor, "ping_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "try_post_pending_curated", lambda: False)
    rc = monitor.main()
    assert rc == 1
    assert alerts and "DATABASE_URL" in alerts[0]


def test_main_refuses_silent_seed_without_flag(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MODELBYTES_ALLOW_SEED", raising=False)
    monkeypatch.setattr(monitor, "TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr(monitor, "TELEGRAM_CHANNEL_ID", "-100123")
    monkeypatch.setattr(monitor, "DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(monitor, "try_post_pending_curated", lambda: False)
    monkeypatch.setattr(monitor, "init_database", lambda: None)
    monkeypatch.setattr(monitor, "load_seen_models", lambda: set())  # looks like first run
    monkeypatch.setattr(monitor, "save_seen_models", lambda s: None)
    for fetcher in ["fetch_openrouter_models", "fetch_ollama_models",
                    "fetch_huggingface_trending", "fetch_major_orgs",
                    "fetch_hf_text_generation"]:
        monkeypatch.setattr(monitor, fetcher, lambda: [])
    alerts = []
    monkeypatch.setattr(monitor, "send_ops_alert", lambda t: alerts.append(t) or True)
    monkeypatch.setattr(monitor, "ping_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "record_publish_run", lambda *a, **k: True)
    rc = monitor.main()
    assert rc == 1
    assert alerts and "seed" in alerts[0].lower()


def test_inline_primary_suppresses_fallback_alert(monkeypatch, tmp_path):
    # With the curator retired (INLINE_PRIMARY=1), a successful inline publish
    # must NOT alert "published via fallback" — that's the normal daily path now.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(monitor, "INLINE_PRIMARY", True)
    monkeypatch.setattr(sys, "argv", ["monitor.py"])
    monkeypatch.setattr(monitor, "DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(monitor, "TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr(monitor, "TELEGRAM_CHANNEL_ID", "-100123")
    monkeypatch.setattr(monitor, "try_post_pending_curated", lambda: False)
    monkeypatch.setattr(monitor, "init_database", lambda: None)
    monkeypatch.setattr(monitor, "load_seen_models", lambda: {"seed/x"})
    monkeypatch.setattr(monitor, "save_seen_models", lambda s: None)
    m = monitor.ModelRelease(name="meta-llama/Llama-4-70B", provider="Meta",
                             source="huggingface-org", url="https://hf.co/meta-llama/Llama-4-70B",
                             description="x", is_open_source=True,
                             release_date=monitor.datetime.now(monitor.timezone.utc).strftime("%Y-%m-%d"))
    monkeypatch.setattr(monitor, "fetch_openrouter_models", lambda: [m])
    for f in ["fetch_ollama_models", "fetch_huggingface_trending",
              "fetch_major_orgs", "fetch_hf_text_generation"]:
        monkeypatch.setattr(monitor, f, lambda: [])
    monkeypatch.setattr(monitor, "enrich_with_hf_cards", lambda models: None)
    monkeypatch.setattr(monitor, "summarize_models",
                        lambda models: "🤖 <b>ModelBytes Digest</b>\n<i>x</i>\n\n━━━ <b>OPEN FRONTIER</b> 🔓\n<b>Llama 4 70B</b> — <i>x</i> <a href=\"https://h\">→ S</a>\n\nTotal: 1 items tracked today")
    monkeypatch.setattr(monitor, "send_telegram_post", lambda m: True)
    monkeypatch.setattr(monitor, "send_slack_post", lambda m: True)
    monkeypatch.setattr(monitor, "mark_posted_digest", lambda *a, **k: True)
    monkeypatch.setattr(monitor, "ping_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "record_publish_run", lambda *a, **k: True)
    alerts = []
    monkeypatch.setattr(monitor, "send_ops_alert", lambda t: alerts.append(t) or True)

    rc = monitor.main()
    assert rc == 0
    assert not any("FALLBACK" in a or "fallback" in a for a in alerts), alerts

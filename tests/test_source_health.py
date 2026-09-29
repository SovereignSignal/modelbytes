"""Per-source health logs, persisted streak state, and 24h admin alerts.

Visibility only: these tests lock the log line, the empty/error threshold,
alert dedupe, and a missing or corrupt state file. They do not change what
the publisher posts.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


T0 = datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc)


def _rows_error(source="OpenRouter", items=0, error="Timeout: down"):
    return [{"source": source, "items": items, "error": error}]


def test_flush_logs_one_line_per_source(capsys, tmp_path):
    path = tmp_path / "source_health.json"
    monitor.flush_source_health(
        [
            {"source": "OpenRouter", "items": 4, "error": ""},
            {"source": "Ollama", "items": 0, "error": ""},
            {"source": "HuggingFace-Trending", "items": 0, "error": "HTTPError: 500"},
        ],
        now=T0,
        path=path,
    )
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.startswith("source_health source=")]
    assert lines == [
        "source_health source=OpenRouter status=ok items=4 error=-",
        "source_health source=Ollama status=empty items=0 error=-",
        "source_health source=HuggingFace-Trending status=error items=0 error=HTTPError: 500",
    ]


def test_partial_failure_with_items_stays_ok(tmp_path):
    path = tmp_path / "source_health.json"
    monitor.flush_source_health(
        [{"source": "HuggingFace-Orgs", "items": 2, "error": "HTTPError: one org"}],
        now=T0,
        path=path,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    rec = saved["HuggingFace-Orgs"]
    assert rec["consecutive_failures"] == 0
    assert rec["consecutive_empties"] == 0
    assert rec["last_ok"]
    assert rec["unhealthy_since"] is None


def test_swallowed_fetcher_errors_keep_empty_fallback_and_record_reason(monkeypatch):
    monkeypatch.setattr(monitor, "_SOURCE_ERRORS", {})

    def boom(*_a, **_k):
        raise RuntimeError("catalog down")

    monkeypatch.setattr(monitor, "_http_get", boom)
    assert monitor.fetch_openrouter_models() == []
    assert monitor.fetch_ollama_models() == []
    assert monitor.fetch_hf_text_generation() == []
    assert monitor.fetch_huggingface_trending() == []
    assert monitor.fetch_org_models("deepseek-ai") == []
    assert monitor.fetch_org_models("Qwen") == []

    assert "catalog down" in monitor._consume_source_error("OpenRouter")
    assert "catalog down" in monitor._consume_source_error("Ollama")
    assert "catalog down" in monitor._consume_source_error("HuggingFace-Top-TextGen")
    assert "catalog down" in monitor._consume_source_error("HuggingFace-Trending")
    org_err = monitor._consume_source_error("HuggingFace-Orgs")
    assert "catalog down" in org_err
    assert "(+1 more)" in org_err


def test_source_error_is_truncated_and_redacts_secrets(monkeypatch):
    secret = "super-secret-token-value"
    monkeypatch.setattr(monitor, "TELEGRAM_BOT_TOKEN", secret)
    monkeypatch.setattr(monitor, "PARALLEL_API_KEY", "parallel-key-secret")
    monkeypatch.setattr(monitor, "_SOURCE_ERRORS", {})
    exc = RuntimeError("failed " + secret + " parallel-key-secret " + ("x" * 400))
    reason = monitor._remember_source_error("OpenRouter", exc)
    assert secret not in reason
    assert "parallel-key-secret" not in reason
    assert len(reason) <= 200
    assert monitor._consume_source_error("OpenRouter")


def test_discovery_failure_returns_empty_and_records_redacted_reason(monkeypatch):
    monkeypatch.setattr(monitor, "_SOURCE_ERRORS", {})
    monkeypatch.setattr(monitor, "DISCOVERY_ENABLED", True)
    monkeypatch.setattr(monitor, "PARALLEL_API_KEY", "parallel-key-secret")

    def boom(*_a, **_k):
        raise RuntimeError("parallel-key-secret boom")

    monkeypatch.setattr(monitor.requests, "post", boom)
    assert monitor.discover_recent_releases("2026-09-29") == ""
    assert monitor.LAST_DISCOVERY_MODELS == []
    reason = monitor._consume_source_error("Discovery")
    assert "boom" in reason
    assert "parallel-key-secret" not in reason
    assert monitor._DISCOVERY_DISABLED is False


def test_disabled_discovery_is_not_tracked_as_empty(monkeypatch):
    monkeypatch.setattr(monitor, "DISCOVERY_ENABLED", False)
    monkeypatch.setattr(monitor, "PARALLEL_API_KEY", "")
    assert monitor.discover_recent_releases("2026-09-29") == ""
    assert monitor._DISCOVERY_DISABLED is True


def test_missing_state_file_starts_fresh(tmp_path):
    path = tmp_path / "missing" / "source_health.json"
    assert monitor.load_source_health(path) == {}
    monitor.flush_source_health(
        [{"source": "Ollama", "items": 3, "error": ""}],
        now=T0,
        path=path,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["Ollama"]["consecutive_empties"] == 0
    assert saved["Ollama"]["consecutive_failures"] == 0
    assert saved["Ollama"]["last_ok"]


def test_corrupt_state_file_is_tolerated(tmp_path, capsys):
    path = tmp_path / "source_health.json"
    path.write_text("{not-json", encoding="utf-8")
    monitor.flush_source_health(
        [{"source": "Ollama", "items": 1, "error": ""}],
        now=T0,
        path=path,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["Ollama"]["last_ok"]
    assert "source_health" in capsys.readouterr().err


def test_empty_and_error_alert_only_after_24h(tmp_path, monkeypatch):
    path = tmp_path / "source_health.json"
    monkeypatch.setattr(monitor, "ADMIN_CHAT_ID", "-100admin")
    sent = []
    monkeypatch.setattr(monitor, "send_ops_alert", lambda text: sent.append(text) or True)

    monitor.flush_source_health(
        _rows_error("OpenRouter"), now=T0, path=path,
    )
    monitor.flush_source_health(
        [{"source": "Ollama", "items": 0, "error": ""}],
        now=T0,
        path=path,
    )
    assert sent == []

    almost = T0 + timedelta(hours=23, minutes=59)
    monitor.flush_source_health(_rows_error("OpenRouter"), now=almost, path=path)
    monitor.flush_source_health(
        [{"source": "Ollama", "items": 0, "error": ""}], now=almost, path=path,
    )
    assert sent == []

    due = T0 + timedelta(hours=24)
    monitor.flush_source_health(_rows_error("OpenRouter"), now=due, path=path)
    monitor.flush_source_health(
        [{"source": "Ollama", "items": 0, "error": ""}], now=due, path=path,
    )
    assert len(sent) == 2
    assert any("OpenRouter" in body for body in sent)
    assert any("Ollama" in body for body in sent)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["OpenRouter"]["consecutive_failures"] == 3
    assert saved["OpenRouter"]["consecutive_empties"] == 0
    assert saved["Ollama"]["consecutive_empties"] == 3
    assert saved["Ollama"]["consecutive_failures"] == 0
    assert saved["OpenRouter"]["last_ok"] is None


def test_alert_deduped_for_24h_then_fires_again(tmp_path, monkeypatch):
    path = tmp_path / "source_health.json"
    monkeypatch.setattr(monitor, "ADMIN_CHAT_ID", "-100admin")
    sent = []
    monkeypatch.setattr(monitor, "send_ops_alert", lambda text: sent.append(text) or True)

    monitor.flush_source_health(_rows_error(), now=T0, path=path)
    due = T0 + timedelta(hours=24)
    monitor.flush_source_health(_rows_error(), now=due, path=path)
    assert len(sent) == 1
    monitor.flush_source_health(
        _rows_error(), now=due + timedelta(hours=23, minutes=59), path=path,
    )
    assert len(sent) == 1
    monitor.flush_source_health(
        _rows_error(), now=due + timedelta(hours=24), path=path,
    )
    assert len(sent) == 2


def test_without_admin_chat_logs_warn_and_does_not_send(tmp_path, monkeypatch, capsys):
    path = tmp_path / "source_health.json"
    monkeypatch.setattr(monitor, "ADMIN_CHAT_ID", "")
    sent = []
    monkeypatch.setattr(monitor, "send_ops_alert", lambda text: sent.append(text) or True)
    old = (T0 - timedelta(hours=25)).isoformat()
    path.write_text(json.dumps({
        "OpenRouter": {
            "last_ok": None,
            "consecutive_failures": 2,
            "consecutive_empties": 0,
            "unhealthy_since": old,
            "last_alert_at": None,
        }
    }), encoding="utf-8")

    monitor.flush_source_health(_rows_error(), now=T0, path=path)
    err = capsys.readouterr().err
    assert "source_health ALERT WARN source=OpenRouter" in err
    assert sent == []

    monitor.flush_source_health(
        _rows_error(), now=T0 + timedelta(hours=1), path=path,
    )
    err = capsys.readouterr().err
    assert "source_health ALERT WARN" not in err


def test_ok_clears_failure_streak(tmp_path):
    path = tmp_path / "source_health.json"
    path.write_text(json.dumps({
        "OpenRouter": {
            "last_ok": None,
            "consecutive_failures": 4,
            "consecutive_empties": 0,
            "unhealthy_since": (T0 - timedelta(hours=30)).isoformat(),
            "last_alert_at": (T0 - timedelta(hours=6)).isoformat(),
        }
    }), encoding="utf-8")
    monitor.flush_source_health(
        [{"source": "OpenRouter", "items": 8, "error": ""}],
        now=T0,
        path=path,
    )
    rec = json.loads(path.read_text(encoding="utf-8"))["OpenRouter"]
    assert rec["consecutive_failures"] == 0
    assert rec["consecutive_empties"] == 0
    assert rec["unhealthy_since"] is None
    assert rec["last_ok"]


def test_preview_does_not_persist_or_alert(tmp_path, monkeypatch, capsys):
    path = tmp_path / "source_health.json"
    monkeypatch.setattr(monitor, "ADMIN_CHAT_ID", "-100admin")
    sent = []
    monkeypatch.setattr(monitor, "send_ops_alert", lambda text: sent.append(text) or True)
    path.write_text("{not-json", encoding="utf-8")
    monitor.flush_source_health(
        _rows_error(),
        now=T0 + timedelta(hours=48),
        path=path,
        preview=True,
    )
    assert path.read_text(encoding="utf-8") == "{not-json"
    assert sent == []
    assert "source_health source=OpenRouter status=error" in capsys.readouterr().err
    assert "source_health ALERT" not in capsys.readouterr().err


def test_writer_health_line_reports_candidates_included_and_drops(monkeypatch, capsys):
    monkeypatch.setattr(monitor, "LAST_LINK_DROPPED", 2)
    monkeypatch.setattr(monitor, "LAST_STALE_DROPPED", 1)
    line = monitor.emit_writer_health(15, 6)
    assert line == (
        "source_health writer candidates=15 included=6 "
        "link_dropped=2 stale_dropped=1"
    )
    assert line in capsys.readouterr().err
    monkeypatch.setattr(monitor, "LAST_LINK_DROPPED", 0)
    monkeypatch.setattr(monitor, "LAST_STALE_DROPPED", 0)


def test_main_logs_each_source_and_writer_summary(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["monitor.py", "--preview"])
    monkeypatch.setattr(monitor, "try_post_pending_curated", lambda: False)
    monkeypatch.setattr(monitor, "init_database", lambda: None)
    monkeypatch.setattr(monitor, "load_seen_models", lambda: {"seed/already"})
    monkeypatch.setattr(monitor, "save_seen_models", lambda _s: None)
    monkeypatch.setattr(monitor, "DISCOVERY_ENABLED", False)
    monkeypatch.setattr(monitor, "PARALLEL_API_KEY", "")
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    model = monitor.ModelRelease(
        name="meta-llama/Llama-4-70B",
        provider="Meta",
        source="openrouter",
        url="https://example.com/llama",
        description="x",
        is_open_source=True,
        release_date=fresh,
    )
    monkeypatch.setattr(monitor, "fetch_openrouter_models", lambda: [model])
    for name in (
        "fetch_ollama_models",
        "fetch_huggingface_trending",
        "fetch_major_orgs",
        "fetch_hf_text_generation",
    ):
        monkeypatch.setattr(monitor, name, lambda: [])
    monkeypatch.setattr(monitor, "enrich_with_hf_cards", lambda models: None)
    body = (
        "━━━ <b>OPEN FRONTIER</b> 🔓\n"
        "<b>Llama 4 70B</b> — <i>big</i> "
        '<a href="https://example.com/llama">→ Source</a>\n'
    )
    monkeypatch.setattr(monitor, "summarize_models", lambda models, *a, **k: body)
    alerts = []
    monkeypatch.setattr(monitor, "send_ops_alert", lambda text: alerts.append(text) or True)

    assert monitor.main() == 0
    err = capsys.readouterr().err
    for source, status, items in (
        ("OpenRouter", "ok", 1),
        ("Ollama", "empty", 0),
        ("HuggingFace-Trending", "empty", 0),
        ("HuggingFace-Orgs", "empty", 0),
        ("HuggingFace-Top-TextGen", "empty", 0),
        ("Discovery", "ok", 0),
    ):
        assert f"source_health source={source} status={status} items={items} " in err
    assert "source_health source=Discovery status=ok items=0 error=disabled" in err
    assert "source_health writer candidates=1 included=1 link_dropped=0 stale_dropped=0" in err
    assert not alerts
    assert not (tmp_path / "source_health.json").exists()

"""Post-QA release forwarding: identity, qualification, outbox, crash path.

No live network, no Telegram, no production database. Receiver calls are a
local stub against https://events.invalid/.
"""
import io
import json
import sys
import textwrap
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor
import release_events
import release_forwarding

ROOT = Path(__file__).resolve().parents[1]
TODAY = "2026-10-03"
FRESH = "2026-10-01"
STALE = "2026-09-03"  # 30 days before TODAY
EVENTS_URL = "https://events.invalid/v1"
TOKEN = "mb-test-token-do-not-log"


def _model(name, **kwargs):
    fields = dict(
        provider="Org",
        source="huggingface-org",
        url=f"https://huggingface.co/{name}",
        description="New model release",
        release_date=FRESH,
        confidence="medium",
    )
    fields.update(kwargs)
    return monitor.ModelRelease(name=name, **fields)


def _arm(monkeypatch, flag="1"):
    monkeypatch.setenv("MODELBYTES_RELEASE_FORWARDING", flag)
    monkeypatch.setenv("RELEASE_EVENTS_URL", EVENTS_URL)
    monkeypatch.setenv("RELEASE_EVENTS_TOKEN", TOKEN)


def _digest(url, display="DeepSeek V4"):
    return (
        "🤖 <b>ModelBytes Digest</b>\n"
        "<i>Saturday, October 03, 2026</i>\n\n"
        "━━━ <b>OPEN FRONTIER</b> 🔓\n"
        f"<b>{display}</b> — <i>reasoning model</i> "
        f'<a href="{url}">→ Source</a>\n\n'
        "📊 Surfaced 1 · scanned 1 today"
    )


class _Resp:
    def __init__(self, status, body=b"{}"):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class MemDB:
    """Just enough Postgres for the outbox helpers."""

    def __init__(self):
        self.rows = {}
        self._result = []
        self.rowcount = 0
        self.calls = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def commit(self):
        return None

    def close(self):
        return None

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        upper = text.upper()
        params = params or ()
        self.calls.append(upper)
        self._result = []
        if upper.startswith("CREATE"):
            self.rowcount = 0
            return
        if upper.startswith("INSERT"):
            event_id, payload = params[0], params[1]
            if isinstance(payload, str):
                payload = json.loads(payload)
            if event_id in self.rows:
                self.rowcount = 0
            else:
                self.rows[event_id] = {
                    "payload": payload,
                    "status": "pending",
                    "attempts": 0,
                }
                self.rowcount = 1
            return
        if upper.startswith("SELECT STATUS"):
            row = self.rows.get(params[0])
            self._result = [(row["status"], row["attempts"])] if row else []
            self.rowcount = len(self._result)
            return
        if "STATUS = 'PENDING'" in upper:
            self._result = [
                (eid, row["payload"], row["attempts"])
                for eid, row in self.rows.items()
                if row["status"] == "pending"
            ]
            self.rowcount = len(self._result)
            return
        if upper.startswith("UPDATE"):
            status, attempts, event_id = params
            self.rows[event_id]["status"] = status
            self.rows[event_id]["attempts"] = attempts
            self.rowcount = 1
            return
        raise AssertionError(f"unexpected SQL: {text}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


def _patch_db(monkeypatch, mem):
    monkeypatch.setattr(monitor, "DATABASE_URL", "postgres://outbox.invalid/modelbytes")
    monkeypatch.setattr(monitor.psycopg2, "connect", lambda *a, **k: mem)


def _install_urlopen(monkeypatch, status, body=b"{}"):
    seen = []

    def _open(req, timeout=5):
        seen.append(req)
        if status >= 400:
            raise urllib.error.HTTPError(
                req.full_url, status, "err", {}, io.BytesIO(body))
        return _Resp(status, body)

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    return seen


def _forbid_http(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("unexpected HTTP")

    monkeypatch.setattr(urllib.request, "urlopen", boom)


def _forbid_db(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("unexpected database connection")

    monkeypatch.setattr(monitor.psycopg2, "connect", boom)


def _sample_event():
    return {
        "id": "model:deepseek-ai/deepseek-v4",
        "kind": "model",
        "name": "DeepSeek-V4",
        "version": "4",
        "source": "modelbytes",
        "url": "https://huggingface.co/deepseek-ai/DeepSeek-V4",
    }


# ── identity ──────────────────────────────────────────────────────────────


def test_same_model_on_hf_and_openrouter_shares_an_id():
    hf = _model(
        "Qwen/Qwen3-32B",
        url="https://huggingface.co/Qwen/Qwen3-32B",
        release_date=FRESH,
    )
    listed = _model(
        "qwen/qwen3-32b",
        source="openrouter",
        url="https://openrouter.ai/models/qwen/qwen3-32b",
        release_date=STALE,
    )
    assert release_forwarding.event_id(hf) == "model:qwen/qwen3-32b"
    assert release_forwarding.event_id(hf) == release_forwarding.event_id(listed)
    version = release_forwarding.explicit_version(hf)
    assert version != FRESH
    assert version != STALE
    assert version != "new"
    assert "32b" in version
    assert "2026" not in version


def test_version_falls_back_to_normalized_id_not_a_date_or_new():
    model = _model("acme/Model-X", release_date=FRESH)
    assert release_forwarding.explicit_version(model) == "acme/model-x"
    payload = release_forwarding.build_payload(model)
    assert payload["version"] == "acme/model-x"
    assert payload["version"] != FRESH
    assert payload["version"] != "new"
    assert payload["published_at"] == FRESH
    assert payload["metadata"]["release_date"] == FRESH
    assert FRESH not in payload["id"]


def test_instruct_and_chat_stay_in_the_identity():
    # collapse_variants keeps capability tiers. Forwarding follows that.
    assert release_forwarding.event_id("meta-llama/Llama-3.1-8B-Instruct") == (
        "model:meta-llama/llama-3.1-8b-instruct"
    )
    assert release_forwarding.event_id("meta-llama/Llama-3.1-8B") == (
        "model:meta-llama/llama-3.1-8b"
    )
    assert release_forwarding.event_id("org/Model-X-chat") != (
        release_forwarding.event_id("org/Model-X")
    )


def test_catalog_prefix_and_quant_suffix_do_not_change_the_id():
    assert release_forwarding.event_id("huggingface/Qwen/Qwen3-32B") == (
        "model:qwen/qwen3-32b"
    )
    assert release_forwarding.event_id("Qwen/Qwen3-32B-GGUF") == (
        release_forwarding.event_id("Qwen/Qwen3-32B")
    )
    assert release_forwarding.event_id("~Qwen/Qwen3-32B:free") == (
        "model:qwen/qwen3-32b"
    )


def test_payload_puts_date_and_catalog_in_metadata_not_the_id():
    model = _model(
        "deepseek-ai/DeepSeek-V4",
        provider="DeepSeek",
        source="huggingface-org",
        url="https://huggingface.co/deepseek-ai/DeepSeek-V4",
        canonical_url="https://huggingface.co/deepseek-ai/DeepSeek-V4",
    )
    payload = release_forwarding.build_payload(model)
    assert payload["id"] == "model:deepseek-ai/deepseek-v4"
    assert payload["kind"] == "model"
    assert payload["version"] == "4"
    assert payload["source"] == "modelbytes"
    assert payload["url"] == "https://huggingface.co/deepseek-ai/DeepSeek-V4"
    assert payload["metadata"]["catalog"] == "huggingface-org"
    assert payload["metadata"]["model_id"] == "deepseek-ai/deepseek-v4"
    assert "pricing_input" not in payload
    assert "pricing" not in payload["metadata"]
    assert "openrouter" not in payload["id"]


# ── qualification fixtures ────────────────────────────────────────────────


def _failure_fixtures():
    good = _model(
        "deepseek-ai/DeepSeek-V4",
        provider="DeepSeek",
        url="https://huggingface.co/deepseek-ai/DeepSeek-V4",
        canonical_url="https://huggingface.co/deepseek-ai/DeepSeek-V4",
        description="Reasoning model",
    )
    undated = _model(
        "meta-llama/Llama-Undated",
        provider="Meta",
        release_date=None,
        url="https://huggingface.co/meta-llama/Llama-Undated",
    )
    old = _model(
        "meta-llama/Llama-Old",
        provider="Meta",
        release_date=STALE,
        url="https://huggingface.co/meta-llama/Llama-Old",
    )
    gguf = _model(
        "unsloth/DeepSeek-V4-GGUF",
        provider="Unsloth",
        url="https://huggingface.co/unsloth/DeepSeek-V4-GGUF",
    )
    # Catalog listing of the HF announcement, even if it also cites the card.
    catalog = _model(
        "deepseek-ai/DeepSeek-V4",
        provider="DeepSeek",
        source="openrouter",
        url="https://openrouter.ai/models/deepseek-ai/DeepSeek-V4",
        canonical_url="https://huggingface.co/deepseek-ai/DeepSeek-V4",
        description="Listed on OpenRouter",
    )
    price = _model(
        "openai/gpt-5",
        provider="OpenAI",
        source="openrouter",
        url="https://openrouter.ai/models/openai/gpt-5",
        canonical_url="https://openai.com/gpt-5",
        description="Price change only: input reduced to $1.25",
        unique_traits=["price_change"],
        pricing_input=1.25,
    )
    return [undated, old, gguf, catalog, price, good]


def test_failure_fixtures_only_the_fresh_primary_release_qualifies():
    selected = release_forwarding.select_qualified(_failure_fixtures(), today=TODAY)
    assert [m.name for m in selected] == ["deepseek-ai/DeepSeek-V4"]
    assert selected[0].source == "huggingface-org"


def test_fixture_rejection_reasons():
    undated, old, gguf, catalog, price, good = _failure_fixtures()
    assert release_forwarding.qualification_reason(undated, today=TODAY) == "undated"
    assert release_forwarding.qualification_reason(old, today=TODAY) == "stale"
    assert release_forwarding.qualification_reason(gguf, today=TODAY) == "derivative"
    assert release_forwarding.qualification_reason(price, today=TODAY) == (
        "price_or_availability"
    )
    keys = {release_forwarding.normalized_model_key(good.name)}
    assert release_forwarding.qualification_reason(
        catalog, today=TODAY, primary_keys=keys) == "catalog_duplicate"
    assert release_forwarding.qualification_reason(good, today=TODAY) is None


def test_openrouter_only_listing_and_serving_sku_do_not_qualify():
    listing = _model(
        "deepseek-ai/DeepSeek-V4",
        source="openrouter",
        url="https://openrouter.ai/models/deepseek-ai/DeepSeek-V4",
        canonical_url=None,
    )
    sku = _model(
        "deepseek-ai/DeepSeek-V4:free",
        source="openrouter",
        url="https://openrouter.ai/models/deepseek-ai/DeepSeek-V4:free",
    )
    assert release_forwarding.qualification_reason(listing, today=TODAY) == (
        "no_primary_url"
    )
    assert release_forwarding.qualification_reason(sku, today=TODAY) == "serving_sku"
    assert release_forwarding.select_qualified([listing, sku], today=TODAY) == []


def test_undated_exception_requires_official_discovery_and_primary_url():
    ok = _model(
        "deepseek-ai/DeepSeek-V4-Flash",
        source="discovery",
        release_date=None,
        confidence="high",
        url="https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash",
        canonical_url="https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash",
        unique_traits=["web_discovery"],
        description="Announced on the model card",
    )
    low = _model(
        "deepseek-ai/DeepSeek-V4-Flash",
        source="discovery",
        release_date=None,
        confidence="low",
        url="https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash",
        unique_traits=["web_discovery"],
    )
    catalog_url = _model(
        "deepseek-ai/DeepSeek-V4-Flash",
        source="discovery",
        release_date=None,
        confidence="high",
        url="https://openrouter.ai/models/deepseek-ai/DeepSeek-V4-Flash",
        unique_traits=["web_discovery"],
    )
    unknown = _model(
        "random-person/Cool-Model",
        source="discovery",
        release_date=None,
        confidence="high",
        url="https://random-person.example/cool-model",
        unique_traits=["web_discovery"],
    )
    assert release_forwarding.qualification_reason(ok, today=TODAY) is None
    assert release_forwarding.qualification_reason(low, today=TODAY) == "undated"
    assert release_forwarding.qualification_reason(catalog_url, today=TODAY) == (
        "undated"
    )
    assert release_forwarding.qualification_reason(unknown, today=TODAY) == "undated"


def test_price_change_with_a_primary_url_still_does_not_qualify():
    model = _model(
        "openai/gpt-5",
        url="https://huggingface.co/openai/gpt-5",
        description="Price reduced to $1.25 per million input tokens",
    )
    assert release_forwarding.qualification_reason(model, today=TODAY) == (
        "price_or_availability"
    )


def test_aggregator_url_is_not_primary():
    model = _model(
        "deepseek-ai/DeepSeek-V4",
        source="discovery",
        url="https://llm-stats.com/models/deepseek-v4",
        canonical_url="https://llm-stats.com/models/deepseek-v4",
    )
    assert release_forwarding.qualification_reason(model, today=TODAY) == (
        "no_primary_url"
    )


# ── receiver outcomes ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,body,outcome",
    [
        (202, b"{}", "delivered"),
        (200, b"{}", "delivered"),
        (200, b'{"status":"accepted"}', "delivered"),
        (200, b'{"status":"duplicate"}', "duplicate"),
        (200, b'{"result":"owned_by_poller"}', "duplicate"),
        (409, b"", "duplicate"),
        (400, b'{"error":"bad"}', "rejected"),
        (401, b"", "rejected"),
        (503, b"", "retryable"),
    ],
)
def test_emit_maps_receiver_status(monkeypatch, status, body, outcome):
    _arm(monkeypatch)
    seen = _install_urlopen(monkeypatch, status, body)
    result = release_events.emit_release_event(_sample_event())
    assert result.outcome == outcome
    assert result.status == status
    assert seen[0].full_url == EVENTS_URL
    assert seen[0].get_header("Authorization") == f"Bearer {TOKEN}"
    posted = json.loads(seen[0].data.decode())
    assert posted["schema"] == "release-event/v1"
    assert posted["id"] == _sample_event()["id"]
    assert "railway" not in seen[0].full_url


def test_emit_timeout_is_retryable_and_log_omits_the_token(monkeypatch, capsys):
    _arm(monkeypatch)

    def boom(req, timeout=5):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    result = release_events.emit_release_event(_sample_event())
    assert result.outcome == "retryable"
    assert result.status is None
    assert TOKEN not in capsys.readouterr().err


def test_emit_without_config_does_not_post(monkeypatch):
    monkeypatch.delenv("RELEASE_EVENTS_URL", raising=False)
    monkeypatch.delenv("RELEASE_EVENTS_TOKEN", raising=False)
    _forbid_http(monkeypatch)
    result = release_events.emit_release_event(_sample_event())
    assert result.outcome == "rejected"
    assert result.detail == "not_configured"


def test_flag_must_be_exactly_one(monkeypatch):
    _arm(monkeypatch, flag="true")
    assert release_forwarding.forwarding_enabled() is False
    _arm(monkeypatch, flag="1")
    monkeypatch.delenv("RELEASE_EVENTS_TOKEN", raising=False)
    assert release_forwarding.forwarding_enabled() is False
    _arm(monkeypatch, flag="1")
    assert release_forwarding.forwarding_enabled() is True


# ── seam: surfaced, QA, preview, outbox ───────────────────────────────────


def test_forward_posts_only_the_qualified_surfaced_model(monkeypatch):
    _arm(monkeypatch)
    mem = MemDB()
    _patch_db(monkeypatch, mem)
    seen = _install_urlopen(monkeypatch, 202, b"{}")
    urls = []
    for model in _failure_fixtures():
        urls.append(model.url)
        if model.canonical_url:
            urls.append(model.canonical_url)
    message = "\n".join(
        f'<b>{u.split("/")[-1]}</b> — <i>x</i> <a href="{u}">→ Source</a>'
        for u in urls
    )
    results = release_forwarding.forward_release_events(
        _failure_fixtures(), message, preview=False, today=TODAY)
    assert len(results) == 1
    assert results[0].outcome == "delivered"
    assert len(seen) == 1
    posted = json.loads(seen[0].data.decode())
    assert posted["id"] == "model:deepseek-ai/deepseek-v4"
    assert posted["version"] == "4"
    assert posted["version"] != FRESH
    assert posted["version"] != "new"
    row = mem.rows["model:deepseek-ai/deepseek-v4"]
    assert row["status"] == "delivered"
    assert row["attempts"] == 1


def test_model_missing_from_the_digest_is_not_forwarded(monkeypatch):
    _arm(monkeypatch)
    _forbid_http(monkeypatch)
    _forbid_db(monkeypatch)
    good = _failure_fixtures()[-1]
    message = _digest("https://example.invalid/someone-else", display="Someone Else")
    assert release_forwarding.forward_release_events(
        [good], message, preview=False, today=TODAY) == []


def test_qa_errors_and_preview_do_not_touch_http_or_the_outbox(monkeypatch):
    _arm(monkeypatch)
    _forbid_http(monkeypatch)
    _forbid_db(monkeypatch)
    good = _failure_fixtures()[-1]
    message = _digest(good.url)
    assert release_forwarding.forward_release_events(
        [good], message, preview=False, today=TODAY, qa_errors=["unbalanced <b>"]) == []
    assert release_forwarding.forward_release_events(
        [good], message, preview=True, today=TODAY) == []


def test_disabled_forward_does_not_write_or_post(monkeypatch):
    monkeypatch.delenv("MODELBYTES_RELEASE_FORWARDING", raising=False)
    monkeypatch.setenv("RELEASE_EVENTS_URL", EVENTS_URL)
    monkeypatch.setenv("RELEASE_EVENTS_TOKEN", TOKEN)
    _forbid_http(monkeypatch)
    _forbid_db(monkeypatch)
    good = _failure_fixtures()[-1]
    assert release_forwarding.forward_release_events(
        [good], _digest(good.url), preview=False, today=TODAY) == []
    assert release_forwarding.retry_pending_release_events() == 0


def test_retryable_then_delivered_then_not_retried(monkeypatch):
    _arm(monkeypatch)
    mem = MemDB()
    _patch_db(monkeypatch, mem)
    good = _failure_fixtures()[-1]
    message = _digest(good.url)
    seen = _install_urlopen(monkeypatch, 503, b"")
    first = release_forwarding.forward_release_events(
        [good], message, preview=False, today=TODAY)
    assert first[0].outcome == "retryable"
    assert mem.rows["model:deepseek-ai/deepseek-v4"]["status"] == "pending"
    assert mem.rows["model:deepseek-ai/deepseek-v4"]["attempts"] == 1

    seen2 = _install_urlopen(monkeypatch, 200, b"{}")
    assert release_forwarding.retry_pending_release_events() == 1
    assert len(seen2) == 1
    assert mem.rows["model:deepseek-ai/deepseek-v4"]["status"] == "delivered"
    assert mem.rows["model:deepseek-ai/deepseek-v4"]["attempts"] == 2

    def boom(*args, **kwargs):
        raise AssertionError("terminal rows must not be retried")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert release_forwarding.retry_pending_release_events() == 0


def test_rejected_row_is_kept_and_not_retried(monkeypatch):
    _arm(monkeypatch)
    mem = MemDB()
    _patch_db(monkeypatch, mem)
    good = _failure_fixtures()[-1]
    _install_urlopen(monkeypatch, 400, b'{"error":"bad"}')
    results = release_forwarding.forward_release_events(
        [good], _digest(good.url), preview=False, today=TODAY)
    assert results[0].outcome == "rejected"
    assert mem.rows["model:deepseek-ai/deepseek-v4"]["status"] == "rejected"

    def boom(*args, **kwargs):
        raise AssertionError("rejected rows must not be retried")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert release_forwarding.retry_pending_release_events() == 0


def test_timeout_leaves_the_row_pending(monkeypatch):
    _arm(monkeypatch)
    mem = MemDB()
    _patch_db(monkeypatch, mem)
    good = _failure_fixtures()[-1]

    def boom(req, timeout=5):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    results = release_forwarding.forward_release_events(
        [good], _digest(good.url), preview=False, today=TODAY)
    assert results[0].outcome == "retryable"
    assert mem.rows["model:deepseek-ai/deepseek-v4"]["status"] == "pending"
    assert TOKEN not in json.dumps(mem.rows["model:deepseek-ai/deepseek-v4"]["payload"])


def test_outbox_schema_matches_the_contract():
    sql = " ".join(release_forwarding.OUTBOX_DDL.split()).upper()
    assert "CREATE TABLE IF NOT EXISTS RELEASE_EVENT_OUTBOX" in sql
    assert "ID TEXT PRIMARY KEY" in sql
    assert "PAYLOAD JSONB NOT NULL" in sql
    assert "STATUS TEXT NOT NULL" in sql
    assert "ATTEMPTS INT NOT NULL DEFAULT 0" in sql
    assert "UPDATED_AT TIMESTAMPTZ NOT NULL DEFAULT NOW()" in sql


def test_summarize_models_does_not_emit(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(monitor, "LLM_API_KEY", "")
    _forbid_http(monkeypatch)
    _forbid_db(monkeypatch)
    monitor.summarize_models([_failure_fixtures()[-1]])


# ── main() wiring ─────────────────────────────────────────────────────────


def _quiet_main(monkeypatch, tmp_path, models=None, summarize=None, preview=False):
    monkeypatch.chdir(tmp_path)
    argv = ["monitor.py", "--preview"] if preview else ["monitor.py"]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(monitor, "INLINE_PRIMARY", True)
    monkeypatch.setattr(monitor, "TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr(monitor, "TELEGRAM_CHANNEL_ID", "-100123")
    monkeypatch.setattr(monitor, "try_post_pending_curated", lambda: False)
    monkeypatch.setattr(monitor, "init_database", lambda: None)
    monkeypatch.setattr(monitor, "load_seen_models", lambda: {"seed/already"})
    monkeypatch.setattr(monitor, "save_seen_models", lambda s: None)
    monkeypatch.setattr(monitor, "discover_recent_releases", lambda *a, **k: "")
    monkeypatch.setattr(monitor, "_recent_digest_names", lambda *a, **k: [])
    monkeypatch.setattr(monitor, "enrich_with_hf_cards", lambda models: None)
    monkeypatch.setattr(monitor, "record_publish_run", lambda *a, **k: True)
    monkeypatch.setattr(monitor, "ping_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "mark_posted_digest", lambda *a, **k: True)
    monkeypatch.setattr(monitor, "send_slack_post", lambda m: True)
    monkeypatch.setattr(monitor, "send_ops_alert", lambda t: True)
    batch = list(models or [])
    monkeypatch.setattr(monitor, "fetch_openrouter_models", lambda: [])
    monkeypatch.setattr(monitor, "fetch_ollama_models", lambda: [])
    monkeypatch.setattr(monitor, "fetch_huggingface_trending", lambda: [])
    monkeypatch.setattr(monitor, "fetch_major_orgs", lambda: batch)
    monkeypatch.setattr(monitor, "fetch_hf_text_generation", lambda: [])
    if summarize is not None:
        monkeypatch.setattr(monitor, "summarize_models", summarize)


def test_main_forwards_only_after_a_successful_publish(monkeypatch, tmp_path):
    _arm(monkeypatch)
    mem = MemDB()
    _patch_db(monkeypatch, mem)
    good = _model(
        "deepseek-ai/DeepSeek-V4",
        provider="DeepSeek",
        url="https://huggingface.co/deepseek-ai/DeepSeek-V4",
        release_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )
    message = _digest(good.url)
    order = []
    seen = _install_urlopen(monkeypatch, 202, b"{}")

    def send(body):
        order.append("send")
        return True

    def summarize(models, *args, **kwargs):
        order.append("summarize")
        return message

    _quiet_main(monkeypatch, tmp_path, models=[good], summarize=summarize)
    monkeypatch.setattr(monitor, "send_telegram_post", send)
    assert monitor.main() == 0
    assert order == ["summarize", "send"]
    assert len(seen) == 1
    posted = json.loads(seen[0].data.decode())
    assert posted["id"] == "model:deepseek-ai/deepseek-v4"
    assert mem.rows["model:deepseek-ai/deepseek-v4"]["status"] == "delivered"


def test_main_preview_makes_no_http_and_no_outbox_write(monkeypatch, tmp_path, capsys):
    _arm(monkeypatch)
    _forbid_http(monkeypatch)
    _forbid_db(monkeypatch)
    good = _model(
        "deepseek-ai/DeepSeek-V4",
        url="https://huggingface.co/deepseek-ai/DeepSeek-V4",
        release_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )

    def send(body):
        raise AssertionError("preview must not send")

    _quiet_main(
        monkeypatch, tmp_path, models=[good],
        summarize=lambda models, *a, **k: _digest(good.url),
        preview=True,
    )
    monkeypatch.setattr(monitor, "send_telegram_post", send)
    assert monitor.main() == 0
    err = capsys.readouterr().err
    assert "release forwarding: sending" not in err
    assert "release forwarding: delivered" not in err


def test_main_qa_block_does_not_forward(monkeypatch, tmp_path):
    _arm(monkeypatch)
    mem = MemDB()
    _patch_db(monkeypatch, mem)
    _forbid_http(monkeypatch)
    calls = []
    monkeypatch.setattr(
        release_forwarding, "forward_release_events",
        lambda *a, **k: calls.append(1) or [],
    )
    good = _model(
        "deepseek-ai/DeepSeek-V4",
        release_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )
    _quiet_main(
        monkeypatch, tmp_path, models=[good],
        summarize=lambda models, *a, **k: _digest(good.url),
    )
    monkeypatch.setattr(
        monitor, "validate_digest_for_publish",
        lambda message, mode="curated": (message, [], ["unbalanced <b> tags"]),
    )

    def send(body):
        raise AssertionError("QA block must not send")

    monkeypatch.setattr(monitor, "send_telegram_post", send)
    assert monitor.main() == 0
    assert calls == []


def test_main_telegram_failure_does_not_forward(monkeypatch, tmp_path):
    _arm(monkeypatch)
    mem = MemDB()
    _patch_db(monkeypatch, mem)
    _forbid_http(monkeypatch)
    calls = []
    monkeypatch.setattr(
        release_forwarding, "forward_release_events",
        lambda *a, **k: calls.append(1) or [],
    )
    good = _model(
        "deepseek-ai/DeepSeek-V4",
        release_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )
    _quiet_main(
        monkeypatch, tmp_path, models=[good],
        summarize=lambda models, *a, **k: _digest(good.url),
    )
    monkeypatch.setattr(monitor, "send_telegram_post", lambda message: False)
    assert monitor.main() == 1
    assert calls == []


def test_main_disabled_logs_and_does_not_connect(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("MODELBYTES_RELEASE_FORWARDING", raising=False)
    monkeypatch.setenv("RELEASE_EVENTS_URL", EVENTS_URL)
    monkeypatch.setenv("RELEASE_EVENTS_TOKEN", TOKEN)
    # A live run still needs a database URL for the digest path. Forwarding
    # itself must not open a connection while the flag is off.
    monkeypatch.setattr(monitor, "DATABASE_URL", "postgres://outbox.invalid/modelbytes")
    _forbid_http(monkeypatch)
    _forbid_db(monkeypatch)
    _quiet_main(monkeypatch, tmp_path)
    assert monitor.main() == 0
    assert "release forwarding: disabled" in capsys.readouterr().err


def test_enabled_quiet_day_does_not_replay_seen_models(monkeypatch, tmp_path):
    """Arming the flag must not walk history. Empty outbox → no POST."""
    _arm(monkeypatch)
    mem = MemDB()
    _patch_db(monkeypatch, mem)
    _forbid_http(monkeypatch)
    _quiet_main(monkeypatch, tmp_path)
    monkeypatch.setattr(
        monitor, "load_seen_models",
        lambda: {"deepseek-ai/DeepSeek-V3", "qwen/Qwen3-32B"},
    )
    assert monitor.main() == 0
    assert mem.rows == {}


def _pending_row():
    return {
        "id": "model:qwen/qwen3-32b",
        "kind": "model",
        "name": "Qwen3-32B",
        "version": "3-32b",
        "source": "modelbytes",
        "source_type": "huggingface-org",
        "url": "https://huggingface.co/Qwen/Qwen3-32B",
        "published_at": FRESH,
        "summary": "queued last run",
        "metadata": {"model_id": "qwen/qwen3-32b", "release_date": FRESH},
    }


@pytest.mark.parametrize("path", ["quiet", "seed", "no_models"])
def test_outbox_retries_on_early_return_paths(monkeypatch, tmp_path, path):
    _arm(monkeypatch)
    mem = MemDB()
    mem.rows["model:qwen/qwen3-32b"] = {
        "payload": _pending_row(),
        "status": "pending",
        "attempts": 1,
    }
    _patch_db(monkeypatch, mem)
    seen = _install_urlopen(monkeypatch, 200, b"{}")
    sent = []
    monkeypatch.setattr(monitor, "send_telegram_post", lambda message: sent.append(message) or True)
    if path == "seed":
        _quiet_main(monkeypatch, tmp_path, models=[_model("deepseek-ai/DeepSeek-V4")])
        monkeypatch.setattr(monitor, "load_seen_models", lambda: set())
        monkeypatch.setattr(monitor, "ALLOW_SEED", True)
    elif path == "no_models":
        fresh = _model(
            "deepseek-ai/DeepSeek-V4",
            release_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )
        _quiet_main(
            monkeypatch, tmp_path, models=[fresh],
            summarize=lambda models, *a, **k: monitor.NO_MODELS_SENTINEL,
        )
    else:
        _quiet_main(monkeypatch, tmp_path)
    assert monitor.main() == 0
    assert sent == []
    assert len(seen) == 1
    posted = json.loads(seen[0].data.decode())
    assert posted["id"] == "model:qwen/qwen3-32b"
    assert mem.rows["model:qwen/qwen3-32b"]["status"] == "delivered"
    assert mem.rows["model:qwen/qwen3-32b"]["attempts"] == 2


def test_curated_branch_invokes_the_seam_without_inventing_models(
        monkeypatch, tmp_path):
    _arm(monkeypatch)
    _forbid_http(monkeypatch)
    monkeypatch.chdir(tmp_path)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pending = tmp_path / "pending"
    pending.mkdir()
    (pending / f"{today}.txt").write_text(
        "🤖 <b>ModelBytes Digest</b>\n<i>Saturday, October 03, 2026</i>\n\n"
        "3 models tracked today\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(monitor, "has_posted_digest", lambda date_str: False)
    monkeypatch.setattr(monitor, "mark_posted_digest", lambda *a, **k: True)
    monkeypatch.setattr(monitor, "send_slack_post", lambda message: True)
    monkeypatch.setattr(monitor, "record_publish_run", lambda *a, **k: True)
    monkeypatch.setattr(monitor, "ping_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "send_telegram_post", lambda message: True)
    calls = []

    def spy(models, message="", **kwargs):
        calls.append(list(models))
        return []

    monkeypatch.setattr(release_forwarding, "forward_release_events", spy)
    assert monitor.try_post_pending_curated() is True
    assert calls == [[]]


# ── start command and crash handler ───────────────────────────────────────


def test_railway_start_command_runs_monitor_main():
    text = (ROOT / "railway.toml").read_text()
    assert 'startCommand = "python monitor.py"' in text
    assert "modelbytes_runner" not in text
    assert 'cronSchedule = "0 16 * * *"' in text
    assert 'restartPolicyType = "ON_FAILURE"' in text
    assert "restartPolicyMaxRetries = 2" in text
    docker = (ROOT / "Dockerfile").read_text()
    assert 'CMD ["python", "monitor.py"]' in docker


def test_runner_is_a_main_shim_and_import_does_not_forward():
    source = (ROOT / "modelbytes_runner.py").read_text()
    assert "runpy.run_module" in source
    assert 'run_name="__main__"' in source
    assert "summarize_models" not in source
    assert "emit_release_event" not in source
    assert "monitor.main" not in source
    assert "SystemExit" not in source
    import modelbytes_runner
    assert monitor.summarize_models.__name__ == "summarize_models"
    assert not hasattr(modelbytes_runner, "summarize_and_forward")


def test_shim_delegates_to_monitor_dunder_main(monkeypatch):
    import runpy

    called = {}

    def fake_run_module(name, run_name=None, **kwargs):
        called["name"] = name
        called["run_name"] = run_name

    monkeypatch.setattr(runpy, "run_module", fake_run_module)
    namespace = {"__name__": "__main__"}
    exec(compile((ROOT / "modelbytes_runner.py").read_text(),
                 "modelbytes_runner.py", "exec"), namespace)
    assert called == {"name": "monitor", "run_name": "__main__"}


def test_monitor_main_guard_calls_handle_crash_on_exception(monkeypatch):
    handled = []

    def fake_main():
        raise RuntimeError("simulated crash")

    def fake_handle(exc, preview, argv=None):
        handled.append((type(exc).__name__, preview))

    original_main = monitor.main
    original_handle = monitor._handle_crash
    monitor.main = fake_main
    monitor._handle_crash = fake_handle
    monkeypatch.setattr(sys, "argv", ["monitor.py"])
    block = textwrap.dedent(
        Path(monitor.__file__).read_text().split('if __name__ == "__main__":', 1)[1]
    )
    assert "_handle_crash" in block
    try:
        exec(block, monitor.__dict__)
    except RuntimeError as exc:
        assert "simulated crash" in str(exc)
    finally:
        monitor.main = original_main
        monitor._handle_crash = original_handle
    assert handled == [("RuntimeError", False)]


def test_monitor_main_guard_preview_flag_reaches_handle_crash(monkeypatch):
    handled = []
    original_main = monitor.main
    original_handle = monitor._handle_crash
    monitor.main = lambda: (_ for _ in ()).throw(RuntimeError("preview crash"))
    monitor._handle_crash = lambda exc, preview, argv=None: handled.append(preview)
    monkeypatch.setattr(sys, "argv", ["monitor.py", "--preview"])
    block = textwrap.dedent(
        Path(monitor.__file__).read_text().split('if __name__ == "__main__":', 1)[1]
    )
    try:
        exec(block, monitor.__dict__)
    except RuntimeError:
        pass
    finally:
        monitor.main = original_main
        monitor._handle_crash = original_handle
    assert handled == [True]

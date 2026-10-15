"""AI Wire delivery survives an outage via a durable outbox.

2026-10-14 review, finding 5: a failed push was one retry and a log line,
so registry records for a sent digest were lost. Items of the publication
manifest are queued in ai_wire_outbox; ordinary runs retry pending rows,
acks and permanent errors are recorded, and the oldest pending row is
logged. Telegram is never re-sent.
"""
import importlib
import io
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_wire
import monitor

WIRE_URL = "https://wire.invalid"
TOKEN = "wire-test-token"


class OutboxStore:
    """The rows that outlive a process: what Postgres would keep."""

    def __init__(self):
        self.rows = {}
        self.clock = datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc)


class OutboxConn:
    """A fresh connection per call, over a shared OutboxStore.

    Only ai_wire_outbox SQL is modelled. Any other statement from the
    publisher is accepted and returns nothing.
    """

    def __init__(self, store):
        self.store = store
        self._result = []
        self.rowcount = 0

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def commit(self):
        return None

    def close(self):
        return None

    def execute(self, sql, params=None):
        upper = " ".join(sql.split()).upper()
        self._result, self.rowcount = [], 0
        if "AI_WIRE_OUTBOX" not in upper or upper.startswith("CREATE"):
            return
        rows = self.store.rows
        if upper.startswith("INSERT"):
            row_id, item = params
            if row_id not in rows:
                self.store.clock += timedelta(seconds=1)
                rows[row_id] = {"item": json.loads(item), "status": "pending",
                                "attempts": 0, "last_error": None,
                                "created_at": self.store.clock}
                self.rowcount = 1
            return
        if upper.startswith("SELECT"):
            self._result = sorted(
                ((rid, r["item"], r["attempts"], r["created_at"])
                 for rid, r in rows.items() if r["status"] == "pending"),
                key=lambda t: (t[3], t[0]))
            return
        if upper.startswith("UPDATE"):
            status, error, ids = params
            for rid in ids:
                rows[rid].update(status=status, last_error=error,
                                 attempts=rows[rid]["attempts"] + 1)
                self.rowcount += 1
            return
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


class Endpoint:
    """urlopen stand-in: fails with `status` until `up` is set."""

    def __init__(self, status=503):
        self.status = status
        self.up = False
        self.calls = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode())
        self.calls.append(body)
        if not self.up:
            raise urllib.error.HTTPError(
                req.full_url, self.status, "err", {}, io.BytesIO(b""))
        return _Resp(json.dumps({"created": len(body["items"])}).encode())


class _Resp:
    def __init__(self, body):
        self.status = 200
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _arm(monkeypatch):
    monkeypatch.setenv("AI_WIRE_ENABLED", "1")
    monkeypatch.setenv("AI_WIRE_URL", WIRE_URL)
    monkeypatch.setenv("AI_WIRE_INGEST_TOKEN", TOKEN)


def _attach(monkeypatch, store):
    monkeypatch.setattr(monitor, "DATABASE_URL", "postgres://outbox.invalid/modelbytes")
    monkeypatch.setattr(monitor, "_db_connect", lambda **k: OutboxConn(store))


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _model(word):
    name = f"meta-llama/Llama-5-{word}-8B"
    return monitor.ModelRelease(
        name=name, provider="Meta", source="huggingface",
        url=f"https://huggingface.co/{name}", description="Test model",
        release_date=_today(), is_open_source=True)


def _harness(monkeypatch, tmp_path, models, sends):
    """Ordinary live run: real renderer, every non-DB edge stubbed."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pending").mkdir(exist_ok=True)
    monkeypatch.setattr(sys, "argv", ["monitor.py"])
    monkeypatch.setattr(monitor, "INLINE_PRIMARY", True)
    monkeypatch.setattr(monitor, "LLM_API_KEY", "")
    monkeypatch.setattr(monitor, "ENRICH_HF_CARDS", False)
    monkeypatch.setattr(monitor, "TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr(monitor, "TELEGRAM_CHANNEL_ID", "-100XXXXXXXXXX")
    monkeypatch.setattr(monitor, "run_alphasignal_crosscheck", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "init_database", lambda: None)
    monkeypatch.setattr(monitor, "init_posted_digest_store", lambda: None)
    monkeypatch.setattr(monitor, "load_seen_models", lambda: {"seed/already"})
    monkeypatch.setattr(monitor, "save_seen_models", lambda s: None)
    monkeypatch.setattr(monitor, "discover_recent_releases", lambda *a, **k: "")
    monkeypatch.setattr(monitor, "_recent_digest_names", lambda *a, **k: [])
    monkeypatch.setattr(monitor, "record_publish_run", lambda *a, **k: True)
    monkeypatch.setattr(monitor, "_record_no_publishable", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "log_dropped_candidate", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "ping_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "mark_posted_digest", lambda *a, **k: True)
    monkeypatch.setattr(monitor, "send_slack_post", lambda m: True)
    monkeypatch.setattr(monitor, "send_ops_alert", lambda t: True)
    for name in ("fetch_openrouter_models", "fetch_ollama_models",
                 "fetch_huggingface_trending", "fetch_hf_text_generation"):
        monkeypatch.setattr(monitor, name, lambda: [])
    monkeypatch.setattr(monitor, "fetch_major_orgs", lambda: list(models))
    monkeypatch.setattr(monitor, "fetch_artificial_analysis_models", lambda *a, **k: [])
    monkeypatch.setattr(monitor, "fetch_testingcatalog_models", lambda *a, **k: [])
    monkeypatch.setattr(monitor, "fetch_artificial_analysis_x_models", lambda *a, **k: [])

    def send(message):
        sends.append(message)
        monitor.LAST_TELEGRAM_MESSAGE_ID = 55
        return True

    monkeypatch.setattr(monitor, "send_telegram_post", send)


@pytest.mark.parametrize("next_run", ["same-day rerun", "next-day quiet run"])
def test_outage_then_restart_then_recovery_fills_the_registry(
        monkeypatch, tmp_path, capsys, next_run):
    _arm(monkeypatch)
    store = OutboxStore()
    endpoint = Endpoint(status=503)
    monkeypatch.setattr(urllib.request, "urlopen", endpoint)
    sends = []
    models = [_model("Alpha"), _model("Bravo")]

    # Run 1: publish succeeds, AI Wire fails on every attempt.
    _attach(monkeypatch, store)
    _harness(monkeypatch, tmp_path, models, sends)
    monkeypatch.setattr(monitor, "has_posted_digest", lambda day: False)
    assert monitor.main() == 0
    assert len(sends) == 1
    assert endpoint.calls, "run 1 must have tried AI Wire"
    pending = {rid: r for rid, r in store.rows.items() if r["status"] == "pending"}
    assert {r["item"]["canonical_key"] for r in pending.values()} == {
        "model:meta-llama/llama-5-alpha-8b", "model:meta-llama/llama-5-bravo-8b"}
    assert all(r["attempts"] >= 1 and r["last_error"] == "http 503"
               for r in pending.values())
    assert "ai_wire_outbox pending=2 oldest=" in capsys.readouterr().err

    # Process restart: module state is rebuilt, only the store survives.
    importlib.reload(ai_wire)
    monitor.LAST_TELEGRAM_MESSAGE_ID = None
    _attach(monkeypatch, store)
    endpoint.up = True
    endpoint.calls.clear()

    # Run 2: an ordinary run fills the registry without reposting.
    if next_run == "same-day rerun":
        _harness(monkeypatch, tmp_path, models, sends)
        monkeypatch.setattr(monitor, "has_posted_digest", lambda day: True)
    else:
        # A new day: yesterday's write-back cache is not today's file.
        for cached in (tmp_path / "pending").glob("*.txt"):
            cached.unlink()
        _harness(monkeypatch, tmp_path, [], sends)
        monkeypatch.setattr(monitor, "has_posted_digest", lambda day: False)
    assert monitor.main() == 0
    assert len(sends) == 1, "Telegram must not be re-sent"
    delivered = [item for call in endpoint.calls for item in call["items"]]
    assert {i["canonical_key"] for i in delivered} == {
        "model:meta-llama/llama-5-alpha-8b", "model:meta-llama/llama-5-bravo-8b"}
    assert all(i["channel_post_url"] == "https://t.me/ModelBytes/55" for i in delivered)
    assert {r["status"] for r in store.rows.values()} == {"delivered"}
    assert "ai_wire_outbox pending=0" in capsys.readouterr().err

    # Acked rows are not sent again.
    endpoint.calls.clear()
    assert ai_wire.retry_pending() == 0
    assert endpoint.calls == []


def _queue_one(monkeypatch, store):
    _arm(monkeypatch)
    _attach(monkeypatch, store)
    message = ("🤖 <b>ModelBytes Digest</b>\n<i>Wednesday, October 14, 2026</i>\n\n"
               "━━━ <b>LOCAL</b> 🔓\n"
               '<b>One</b> — <i>x</i> <a href="https://example.com/one">→ HF</a>\n')
    ai_wire.push_published_digest([], message, message_id=7, today="2026-10-14")


def test_permanent_rejection_is_recorded_and_not_retried(monkeypatch):
    store = OutboxStore()
    endpoint = Endpoint(status=422)
    monkeypatch.setattr(urllib.request, "urlopen", endpoint)
    _queue_one(monkeypatch, store)
    assert len(endpoint.calls) == 1
    (row,) = store.rows.values()
    assert row["status"] == "rejected" and row["last_error"] == "http 422"
    assert ai_wire.retry_pending() == 0
    assert len(endpoint.calls) == 1


def test_auth_error_stays_pending_for_the_next_run(monkeypatch):
    store = OutboxStore()
    endpoint = Endpoint(status=401)
    monkeypatch.setattr(urllib.request, "urlopen", endpoint)
    _queue_one(monkeypatch, store)
    (row,) = store.rows.values()
    assert row["status"] == "pending" and row["last_error"] == "http 401"
    endpoint.up = True
    assert ai_wire.retry_pending() == 1
    assert row["status"] == "delivered" and row["attempts"] == 2


def test_requeueing_a_delivered_item_does_not_resend(monkeypatch):
    store = OutboxStore()
    endpoint = Endpoint()
    endpoint.up = True
    monkeypatch.setattr(urllib.request, "urlopen", endpoint)
    _queue_one(monkeypatch, store)
    _queue_one(monkeypatch, store)
    assert len(endpoint.calls) == 1
    assert len(store.rows) == 1


def test_health_line_names_the_oldest_pending_row():
    created = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)
    now = created + timedelta(hours=48)
    rows = [("2026-10-12|model:a/b", {}, 3, created),
            ("2026-10-13|model:c/d", {}, 1, created + timedelta(days=1))]
    line = ai_wire.outbox_health_line(rows, now=now)
    assert line == ("ai_wire_outbox pending=2 oldest=2026-10-12T16:00:00+00:00 "
                    "age_h=48.0 key=2026-10-12|model:a/b")
    assert ai_wire.outbox_health_line([]) == "ai_wire_outbox pending=0"


def test_preview_and_flag_off_do_not_touch_the_outbox(monkeypatch, tmp_path):
    store = OutboxStore()
    store.rows["x|model:a/b"] = {"item": {"canonical_key": "model:a/b"},
                                 "status": "pending", "attempts": 1,
                                 "last_error": "http 503",
                                 "created_at": store.clock}
    endpoint = Endpoint()
    endpoint.up = True
    monkeypatch.setattr(urllib.request, "urlopen", endpoint)
    _attach(monkeypatch, store)
    assert ai_wire.retry_pending() == 0  # flag off
    _arm(monkeypatch)
    _harness(monkeypatch, tmp_path, [], [])
    monkeypatch.setattr(sys, "argv", ["monitor.py", "--preview"])
    assert monitor.main() == 0
    assert endpoint.calls == []
    assert store.rows["x|model:a/b"]["status"] == "pending"

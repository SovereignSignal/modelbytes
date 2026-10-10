"""AI Wire ingest: mapping, flag-off no-op, and a push that never fails the digest.

No live network. The receiver is a local stub against https://wire.invalid/.
"""
import io
import json
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_wire
import monitor

ROOT = Path(__file__).resolve().parents[1]
WIRE_URL = "https://wire.invalid"
TOKEN = "super-secret-wire-token"
TODAY = "2026-10-03"


def _model(name, **kwargs):
    fields = dict(
        provider="Org",
        source="huggingface-org",
        url=f"https://huggingface.co/{name}",
        description="New model release",
        release_date="2026-10-01",
        confidence="medium",
    )
    fields.update(kwargs)
    return monitor.ModelRelease(name=name, **fields)


def _arm(monkeypatch, flag="1", url=WIRE_URL, token=TOKEN):
    if flag is None:
        monkeypatch.delenv("AI_WIRE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("AI_WIRE_ENABLED", flag)
    if url is None:
        monkeypatch.delenv("AI_WIRE_URL", raising=False)
    else:
        monkeypatch.setenv("AI_WIRE_URL", url)
    if token is None:
        monkeypatch.delenv("AI_WIRE_INGEST_TOKEN", raising=False)
    else:
        monkeypatch.setenv("AI_WIRE_INGEST_TOKEN", token)


def _digest(*sections):
    """sections are (lane, entry_lines)."""
    lines = [
        "🤖 <b>ModelBytes Digest</b>",
        "<i>Saturday, October 03, 2026</i>",
        "",
    ]
    for lane, entries in sections:
        lines.extend(["", f"━━━ <b>{lane}</b> 🔓", ""])
        for entry in entries:
            lines.append(entry)
            lines.append("")
    lines.append("📊 Surfaced 1 · scanned 2 today")
    return "\n".join(lines)


def _entry(title, url, prose="reasoning model", extra=""):
    href = f'<a href="{url}">→ HF</a>' if url else ""
    tail = f" {extra}" if extra else ""
    return f"<b>{title}</b> — <i>{prose}</i>{tail} {href}".strip()


class _Resp:
    def __init__(self, status, body=b'{"upserted": 1, "created": 1, "keys": []}'):
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


def _install_urlopen(monkeypatch, status=200, body=None, fail_times=0, error=None):
    seen = []

    def _open(req, timeout=None):
        seen.append((req, timeout))
        if len(seen) <= fail_times:
            if error is not None:
                raise error
            raise urllib.error.HTTPError(
                req.full_url, status, "err", {}, io.BytesIO(body or b""))
        if status >= 400 and error is None and fail_times == 0:
            raise urllib.error.HTTPError(
                req.full_url, status, "err", {}, io.BytesIO(body or b""))
        payload = body if body is not None else b'{"upserted": 1}'
        return _Resp(200 if status >= 400 and fail_times else status, payload)

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    return seen


def _forbid_http(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("unexpected HTTP")

    monkeypatch.setattr(urllib.request, "urlopen", boom)


# ── identity ──────────────────────────────────────────────────────────────


def test_hf_openrouter_and_lab_blog_collapse_when_the_id_is_known():
    hf = _model(
        "huggingface/Qwen/Qwen3-32B-GGUF",
        url="https://huggingface.co/Qwen/Qwen3-32B-GGUF",
    )
    listed = _model(
        "qwen/qwen3-32b:free",
        source="openrouter",
        url="https://openrouter.ai/models/qwen/qwen3-32b:free",
    )
    blog = _model(
        "Qwen/Qwen3-32B",
        source="discovery",
        url="https://qwen.ai/blog/qwen3-32b",
    )
    assert ai_wire.canonical_key(hf) == "model:qwen/qwen3-32b"
    assert ai_wire.canonical_key(listed) == ai_wire.canonical_key(hf)
    assert ai_wire.canonical_key(blog) == ai_wire.canonical_key(hf)
    assert ai_wire.canonical_key(hf) == ai_wire.canonical_key(
        None,
        url="https://openrouter.ai/models/Qwen/Qwen3-32B:free",
        title="Qwen3 32B",
    )


def test_blog_title_without_an_id_does_not_invent_an_org():
    key = ai_wire.canonical_key(
        None, url="https://qwen.ai/blog/qwen3-32b", title="Qwen3-32B",
    )
    assert key == "model:qwen3-32b"
    assert "/" not in key.split("model:", 1)[1]


def test_items_use_the_section_lane_and_only_entries_in_the_digest():
    shown = _model(
        "deepseek-ai/DeepSeek-V4",
        url="https://huggingface.co/deepseek-ai/DeepSeek-V4",
        modality="text-to-image",
        release_date="2026-10-01",
        description="<b>ignore me</b>",
    )
    omitted = _model(
        "other/Not-In-Digest",
        url="https://huggingface.co/other/Not-In-Digest",
        modality="text",
    )
    blog_only_url = "https://reflection.ai/blog/beam"
    message = _digest(
        ("OPEN FRONTIER", [
            _entry(
                "DeepSeek V4",
                shown.url,
                prose="A vision release with a <b>long</b> claim.",
            ),
        ]),
        ("SPECIALIZED", [
            _entry("Reflection Beam", blog_only_url, prose="A lab note."),
        ]),
    )
    items = ai_wire.items_from_digest(
        [shown, omitted], message,
        message_id=979,
        posted_at=datetime(2026, 10, 3, 16, 0, tzinfo=timezone.utc),
    )
    keys = [item["canonical_key"] for item in items]
    assert "model:other/not-in-digest" not in keys
    assert keys == ["model:deepseek-ai/deepseek-v4", "model:reflection-beam"]

    deepseek, beam = items
    assert deepseek["source_bot"] == "modelbytes"
    assert deepseek["channel"] == "ModelBytes"
    assert deepseek["kind"] == "model"
    assert deepseek["title"] == "DeepSeek V4"
    assert deepseek["lane"] == "OPEN FRONTIER"
    assert deepseek["modality"] == "image"
    assert deepseek["org"] == "deepseek-ai"
    assert deepseek["url"] == shown.url
    assert deepseek["summary"] == "A vision release with a long claim."
    assert "<" not in deepseek["summary"]
    assert deepseek["published_at"] == "2026-10-01"
    assert deepseek["posted_at"] == "2026-10-03T16:00:00+00:00"
    assert deepseek["channel_post_url"] == "https://t.me/ModelBytes/979"
    assert beam["lane"] == "SPECIALIZED"
    assert beam["url"] == blog_only_url
    assert "modality" not in beam
    assert "channel_post_url" in beam


def test_same_model_in_two_sections_is_one_item_preferring_the_primary_url():
    message = _digest(
        ("LOCAL", [
            _entry(
                "Qwen3 32B",
                "https://openrouter.ai/models/qwen/qwen3-32b:free",
                prose="catalog row",
            ),
        ]),
        ("OPEN FRONTIER", [
            _entry(
                "Qwen3 32B",
                "https://huggingface.co/Qwen/Qwen3-32B-GGUF",
                prose="weights are up",
                extra="Released Aug 14.",
            ),
        ]),
    )
    items = ai_wire.items_from_digest(
        [], message, today="2026-08-21",
    )
    assert len(items) == 1
    item = items[0]
    assert item["canonical_key"] == "model:qwen/qwen3-32b"
    assert item["lane"] == "OPEN FRONTIER"
    assert item["url"] == "https://huggingface.co/Qwen/Qwen3-32B-GGUF"
    assert item["org"] == "qwen"
    assert item["published_at"] == "2026-08-14"
    assert item["summary"] == "weights are up"


def test_released_month_after_the_digest_month_uses_the_previous_year():
    message = _digest(
        ("CLOSED FRONTIER", [
            _entry(
                "Old Year",
                "https://example.com/old-year",
                extra="Released Dec 30.",
            ),
        ]),
    )
    items = ai_wire.items_from_digest([], message, today="2026-01-02")
    assert items[0]["published_at"] == "2025-12-30"


def test_summary_is_plain_text_capped_at_600():
    prose = "word " * 200
    message = _digest(
        ("LOCAL", [_entry("Tiny", "https://example.com/tiny", prose=prose)]),
    )
    item = ai_wire.items_from_digest([], message)[0]
    assert len(item["summary"]) <= 600
    assert "<" not in item["summary"]


def test_primary_url_skips_an_aggregator_when_a_model_page_is_present():
    message = _digest(
        ("OPEN FRONTIER", [
            "<b>Qwen3</b> — <i>weights</i> "
            '<a href="https://aireleasetracker.com/latest">→ tracker</a> '
            '<a href="https://huggingface.co/Qwen/Qwen3-32B">→ HF</a>',
        ]),
    )
    item = ai_wire.items_from_digest([], message)[0]
    assert item["url"] == "https://huggingface.co/Qwen/Qwen3-32B"
    assert item["canonical_key"] == "model:qwen/qwen3-32b"


def test_only_aggregator_link_is_still_included():
    message = _digest(
        ("WATCH", [
            "<b>Some Model</b> — <i>announced</i> "
            '<a href="https://aireleasetracker.com/latest">→ tracker</a>',
        ]),
    )
    item = ai_wire.items_from_digest([], message)[0]
    assert item["lane"] == "WATCH"
    assert item["url"] == "https://aireleasetracker.com/latest"
    assert item["canonical_key"] == "model:some-model"


def test_entries_past_the_telegram_limit_are_not_included():
    first = _entry("Kept", "https://huggingface.co/acme/kept")
    message = (
        "🤖 <b>ModelBytes Digest</b>\n"
        "<i>Saturday, October 03, 2026</i>\n\n"
        "━━━ <b>OPEN FRONTIER</b> 🔓\n"
        + first + "\n"
        + ("pad line\n" * 600)
        + "━━━ <b>LOCAL</b> 🏠\n"
        + _entry("Dropped", "https://huggingface.co/acme/dropped")
        + "\n"
    )
    assert len(message) > 4096
    items = ai_wire.items_from_digest([], message)
    assert [item["canonical_key"] for item in items] == ["model:acme/kept"]


def test_entry_without_a_url_is_omitted():
    message = _digest(
        ("LOCAL", ["<b>No Link</b> — <i>nothing to cite</i>"]),
    )
    assert ai_wire.items_from_digest([], message) == []


def test_also_tracked_bullets_are_included():
    message = (
        "🤖 <b>ModelBytes Digest</b>\n"
        "<i>Saturday, October 03, 2026</i>\n\n"
        "━━━ <b>ALSO TRACKED</b>\n"
        '  • <a href="https://huggingface.co/acme/Model-X">Model-X</a> (hf)\n'
    )
    items = ai_wire.items_from_digest([], message, message_id="4242")
    assert len(items) == 1
    assert items[0]["canonical_key"] == "model:acme/model-x"
    assert items[0]["lane"] == "ALSO TRACKED"
    assert items[0]["title"] == "Model-X"
    assert items[0]["channel_post_url"] == "https://t.me/ModelBytes/4242"


def test_modality_map_covers_the_contract_enum():
    assert ai_wire.wire_modality("text") == "text"
    assert ai_wire.wire_modality("language") == "text"
    assert ai_wire.wire_modality("multimodal") == "multimodal"
    assert ai_wire.wire_modality("text-to-video") == "video"
    assert ai_wire.wire_modality("text-to-speech") == "audio"
    assert ai_wire.wire_modality("speech-to-text") == "audio"
    assert ai_wire.wire_modality("embedding") is None


# ── flag and HTTP ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("flag,url,token", [
    (None, WIRE_URL, TOKEN),
    ("", WIRE_URL, TOKEN),
    ("0", WIRE_URL, TOKEN),
    ("true", WIRE_URL, TOKEN),
    ("1", None, TOKEN),
    ("1", WIRE_URL, None),
    ("1", "", TOKEN),
    ("1", WIRE_URL, ""),
])
def test_flag_off_is_a_noop(monkeypatch, flag, url, token):
    _arm(monkeypatch, flag=flag, url=url, token=token)
    _forbid_http(monkeypatch)
    assert ai_wire.enabled() is False
    message = _digest(
        ("OPEN FRONTIER", [_entry("DeepSeek V4", "https://example.com/m")]),
    )
    assert ai_wire.push_published_digest([_model("acme/m")], message) is None


def test_enabled_requires_flag_url_and_token(monkeypatch):
    _arm(monkeypatch)
    assert ai_wire.enabled() is True


def test_push_is_one_batch_with_bearer_auth_and_a_five_second_timeout(
        monkeypatch, capsys):
    _arm(monkeypatch)
    seen = _install_urlopen(
        monkeypatch, 200, b'{"upserted": 2, "created": 1, "keys": ["a", "b"]}',
    )
    message = _digest(
        ("OPEN FRONTIER", [
            _entry("One", "https://huggingface.co/acme/one"),
            _entry("Two", "https://huggingface.co/acme/two"),
        ]),
    )
    ai_wire.push_published_digest([], message, message_id=7)
    assert len(seen) == 1
    req, timeout = seen[0]
    assert timeout == 5
    assert req.full_url == "https://wire.invalid/api/ingest/items"
    assert req.get_header("Authorization") == f"Bearer {TOKEN}"
    body = json.loads(req.data.decode())
    assert list(body) == ["items"]
    assert len(body["items"]) == 2
    assert [item["canonical_key"] for item in body["items"]] == [
        "model:acme/one", "model:acme/two",
    ]
    err = capsys.readouterr().err
    assert "ai_wire push ok n=2" in err
    assert TOKEN not in err


def test_push_retries_once_then_logs_failure_without_raising(monkeypatch, capsys):
    _arm(monkeypatch)
    seen = []

    def _open(req, timeout=None):
        seen.append(timeout)
        raise TimeoutError("timed out talking to wire.invalid")

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    message = _digest(
        ("LOCAL", [_entry("One", "https://example.com/one")]),
    )
    assert ai_wire.push_published_digest([], message) is None
    assert seen == [5, 5]
    err = capsys.readouterr().err
    assert "ai_wire push failed:" in err
    assert TOKEN not in err
    assert "wire.invalid" not in err


def test_client_error_is_not_retried(monkeypatch, capsys):
    _arm(monkeypatch)
    seen = _install_urlopen(monkeypatch, status=401, body=b'{"error":"bad token"}')
    message = _digest(
        ("LOCAL", [_entry("One", "https://example.com/one")]),
    )
    ai_wire.push_published_digest([], message)
    assert len(seen) == 1
    assert "ai_wire push failed:" in capsys.readouterr().err


def test_server_error_retries_once_then_succeeds(monkeypatch, capsys):
    _arm(monkeypatch)
    calls = {"n": 0}

    def _open(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                req.full_url, 503, "unavailable", {}, io.BytesIO(b""))
        return _Resp(200, b'{"upserted": 1}')

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    message = _digest(
        ("LOCAL", [_entry("One", "https://example.com/one")]),
    )
    ai_wire.push_published_digest([], message)
    assert calls["n"] == 2
    assert "ai_wire push ok n=1" in capsys.readouterr().err


def test_batch_is_capped_at_100(monkeypatch):
    _arm(monkeypatch)
    seen = _install_urlopen(monkeypatch, 200, b'{"upserted": 100}')
    items = [
        {
            "source_bot": "modelbytes",
            "kind": "model",
            "canonical_key": f"model:acme/m{i}",
            "title": f"M{i}",
            "url": f"https://example.com/{i}",
        }
        for i in range(101)
    ]
    ai_wire.push_items(items)
    body = json.loads(seen[0][0].data.decode())
    assert len(body["items"]) == 100


# ── publish seam ──────────────────────────────────────────────────────────


def _quiet_main(monkeypatch, tmp_path, models=None, summarize=None, preview=False):
    monkeypatch.chdir(tmp_path)
    argv = ["monitor.py", "--preview"] if preview else ["monitor.py"]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(monitor, "INLINE_PRIMARY", True)
    monkeypatch.setattr(monitor, "TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr(monitor, "TELEGRAM_CHANNEL_ID", "-100123")
    monkeypatch.setattr(monitor, "DATABASE_URL", "postgres://invalid/modelbytes")
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
    monkeypatch.setattr(monitor, "fetch_openrouter_models", lambda: [])
    monkeypatch.setattr(monitor, "fetch_ollama_models", lambda: [])
    monkeypatch.setattr(monitor, "fetch_huggingface_trending", lambda: [])
    monkeypatch.setattr(monitor, "fetch_major_orgs", lambda: list(models or []))
    monkeypatch.setattr(monitor, "fetch_hf_text_generation", lambda: [])
    monkeypatch.setattr(monitor, "fetch_artificial_analysis_models", lambda *a, **k: [])
    monkeypatch.setattr(monitor, "fetch_testingcatalog_models", lambda *a, **k: [])
    if summarize is not None:
        monkeypatch.setattr(monitor, "summarize_models", summarize)


def test_main_pushes_only_the_sent_digest_after_telegram(monkeypatch, tmp_path):
    _arm(monkeypatch)
    shown = _model(
        "deepseek-ai/DeepSeek-V4",
        url="https://huggingface.co/deepseek-ai/DeepSeek-V4",
        release_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )
    omitted = _model(
        "other/Held-Back",
        url="https://huggingface.co/other/Held-Back",
        release_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )
    message = _digest(
        ("OPEN FRONTIER", [_entry("DeepSeek V4", shown.url)]),
    )
    order = []
    seen = _install_urlopen(monkeypatch, 200, b'{"upserted": 1}')

    def send(body):
        order.append("send")
        monitor.LAST_TELEGRAM_MESSAGE_ID = 55
        return True

    def summarize(models, *args, **kwargs):
        order.append("summarize")
        return message

    _quiet_main(monkeypatch, tmp_path, models=[shown, omitted], summarize=summarize)
    monkeypatch.setattr(monitor, "send_telegram_post", send)
    assert monitor.main() == 0
    assert order == ["summarize", "send"]
    assert len(seen) == 1
    posted = json.loads(seen[0][0].data.decode())
    assert [item["canonical_key"] for item in posted["items"]] == [
        "model:deepseek-ai/deepseek-v4",
    ]
    assert posted["items"][0]["channel_post_url"] == "https://t.me/ModelBytes/55"


def test_main_preview_and_send_failure_do_not_push(monkeypatch, tmp_path):
    _arm(monkeypatch)
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise AssertionError("unexpected HTTP")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    shown = _model(
        "deepseek-ai/DeepSeek-V4",
        url="https://huggingface.co/deepseek-ai/DeepSeek-V4",
        release_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )
    message = _digest(("OPEN FRONTIER", [_entry("DeepSeek V4", shown.url)]))
    _quiet_main(
        monkeypatch, tmp_path, models=[shown],
        summarize=lambda models, *a, **k: message,
        preview=True,
    )
    assert monitor.main() == 0
    assert calls == []

    _quiet_main(
        monkeypatch, tmp_path, models=[shown],
        summarize=lambda models, *a, **k: message,
    )
    monkeypatch.setattr(monitor, "send_telegram_post", lambda message: False)
    assert monitor.main() == 1
    assert calls == []


def test_main_survives_a_raising_push(monkeypatch, tmp_path):
    _arm(monkeypatch)
    _forbid_http(monkeypatch)
    shown = _model(
        "deepseek-ai/DeepSeek-V4",
        url="https://huggingface.co/deepseek-ai/DeepSeek-V4",
        release_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )
    message = _digest(("OPEN FRONTIER", [_entry("DeepSeek V4", shown.url)]))

    def explode(*args, **kwargs):
        raise RuntimeError("wire down")

    monkeypatch.setattr(ai_wire, "push_published_digest", explode)
    _quiet_main(
        monkeypatch, tmp_path, models=[shown],
        summarize=lambda models, *a, **k: message,
    )
    monkeypatch.setattr(monitor, "send_telegram_post", lambda message: True)
    assert monitor.main() == 0


def test_curated_post_pushes_entries_parsed_from_the_body(monkeypatch, tmp_path):
    _arm(monkeypatch)
    seen = _install_urlopen(monkeypatch, 200, b'{"upserted": 1}')
    monkeypatch.chdir(tmp_path)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pending = tmp_path / "pending"
    pending.mkdir()
    body = _digest(
        ("CLOSED FRONTIER", [
            _entry("Ox Alpha", "https://openrouter.ai/models/stealth/ox-alpha"),
        ]),
    )
    (pending / f"{today}.txt").write_text(body, encoding="utf-8")
    monkeypatch.setattr(monitor, "has_posted_digest", lambda date_str: False)
    monkeypatch.setattr(monitor, "mark_posted_digest", lambda *a, **k: True)
    monkeypatch.setattr(monitor, "send_slack_post", lambda message: True)
    monkeypatch.setattr(monitor, "record_publish_run", lambda *a, **k: True)
    monkeypatch.setattr(monitor, "ping_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "send_ops_alert", lambda text: True)
    monkeypatch.setattr(monitor, "validate_digest_for_publish",
                        lambda message, mode="curated": (message, [], []))

    def send(message):
        monitor.LAST_TELEGRAM_MESSAGE_ID = 88
        return True

    monkeypatch.setattr(monitor, "send_telegram_post", send)
    assert monitor.try_post_pending_curated() is True
    assert len(seen) == 1
    posted = json.loads(seen[0][0].data.decode())
    assert posted["items"][0]["canonical_key"] == "model:stealth/ox-alpha"
    assert posted["items"][0]["lane"] == "CLOSED FRONTIER"
    assert posted["items"][0]["channel_post_url"] == "https://t.me/ModelBytes/88"


# ── backfill ──────────────────────────────────────────────────────────────


class _HistDB:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False
        self.sql = ""
        self.params = None

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def commit(self):
        return None

    def close(self):
        self.closed = True

    def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())
        self.params = params
        upper = self.sql.upper()
        if "POSTED_DIGESTS" not in upper or "PUBLISH_RUNS" not in upper:
            raise AssertionError(self.sql)
        if "TELEGRAM_MESSAGE_ID" not in upper or "BODY" not in upper:
            raise AssertionError(self.sql)

    def fetchall(self):
        limit = 14
        if self.params:
            limit = int(self.params[-1])
        kept = [
            (row["post_date"], row["body"], row["posted_at"], row["message_id"])
            for row in self.rows
            if (row.get("body") or "").strip()
        ]
        kept.sort(key=lambda row: str(row[0]), reverse=True)
        return kept[:limit]


def _history_row(post_date, body, message_id=None):
    return {
        "post_date": post_date,
        "body": body,
        "posted_at": datetime(2026, 10, 3, 16, 5, tzinfo=timezone.utc),
        "message_id": message_id,
    }


def test_backfill_dry_run_reads_history_and_does_not_post(monkeypatch, capsys):
    _arm(monkeypatch)
    _forbid_http(monkeypatch)
    body = _digest(
        ("OPEN FRONTIER", [
            _entry("DeepSeek V4", "https://huggingface.co/deepseek-ai/DeepSeek-V4"),
        ]),
    )
    mem = _HistDB([
        _history_row(date(2026, 10, 3), body, 55),
        _history_row(date(2026, 10, 2), "   ", 1),
        _history_row(date(2026, 10, 1), body, None),
    ])
    monkeypatch.setattr(monitor, "DATABASE_URL", "postgres://invalid/modelbytes")
    monkeypatch.setattr(monitor, "_db_connect", lambda **k: mem)
    code = ai_wire.backfill(days=1, apply=False)
    assert code == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "model:deepseek-ai/deepseek-v4" in out
    assert "https://t.me/ModelBytes/55" in out
    assert mem.closed is True


def test_backfill_apply_posts_one_batch_per_digest(monkeypatch, capsys):
    _arm(monkeypatch)
    seen = _install_urlopen(monkeypatch, 200, b'{"upserted": 1}')
    body_a = _digest(
        ("OPEN FRONTIER", [
            _entry("One", "https://huggingface.co/acme/one"),
        ]),
    )
    body_b = _digest(
        ("LOCAL", [
            _entry("Two", "https://huggingface.co/acme/two"),
        ]),
    )
    mem = _HistDB([
        _history_row(date(2026, 10, 3), body_a, 9),
        _history_row(date(2026, 10, 2), body_b, 8),
    ])
    monkeypatch.setattr(monitor, "DATABASE_URL", "postgres://invalid/modelbytes")
    monkeypatch.setattr(monitor, "_db_connect", lambda **k: mem)
    assert ai_wire.backfill(days=14, apply=True) == 0
    assert len(seen) == 2
    keys = []
    for req, timeout in seen:
        assert timeout == 5
        payload = json.loads(req.data.decode())
        assert len(payload["items"]) == 1
        keys.append(payload["items"][0]["canonical_key"])
    assert keys == ["model:acme/one", "model:acme/two"]
    err = capsys.readouterr().err
    assert err.count("ai_wire push ok n=") == 2


def test_backfill_without_history_is_a_noop(monkeypatch, capsys):
    _arm(monkeypatch)
    _forbid_http(monkeypatch)

    def boom(**kwargs):
        raise AssertionError("no database")

    monkeypatch.setattr(monitor, "DATABASE_URL", "")
    monkeypatch.setattr(monitor, "_db_connect", boom)
    assert ai_wire.backfill(apply=True) == 0
    assert "no posted digest history" in capsys.readouterr().out.lower()


def test_backfill_apply_does_not_post_when_disabled(monkeypatch, capsys):
    _arm(monkeypatch, flag="0")
    _forbid_http(monkeypatch)
    body = _digest(
        ("LOCAL", [_entry("One", "https://example.com/one")]),
    )
    mem = _HistDB([_history_row(date(2026, 10, 3), body, 3)])
    monkeypatch.setattr(monitor, "DATABASE_URL", "postgres://invalid/modelbytes")
    monkeypatch.setattr(monitor, "_db_connect", lambda **k: mem)
    assert ai_wire.backfill(apply=True) == 0
    out = capsys.readouterr().out
    assert "AI_WIRE_ENABLED" in out
    assert "model:one" in out


def test_cli_guard_runs_backfill_before_publish():
    text = (ROOT / "monitor.py").read_text()
    main_block = text.split('if __name__ == "__main__":', 1)[1]
    assert "--ai-wire-backfill" in main_block
    assert main_block.index("--ai-wire-backfill") < main_block.index("_rc = main()")

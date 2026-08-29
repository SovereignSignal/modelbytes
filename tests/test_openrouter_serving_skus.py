"""OpenRouter :batch / :free serving SKUs must not become digest items.

2026-08-29: catalogs found one unseen ID — z-ai/glm-5.3-flash:batch — the
writer produced 0 entries, and the template posted a 222-char ALSO TRACKED
stub. :batch and :free are serving duplicates of a base id, not releases.

Levers:
1. Fetch skips :batch / :free (collapse onto the base id).
2. Template fallback that would only render those leftovers is a no-post day.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor
from tests.test_fallback_hardening import _live_main_env


def _or_row(model_id, created=1_775_000_000, owned_by="z-ai"):
    return {
        "id": model_id,
        "owned_by": owned_by,
        "created": created,
        "context_length": 128000,
        "description": "A model.",
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
    }


class _Resp:
    def __init__(self, rows):
        self._rows = rows

    def json(self):
        return {"data": self._rows}


def _batch_release(**kw):
    fields = dict(
        name="z-ai/glm-5.3-flash:batch",
        provider="Z.AI",
        source="openrouter",
        url="https://openrouter.ai/models/z-ai/glm-5.3-flash:batch",
        description="Batch inference SKU.",
        release_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        is_open_source=False,
    )
    fields.update(kw)
    return monitor.ModelRelease(**fields)


# ── helpers ──

def test_serving_sku_detects_batch_and_free():
    assert monitor.is_openrouter_serving_sku("z-ai/glm-5.3-flash:batch")
    assert monitor.is_openrouter_serving_sku("google/gemini-3-flash:free")
    assert monitor.is_openrouter_serving_sku("ACME/X:BATCH")
    assert not monitor.is_openrouter_serving_sku("z-ai/glm-5.3-flash")
    assert not monitor.is_openrouter_serving_sku("tencent/hy4-preview")
    assert not monitor.is_openrouter_serving_sku("")


def test_openrouter_base_id_strips_serving_suffix():
    assert monitor._openrouter_base_id("z-ai/glm-5.3-flash:batch") == (
        "z-ai/glm-5.3-flash")
    assert monitor._openrouter_base_id("google/gemini-3-flash:free") == (
        "google/gemini-3-flash")
    assert monitor._openrouter_base_id("z-ai/glm-5.3-flash") == (
        "z-ai/glm-5.3-flash")


# ── lever 1: fetch collapse ──

def test_fetch_openrouter_skips_batch_and_free_when_base_present(monkeypatch):
    rows = [
        _or_row("z-ai/glm-5.3-flash"),
        _or_row("z-ai/glm-5.3-flash:batch"),
        _or_row("z-ai/glm-5.3-flash:free"),
        _or_row("tencent/hy4-preview", owned_by="tencent"),
    ]
    monkeypatch.setattr(monitor, "_http_get", lambda *a, **k: _Resp(rows))
    names = [m.name for m in monitor.fetch_openrouter_models()]
    assert "z-ai/glm-5.3-flash" in names
    assert "tencent/hy4-preview" in names
    assert "z-ai/glm-5.3-flash:batch" not in names
    assert "z-ai/glm-5.3-flash:free" not in names


def test_fetch_openrouter_skips_orphan_batch(monkeypatch):
    # Base already seen yesterday; only the :batch sibling is "new" in the
    # catalog. It is still a serving SKU — do not emit it as a release.
    rows = [_or_row("z-ai/glm-5.3-flash:batch")]
    monkeypatch.setattr(monitor, "_http_get", lambda *a, **k: _Resp(rows))
    assert monitor.fetch_openrouter_models() == []


# ── lever 2: thin template leftover is a quiet day ──

def test_template_serving_sku_only_is_sentinel():
    msg = monitor.build_digest_message([_batch_release()])
    assert msg.strip() == monitor.NO_MODELS_SENTINEL
    assert "ALSO TRACKED" not in msg
    assert "glm-5.3-flash:batch" not in msg


def test_template_drops_serving_sku_keeps_real_model():
    real = monitor.ModelRelease(
        name="acme/X-2",
        provider="Acme",
        source="discovery",
        url="https://vendor.ai/blog/x2",
        description="Acme released X-2, a 70B open model.",
        release_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        likes=600,
    )
    msg = monitor.build_digest_message([_batch_release(), real])
    assert msg.strip() != monitor.NO_MODELS_SENTINEL
    assert "X-2" in msg
    assert "glm-5.3-flash:batch" not in msg


def test_batch_only_day_is_not_posted(monkeypatch, tmp_path):
    # Replay 2026-08-29: one unseen OpenRouter :batch SKU, writer falls back
    # to the template, channel must stay quiet instead of ALSO TRACKED.
    _live_main_env(monkeypatch, tmp_path)
    monkeypatch.setattr(monitor, "save_seen_models", lambda s: None)
    monkeypatch.setattr(monitor, "fetch_openrouter_models",
                        lambda: [_batch_release()])
    monkeypatch.setattr(monitor, "discover_recent_releases", lambda *a, **k: "")
    monkeypatch.setattr(monitor, "LAST_DISCOVERY_MODELS", [])
    monkeypatch.setattr(
        monitor, "summarize_models",
        lambda models, *a, **k: monitor.build_digest_message(models),
    )

    sent, alerts, runs = [], [], []
    monkeypatch.setattr(monitor, "send_ops_alert", lambda t: alerts.append(t) or True)
    monkeypatch.setattr(monitor, "record_publish_run",
                        lambda *a, **k: runs.append((a, k)) or True)
    monkeypatch.setattr(monitor, "ping_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "send_telegram_post",
                        lambda m: sent.append(m) or True)

    rc = monitor.main()
    assert rc == 0
    assert not sent, f"serving-SKU-only day must not post; got {sent}"
    assert any("no-models" in a for (a, k) in runs), \
        f"expected a no-models publish_run; got {runs}"
    assert not alerts, (
        "a quiet serving-SKU leftover day must not 🚨 the operator; "
        f"got {alerts}")

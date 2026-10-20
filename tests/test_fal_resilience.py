"""fal.ai rate limits: one fetch per run, Retry-After, degraded + last-good fallback.

Nothing here calls api.fal.ai; token values are placeholders and URLs are
example.com. The last-good store is stubbed with an in-memory dict.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor
from test_fal_models import FIXTURE, TODAY, _Page, _install_fal


def _rows():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def store(monkeypatch):
    saved = {}
    monkeypatch.setattr(monitor, "_fal_load_last_good", lambda: saved.get("v"))
    monkeypatch.setattr(
        monitor, "_fal_save_last_good",
        lambda count, rows: saved.__setitem__("v", {"count": count, "rows": rows}))
    return saved


def _names(models):
    return sorted(m.name for m in models)


def test_fal_is_fetched_once_per_run(monkeypatch, store):
    calls = _install_fal(monkeypatch, page_size=1000)
    first = monitor.fetch_fal_models(set(), today=TODAY)
    second = monitor.fetch_fal_models(set(), today=TODAY)
    assert len(calls) == 1
    assert _names(first) == _names(second) and first
    monitor.reset_fal_run_cache()
    monitor.fetch_fal_models(set(), today=TODAY)
    assert len(calls) == 2


def test_normal_fetch_is_ok_and_persisted(monkeypatch, store):
    _install_fal(monkeypatch)
    models = monitor.fetch_fal_models(set(), today=TODAY)
    assert models
    assert store["v"]["count"] == len(_rows())
    assert store["v"]["rows"]
    assert all(monitor._fal_is_launch_endpoint(r) for r in store["v"]["rows"])
    err = monitor._consume_source_error("fal")
    assert err == ""
    assert monitor.LAST_FAL_DEGRADED is False


def _degraded_line(monkeypatch, tmp_path, items, err):
    monkeypatch.setattr(monitor, "ADMIN_CHAT_ID", "")
    return monitor.flush_source_health(
        [{"source": "fal", "items": items, "error": err, "status": "degraded"}],
        preview=True)[0]


def test_429_exhausted_is_degraded_and_falls_back(monkeypatch, store, tmp_path):
    _install_fal(monkeypatch)
    good = monitor.fetch_fal_models(set(), today=TODAY)
    monitor.reset_fal_run_cache()

    class Resp429:
        status_code = 429
        headers = {"Retry-After": "2"}

        def raise_for_status(self):
            raise requests.HTTPError("429 Too Many Requests", response=self)

    attempts, sleeps = [], []
    monkeypatch.setattr(monitor, "_fal_request",
                        lambda url, params: attempts.append(1) or Resp429())
    monkeypatch.setattr(monitor, "_fal_sleep", sleeps.append)
    monkeypatch.setattr(monitor, "_fal_get", monitor._fal_get_with_retry)
    fallback = monitor.fetch_fal_models(set(), today=TODAY)
    assert len(attempts) == 3 and sleeps == [2.0, 2.0]
    assert _names(fallback) == _names(good)
    assert monitor.LAST_FAL_DEGRADED is True
    err = monitor._consume_source_error("fal")
    assert "HTTPError" in err
    line = _degraded_line(monkeypatch, tmp_path, len(fallback), err)
    assert "status=degraded" in line and "HTTPError" in line


def test_fallback_items_still_get_date_filtering(monkeypatch, store):
    _install_fal(monkeypatch)
    monitor.fetch_fal_models(set(), today=TODAY)
    monitor.reset_fal_run_cache()
    _install_fal(monkeypatch, fail=True)
    later = (datetime.fromisoformat(TODAY) + timedelta(days=60)).date().isoformat()
    assert monitor.fetch_fal_models(set(), today=later) == []
    assert monitor.LAST_FAL_DEGRADED is True


def test_low_count_is_degraded_and_falls_back(monkeypatch, store):
    _install_fal(monkeypatch)
    good = monitor.fetch_fal_models(set(), today=TODAY)
    monitor.reset_fal_run_cache()
    monitor._consume_source_error("fal")
    store["v"]["count"] = 200  # last good run saw a much bigger catalog
    _install_fal(monkeypatch, rows=_rows()[:1])
    fallback = monitor.fetch_fal_models(set(), today=TODAY)
    assert _names(fallback) == _names(good)
    assert monitor.LAST_FAL_DEGRADED is True
    assert "returned 1 rows" in monitor._consume_source_error("fal")
    assert store["v"]["count"] == 200  # a bad fetch never replaces last good


def test_floor_rule():
    assert monitor._fal_count_is_low(4, 20)
    assert monitor._fal_count_is_low(1, 8)
    assert not monitor._fal_count_is_low(2, 8)
    assert not monitor._fal_count_is_low(3, 0)


def test_failure_without_last_good_stays_an_error(monkeypatch, store):
    _install_fal(monkeypatch, fail=True)
    assert monitor.fetch_fal_models(set(), today=TODAY) == []
    assert monitor.LAST_FAL_DEGRADED is False
    assert "fal down" in monitor._consume_source_error("fal")


def test_retry_after_seconds_date_and_cap():
    f = monitor._fal_retry_after
    assert f("7", 1) == 7.0
    assert f("900", 1) == 60.0
    soon = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    assert 25 <= f(soon, 1) <= 30
    assert f("", 2) == monitor.HTTP_BACKOFF_SECONDS * 2
    assert f("garbage", 1) == monitor.HTTP_BACKOFF_SECONDS


def test_429_then_success_returns_the_page(monkeypatch):
    class R429:
        status_code = 429
        headers = {}

    seq = [R429(), _Page({"models": []})]
    sleeps = []
    monkeypatch.setattr(monitor, "_fal_request", lambda u, p: seq.pop(0))
    monkeypatch.setattr(monitor, "_fal_sleep", sleeps.append)
    assert monitor._fal_get_with_retry("https://example.com/fal", {}).json() == {"models": []}
    assert len(sleeps) == 1


def test_main_marks_degraded_status(monkeypatch):
    row = {"source": "fal", "items": 3, "error": "HTTPError: 429", "status": "degraded"}
    line = monitor.flush_source_health([row], preview=True)[0]
    assert line.startswith("source_health source=fal status=degraded items=3")


def test_degraded_source_alerts_through_the_existing_path(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(monitor, "ADMIN_CHAT_ID", "placeholder")
    monkeypatch.setattr(monitor, "send_ops_alert", lambda text: sent.append(text) or True)
    path = tmp_path / "health.json"
    t0 = datetime(2026, 10, 20, tzinfo=timezone.utc)
    row = {"source": "fal", "items": 3, "error": "x", "status": "degraded"}
    monitor.flush_source_health([row], now=t0, path=path)
    assert sent == []
    monitor.flush_source_health([row], now=t0 + timedelta(hours=25), path=path)
    assert len(sent) == 1 and "fal" in sent[0] and "degraded" in sent[0]

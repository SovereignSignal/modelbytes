"""fal newest-first list (sort=recent) and variant-aware recent-name dedupe.

The fixture is the first page of GET /v1/models?limit=100&sort=recent taken
on 2026-10-23, trimmed to 44 rows and sanitized (no thumbnails or URLs,
cursor "placeholder"). Nothing here calls api.fal.ai.
"""
import json
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor
from test_fal_models import FIXTURE as CATALOG_FIXTURE, _Page


RECENT_FIXTURE = Path(__file__).parent / "fixtures" / "fal_recent_2026-10-23.json"
BASELINED = {"fal/krea-v3"}
# Names the channel printed in the 10 days before each run (t.me/s/ModelBytes).
RECENT_NAMES = ["HeyGen Voice", "MOSS Image", "Nemotron 3.5 Bolt 74B A9B",
                "FLUX.3 Image Klein Base", "Ming 3.0 ASR Tiny"]


def _recent_body(cutoff=None):
    """The recorded page as fal would have served it before `cutoff` (UTC)."""
    body = json.loads(RECENT_FIXTURE.read_text())
    if cutoff:
        body["models"] = [r for r in body["models"]
                          if r["metadata"]["date"] < cutoff]
    return body


def _stale_page():
    """A next page whose rows are all past the 14-day clock on 2026-10-22."""
    body = _recent_body()
    body["models"] = [r for r in body["models"]
                      if r["metadata"]["date"] < "2026-10-08"]
    body["next_cursor"] = "placeholder-2"
    return body


@pytest.fixture
def store(monkeypatch):
    """Last good full crawl: the 2026-10-16 catalog fixture."""
    rows = json.loads(CATALOG_FIXTURE.read_text())
    saved = {"v": {"count": len(rows),
                   "rows": [r for r in rows if monitor._fal_is_launch_endpoint(r)]}}
    monkeypatch.setattr(monitor, "_fal_load_last_good", lambda: saved.get("v"))
    monkeypatch.setattr(
        monitor, "_fal_save_last_good",
        lambda count, rows: saved.__setitem__("v", {"count": count, "rows": rows}))
    return saved


def _install(monkeypatch, recent=None, full_rows=None, recent_error=None):
    """recent: list of page bodies for sort=recent. Full crawl: one page."""
    pages = recent if recent is not None else [_recent_body(), _stale_page()]
    calls = []

    def fake_get(url, params):
        calls.append(dict(params))
        if params.get("sort") == "recent":
            if recent_error is not None:
                raise recent_error
            index = min(sum(1 for c in calls if c.get("sort") == "recent") - 1,
                        len(pages) - 1)
            return _Page(pages[index])
        return _Page({"models": full_rows or [], "next_cursor": None,
                      "has_more": False})

    monkeypatch.setattr(monitor, "_fal_get", fake_get)
    return calls


def _names(models):
    return sorted(m.name for m in models)


def test_baselined_run_reads_recent_pages_until_one_is_all_stale(monkeypatch, store):
    calls = _install(monkeypatch)
    monitor.fetch_fal_models(set(BASELINED), today="2026-10-22")
    assert calls == [{"limit": 100, "sort": "recent"},
                     {"limit": 100, "sort": "recent", "cursor": "placeholder"}]


@pytest.mark.parametrize("today,cutoff,expected", [
    ("2026-10-20", "2026-10-20T16:00", ["Fibo 2.0"]),
    ("2026-10-21", "2026-10-21T16:00",
     ["Fibo 2.0", "HeyGen Video", "HeyGen Voice Turbo"]),
    ("2026-10-22", "2026-10-22T16:00",
     ["Fibo 2.0", "HeyGen Video", "HeyGen Voice Turbo", "Ideogram Vector"]),
])
def test_recent_page_catches_the_oct_20_to_22_launches(
        monkeypatch, store, today, cutoff, expected):
    _install(monkeypatch, recent=[_recent_body(cutoff), _stale_page()])
    models = monitor.fetch_fal_models(
        set(BASELINED), today=today, recent_names=RECENT_NAMES)
    assert _names(models) == expected


def test_recent_launch_rows_render_as_hosted_fal_entries(monkeypatch, store):
    _install(monkeypatch)
    by = {m.name: m for m in monitor.fetch_fal_models(
        set(BASELINED), today="2026-10-22", recent_names=RECENT_NAMES)}
    vector = by["Ideogram Vector"]
    assert vector.provider == "Ideogram"
    assert vector.release_date == "2026-10-22"
    assert vector.url == "https://fal.ai/models/ideogram/vector/text-to-vector"
    assert "fal-id:fal/ideogram-vector" in vector.unique_traits
    video = by["HeyGen Video"]
    assert video.provider == "HeyGen"
    assert video.modality == "text-to-video"
    assert video.url == "https://fal.ai/models/heygen/heygen-video/text-to-video"
    turbo = by["HeyGen Voice Turbo"]
    assert turbo.url == "https://fal.ai/models/heygen/heygen-voice/turbo"
    assert turbo.modality == "text-to-speech"
    # Publishing the variant records the whole launch, so the covered base
    # endpoint cannot come back as a candidate on the next run.
    assert "fal-id:fal/heygen-voice" in turbo.unique_traits
    assert "fal-id:fal/heygen/heygen-voice" in turbo.unique_traits


def test_stale_launches_on_the_recent_page_are_skipped(monkeypatch, store):
    _install(monkeypatch)
    names = " ".join(_names(monitor.fetch_fal_models(
        set(BASELINED), today="2026-10-22", recent_names=RECENT_NAMES)))
    for stale in ("FLUX 3 Max", "Kling", "Krea", "Nemotron", "Meshy", "Kotoba"):
        assert stale not in names
    assert "MOSS Image" not in names  # printed on Oct 19


def test_first_run_without_a_fal_baseline_reads_the_full_catalog(monkeypatch, store):
    calls = _install(monkeypatch)
    monitor.fetch_fal_models(set(), today="2026-10-22")
    assert calls == [{"limit": 100}]


def test_no_last_good_list_reads_the_full_catalog(monkeypatch):
    calls = _install(monkeypatch)
    monitor.fetch_fal_models(set(BASELINED), today="2026-10-22")
    assert calls == [{"limit": 100}]


def test_unacknowledged_sort_falls_back_to_the_full_catalog(monkeypatch, store):
    body = _recent_body()
    del body["ordering"]
    rows = json.loads(CATALOG_FIXTURE.read_text())
    calls = _install(monkeypatch, recent=[body], full_rows=rows)
    models = monitor.fetch_fal_models(set(BASELINED), today="2026-10-16")
    assert calls == [{"limit": 100, "sort": "recent"}, {"limit": 100}]
    assert "Krea 3" not in _names(models)  # fal/krea-v3 is in the baseline
    assert "NVIDIA Nemotron 3.5 Super" in _names(models)
    assert monitor.LAST_FAL_DEGRADED is False


def test_empty_recent_page_falls_back_to_the_full_catalog(monkeypatch, store):
    calls = _install(monkeypatch, recent=[{
        "models": [], "next_cursor": None, "has_more": False,
        "ordering": {"sort": "recent"}}])
    monitor.fetch_fal_models(set(BASELINED), today="2026-10-22")
    assert calls[-1] == {"limit": 100}


def test_rate_limited_recent_page_uses_last_good_without_a_full_crawl(
        monkeypatch, store):
    calls = _install(monkeypatch, recent_error=requests.HTTPError("429 Too Many Requests"))
    monitor.fetch_fal_models(set(BASELINED), today="2026-10-16")
    assert calls == [{"limit": 100, "sort": "recent"}]
    assert monitor.LAST_FAL_DEGRADED is True
    assert "HTTPError" in monitor._consume_source_error("fal")


def test_an_old_row_on_a_page_does_not_end_the_read(monkeypatch, store):
    # fal sorts by publication date, but metadata.date is the creation date:
    # the recorded page has kotoba-stt (2026-10-09) and vidu q4 (2026-10-05)
    # among newer rows. A launch on the next page must still be read.
    first = _recent_body()
    first["models"] = [r for r in first["models"]
                       if not r["endpoint_id"].startswith("ideogram/")]
    second = _stale_page()
    second["models"] = [r for r in _recent_body()["models"]
                        if r["endpoint_id"].startswith("ideogram/")] + second["models"]
    calls = _install(monkeypatch, recent=[first, second, _stale_page()])
    models = monitor.fetch_fal_models(set(BASELINED), today="2026-10-22",
                                      recent_names=RECENT_NAMES)
    assert "Ideogram Vector" in _names(models)
    assert len(calls) == 3


def test_recent_page_cap_stops_a_runaway_cursor(monkeypatch, store):
    fresh = _recent_body()
    fresh["models"] = fresh["models"][:2]
    calls = _install(monkeypatch, recent=[fresh])
    monitor.fetch_fal_models(set(BASELINED), today="2026-10-22")
    assert len(calls) == monitor._FAL_RECENT_PAGE_CAP


def test_recent_rows_feed_the_baseline_and_leave_last_good_alone(monkeypatch, store):
    before = json.dumps(store["v"], sort_keys=True)
    _install(monkeypatch)
    seen = set(BASELINED)
    models = monitor.fetch_fal_models(seen, today="2026-10-22",
                                      recent_names=RECENT_NAMES)
    monitor._note_fal_baseline(seen, preview=False)
    assert "fal/flux-3-max" in seen  # stale launch, recorded silently
    assert "fal/moss-image" in seen  # covered by a recent digest
    for model in models:  # candidates stay unseen until a post marks them
        assert not (monitor._fal_ids_from_model(model) & seen)
    assert json.dumps(store["v"], sort_keys=True) == before



def test_new_variant_of_a_recent_name():
    f = monitor._is_new_variant_name
    assert f("HeyGen Voice Turbo", ["HeyGen Voice"])
    assert f("HeyGen Voice 2", ["HeyGen Voice"])
    assert f("FLUX 3 Max", ["FLUX.3"])
    # Repeats, vendor prefixes and task endpoints are not new models.
    assert not f("HeyGen Voice", ["HeyGen Voice"])
    assert not f("NVIDIA Nemotron 3.5 Super", ["Nemotron 3.5 Super"])
    assert not f("H3 Max Reframe", ["H3 Max"])
    # A variant that was itself printed is covered.
    assert not f("HeyGen Voice Turbo", ["HeyGen Voice", "HeyGen Voice Turbo"])
    # Unrelated names say nothing.
    assert not f("Ideogram Vector", ["HeyGen Voice"])


def test_covered_launch_without_a_new_variant_stays_skipped(monkeypatch, store):
    _install(monkeypatch)
    names = _names(monitor.fetch_fal_models(
        set(BASELINED), today="2026-10-22",
        recent_names=RECENT_NAMES + ["HeyGen Voice Turbo", "HeyGen Video"]))
    assert names == ["Fibo 2.0", "Ideogram Vector"]

"""Parallel.ai web discovery — the inline path's freshness engine (2026-06-16).

The deterministic fetchers surface a static catalog that the dedup table drains
to 0-new after a few days (the dark-channel incident). discover_recent_releases
queries Parallel.ai for genuinely-new releases and feeds cited web context to
the writer model, so the channel stays fresh without the claude.ai curator.
Must degrade silently — discovery failure → fetcher-only, never a crash.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


_PARALLEL_RESP = {
    "results": [
        {"url": "https://vendor.ai/blog/x2", "title": "Acme X-2 released",
         "publish_date": "2026-06-15", "excerpts": ["Acme released X-2, a 70B open model."]},
        {"url": "https://old.example/y", "title": "Old model from last year",
         "publish_date": "2025-01-01", "excerpts": ["ancient"]},
        {"url": "https://huggingface.co/z", "title": "Z model card",
         "publish_date": "", "excerpts": ["Z is a 7B coder."]},
    ]
}


def _resp(payload):
    r = MagicMock(); r.raise_for_status = lambda: None
    r.json.return_value = payload
    return r


@pytest.fixture
def _discovery_on(monkeypatch):
    monkeypatch.setattr(monitor, "PARALLEL_API_KEY", "pk-test")
    monkeypatch.setattr(monitor, "DISCOVERY_ENABLED", True)


def test_discovery_disabled_without_key(monkeypatch):
    monkeypatch.setattr(monitor, "PARALLEL_API_KEY", "")
    monkeypatch.setattr(monitor, "DISCOVERY_ENABLED", True)
    assert monitor.discover_recent_releases(today="2026-06-16") == ""


def test_discovery_filters_stale_and_formats(_discovery_on, monkeypatch):
    monkeypatch.setattr(monitor.requests, "post", lambda url, **k: _resp(_PARALLEL_RESP))
    block = monitor.discover_recent_releases(today="2026-06-16", max_age_days=14)
    assert "Acme X-2 released" in block           # recent → kept
    assert "https://vendor.ai/blog/x2" in block
    assert "Z model card" in block                # undated → kept (writer judges)
    assert "Old model from last year" not in block  # >14 days → dropped


def test_discovery_upgrades_http_urls(_discovery_on, monkeypatch):
    # Writer copies URLs character-for-character; if discovery hands it
    # http://, that scheme lands in the digest and (before 2026-08-13) blocked
    # the whole fallback post. Normalize at the source. Aggregator hosts are
    # dropped entirely — they are how roundup pages reached the writer and
    # zeroed catalog-quiet days (2026-08-24).
    payload = {
        "results": [
            {"url": "http://aireleasetracker.com/", "title": "Tracker dump",
             "publish_date": "2026-06-15", "excerpts": ["new models today"]},
            {"url": "http://vendor.ai/blog/x2", "title": "Acme X-2 released",
             "publish_date": "2026-06-15", "excerpts": ["70B open model"]},
        ]
    }
    monkeypatch.setattr(monitor.requests, "post", lambda url, **k: _resp(payload))
    block = monitor.discover_recent_releases(today="2026-06-16", max_age_days=14)
    assert "aireleasetracker" not in block
    assert "https://vendor.ai/blog/x2" in block
    assert "http://" not in block


def test_discovery_graceful_on_failure(_discovery_on, monkeypatch):
    def boom(url, **k):
        raise RuntimeError("parallel 500")
    monkeypatch.setattr(monitor.requests, "post", boom)
    assert monitor.discover_recent_releases(today="2026-06-16") == ""


def test_discovery_empty_results(_discovery_on, monkeypatch):
    monkeypatch.setattr(monitor.requests, "post", lambda url, **k: _resp({"results": []}))
    assert monitor.discover_recent_releases(today="2026-06-16") == ""


def test_recent_digest_names(tmp_path):
    pend = tmp_path / "pending"; pend.mkdir()
    (pend / "2026-06-14.txt").write_text(
        "🤖 <b>ModelBytes Digest</b>\n\n<b>MiniMax M3</b> — <i>x</i> <a href=\"u\">→ S</a>\n"
        "<b>Gemma 4 12B</b> — <i>y</i> <a href=\"u\">→ S</a>")
    (pend / "2026-06-15.txt").write_text(
        "<b>Kimi K2.7 Code</b> — <i>z</i> <a href=\"u\">→ S</a>")
    names = monitor._recent_digest_names(today="2026-06-16", pending_dir=pend)
    assert "MiniMax M3" in names and "Gemma 4 12B" in names and "Kimi K2.7 Code" in names


def test_recent_digest_names_excludes_today(tmp_path):
    pend = tmp_path / "pending"; pend.mkdir()
    (pend / "2026-06-16.txt").write_text("<b>Today Model</b> — <i>x</i> <a href=\"u\">→ S</a>")
    assert monitor._recent_digest_names(today="2026-06-16", pending_dir=pend) == []


def test_web_context_and_avoid_reach_the_prompt(monkeypatch):
    captured = {}
    class FR:
        def raise_for_status(self): return None
        def json(self): return {"choices": [{"message": {"content": "x"}}]}
    monkeypatch.setattr(monitor, "LLM_API_KEY", "k")
    monkeypatch.setattr(monitor.requests, "post", lambda url, **k: captured.update(k) or FR())
    monitor.summarize_models([], web_context="- New Thing — https://a.b\n  fresh",
                             recent_names=["Old Model A", "Old Model B"])
    prompt = captured["json"]["messages"][0]["content"]
    assert "FRESH WEB RESEARCH" in prompt and "New Thing" in prompt
    assert "ALREADY COVERED" in prompt and "Old Model A" in prompt
    assert "omit that model entirely" in prompt
    assert "Take line" in prompt


def test_summarize_returns_nothing_when_no_models_and_no_web():
    assert monitor.summarize_models([], web_context="") == "No new models today."


# ── URL hardening: published links must be ones we actually provided ──

def test_collect_provided_urls():
    m = monitor.ModelRelease(name="a/b", provider="p", source="huggingface",
                             url="https://huggingface.co/a/b", description="x",
                             canonical_url="https://vendor.ai/b")
    web = "- Title (2026-06-16) — https://blog.example/post\n  excerpt"
    urls = monitor._collect_provided_urls([m], web)
    assert "https://huggingface.co/a/b" in urls
    assert "https://vendor.ai/b" in urls
    assert "https://blog.example/post" in urls


def test_collect_provided_urls_accepts_either_scheme():
    # Writer may copy http:// even after discovery upgraded the source to
    # https:// (or the reverse). Scheme-only mismatch must not look like a
    # constructed URL and get stripped.
    web = "- Title — http://aireleasetracker.com/\n  excerpt"
    urls = monitor._collect_provided_urls([], web)
    assert "http://aireleasetracker.com" in urls
    assert "https://aireleasetracker.com" in urls


def test_strip_drops_constructed_link_keeps_provided():
    allowed = {"https://blog.example/x2"}
    summary = (
        "<i>take</i>\n\n"
        "━━━ <b>OPEN FRONTIER</b> 🔓\n"
        '<b>Real Model</b> — <i>d</i> 70B. <a href="https://blog.example/x2">→ Source</a>\n'
        '<b>Hallucinated</b> — <i>d</i>. <a href="https://huggingface.co/made/up">→ Source</a>\n')
    cleaned, dropped = monitor._strip_unverified_links(summary, allowed)
    assert dropped == 1
    assert "Real Model" in cleaned
    assert "Hallucinated" not in cleaned and "made/up" not in cleaned


def test_strip_drops_orphaned_tier_header():
    # If the only entry under a tier is dropped, the tier header goes too.
    allowed = {"https://ok.example/a"}
    summary = (
        "━━━ <b>OPEN FRONTIER</b> 🔓\n"
        '<b>Good</b> — <i>d</i>. <a href="https://ok.example/a">→ S</a>\n'
        "━━━ <b>SPECIALIZED</b> 🎯\n"
        '<b>Bad</b> — <i>d</i>. <a href="https://bad.example/z">→ S</a>\n')
    cleaned, dropped = monitor._strip_unverified_links(summary, allowed)
    assert dropped == 1
    assert "OPEN FRONTIER" in cleaned
    assert "SPECIALIZED" not in cleaned  # orphaned header removed


def test_strip_keeps_all_when_all_provided():
    allowed = {"https://a.example/1", "https://b.example/2"}
    summary = ('<b>A</b> — <i>d</i>. <a href="https://a.example/1">→ S</a>\n'
               '<b>B</b> — <i>d</i>. <a href="https://b.example/2/">→ S</a>')
    cleaned, dropped = monitor._strip_unverified_links(summary, allowed)
    assert dropped == 0
    assert "A" in cleaned and "B" in cleaned


def test_strip_keeps_provided_url_with_path_or_query_suffix():
    # Writer often appends /tree/main or ?utm= to a URL we provided. That is
    # not a constructed link — dropping it zeroed otherwise-valid entries.
    allowed = {"https://huggingface.co/acme/Model-1"}
    summary = (
        '<b>Model 1</b> — <i>d</i>. '
        '<a href="https://huggingface.co/acme/Model-1/tree/main">→ S</a>\n'
        '<b>Sibling</b> — <i>d</i>. '
        '<a href="https://huggingface.co/acme/Model-1-instruct">→ S</a>\n'
    )
    cleaned, dropped = monitor._strip_unverified_links(summary, allowed)
    assert dropped == 1
    assert "Model 1" in cleaned
    assert "Sibling" not in cleaned


def test_discovery_emits_model_releases_for_primary_sources(_discovery_on, monkeypatch):
    monkeypatch.setattr(monitor.requests, "post", lambda url, **k: _resp(_PARALLEL_RESP))
    monitor.discover_recent_releases(today="2026-06-16", max_age_days=14)
    names = [m.name for m in monitor.LAST_DISCOVERY_MODELS]
    urls = [m.url for m in monitor.LAST_DISCOVERY_MODELS]
    assert any("X-2" in n or "x2" in n.lower() for n in names)
    assert "https://vendor.ai/blog/x2" in urls
    assert all("old.example" not in (m.url or "") for m in monitor.LAST_DISCOVERY_MODELS)


def test_filter_discovery_drops_aggregators_and_roundups():
    agg = monitor.ModelRelease(
        name="Tracker dump", provider="Web", source="discovery",
        url="https://aireleasetracker.com/model/zai/glm-5.3",
        description="GLM-5.3 by Z.ai", release_date="2026-08-14")
    roundup = monitor.ModelRelease(
        name="AI weekly roundup", provider="Web", source="discovery",
        url="https://vendor.ai/blog/week",
        description="this week's new models", release_date="2026-08-23")
    keeper = monitor.ModelRelease(
        name="acme/X-2", provider="Acme", source="discovery",
        url="https://vendor.ai/blog/x2",
        description="Acme released X-2, a 70B open model.",
        release_date="2026-08-23")
    kept = monitor._filter_discovery_models(
        [agg, roundup, keeper], recent_names=[], seen=set(), today="2026-08-24")
    assert [m.name for m in kept] == ["acme/X-2"]


def test_filter_discovery_drops_already_covered_and_seen():
    vision = monitor.ModelRelease(
        name="deepseek/deepseek-v4-flash-vision-exp", provider="DeepSeek",
        source="discovery",
        url="https://the-decoder.com/deepseek-vision",
        description="DeepSeek V4 Flash Vision Exp is available through the API.",
        release_date="2026-08-24")
    seen_hit = monitor.ModelRelease(
        name="acme/X-2", provider="Acme", source="discovery",
        url="https://vendor.ai/blog/x2",
        description="Acme X-2", release_date="2026-08-23")
    kept = monitor._filter_discovery_models(
        [vision, seen_hit],
        recent_names=["DeepSeek V4 Flash Vision Exp"],
        seen={"acme/X-2"},
        today="2026-08-24")
    assert kept == []

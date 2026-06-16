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


def test_summarize_returns_nothing_when_no_models_and_no_web():
    assert monitor.summarize_models([], web_context="") == "No new models today."

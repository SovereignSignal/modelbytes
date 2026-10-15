"""AlphaSignal coverage cross-check.

The public feed is https://alphasignal.ai/feed.xml (RSS, verified
2026-10-15; atom.xml is the same items). It is a stream of articles, not
one email blob. The daily issue is items from the digest day and the
previous UTC day. Model names in that issue that are absent from the last
14 posted digests are flagged. The check never posts them.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


TODAY = "2026-10-15"

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>Princeton Researchers Fix Costly AI Training Mistakes</title>
    <link>https://alphasignal.ai/news/princeton-learning-rates</link>
    <pubDate>Thu, 15 Oct 2026 00:00:18 GMT</pubDate>
    <description>No model in this one. It must not hide yesterday.</description>
  </item>
  <item>
    <title>HeyGen Voice is #1 and Microsoft-Decision-1 ships</title>
    <link>https://alphasignal.ai/news/heygen-voice</link>
    <pubDate>Wed, 14 Oct 2026 19:01:14 GMT</pubDate>
    <description>Z.ai releases GLM-Legal for contract review. Claude Code now runs nested sessions.</description>
  </item>
  <item>
    <title>Google DeepMind Shrinks Gemma 4 E4B to 5GB</title>
    <link>https://alphasignal.ai/news/gemma-4-e4b</link>
    <pubDate>Tue, 13 Oct 2026 12:00:00 GMT</pubDate>
    <description>Two days old. Not this issue.</description>
  </item>
</channel></rss>
"""


class _Feed:
    def __init__(self, text):
        self.text = text
        self.content = text.encode("utf-8")

    def raise_for_status(self):
        return None


def test_extracts_closed_and_specialist_names_and_skips_products():
    text = (
        "HeyGen Voice is #1 on the voice arena. "
        "Z.ai releases GLM-Legal for contract review. "
        "Microsoft-Decision-1 is now available via API. "
        "Claude Code now runs nested sessions."
    )
    assert monitor.extract_model_mentions(text) == [
        "HeyGen Voice",
        "GLM-Legal",
        "Microsoft-Decision-1",
    ]


def test_extracts_versioned_names_from_article_titles():
    assert monitor.extract_model_mentions(
        "Google DeepMind Shrinks Gemma 4 E4B to 5GB"
    ) == ["Gemma 4 E4B"]
    assert monitor.extract_model_mentions(
        "xAI's Grok Voice 3.0 Tops Speech Benchmark"
    ) == ["Grok Voice 3.0"]
    assert monitor.extract_model_mentions(
        "SoSBench Catches DeepSeek-R1 Leaking Dangerous Science"
    ) == ["DeepSeek-R1"]
    assert monitor.extract_model_mentions(
        "Black Forest Labs Slashes FLUX 3 Video 4K Prices by 50%"
    ) == ["FLUX 3"]


def test_latest_issue_includes_yesterday_and_skips_the_day_before():
    items = monitor.parse_rss_items(FEED, "AlphaSignal")
    latest = monitor.alphasignal_latest_items(items, today=TODAY)
    assert [item["publish_date"] for item in latest] == [
        "2026-10-15", "2026-10-14"]
    assert all("Gemma" not in item["title"] for item in latest)


def test_unposted_names_ignore_older_days_and_already_posted_models():
    posted = "Yesterday we posted <b>Microsoft-Decision-1</b> and Gemma 4 E4B."
    assert monitor.alphasignal_unposted_models(FEED, posted, today=TODAY) == [
        "HeyGen Voice",
        "GLM-Legal",
    ]


def test_unposted_names_on_empty_or_broken_feed():
    assert monitor.alphasignal_unposted_models("", "HeyGen Voice", today=TODAY) == []
    assert monitor.alphasignal_unposted_models("<rss>", "", today=TODAY) == []
    assert monitor.alphasignal_unposted_models(FEED, "", today=TODAY) == [
        "HeyGen Voice",
        "Microsoft-Decision-1",
        "GLM-Legal",
    ]


def test_crosscheck_alerts_outside_preview_and_survives_a_dead_feed(monkeypatch):
    monkeypatch.setattr(
        monitor, "_alphasignal_feed_get", lambda url: _Feed(FEED))
    monkeypatch.setattr(
        monitor, "_digest_history",
        lambda **k: [("2026-10-13", "<b>Microsoft-Decision-1</b>")],
    )
    alerts = []
    monkeypatch.setattr(
        monitor, "send_ops_alert", lambda text: alerts.append(text) or True)

    preview = monitor.run_alphasignal_crosscheck(today=TODAY, preview=True)
    assert preview == ["HeyGen Voice", "GLM-Legal"]
    assert alerts == []

    live = monitor.run_alphasignal_crosscheck(today=TODAY, preview=False)
    assert live == ["HeyGen Voice", "GLM-Legal"]
    assert len(alerts) == 1
    assert "HeyGen Voice" in alerts[0]
    assert "GLM-Legal" in alerts[0]
    assert "Microsoft-Decision-1" not in alerts[0]
    assert "alphasignal.ai/feed.xml" in alerts[0]

    def boom(url):
        raise RuntimeError("feed down")

    monkeypatch.setattr(monitor, "_alphasignal_feed_get", boom)
    assert monitor.run_alphasignal_crosscheck(today=TODAY, preview=False) == []
    assert len(alerts) == 1
    reason = monitor._consume_source_error("AlphaSignal")
    assert "feed down" in reason

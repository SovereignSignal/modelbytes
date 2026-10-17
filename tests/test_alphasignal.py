"""AlphaSignal coverage cross-check.

The public feed is https://alphasignal.ai/feed.xml (RSS, verified
2026-10-15; atom.xml is the same items). It is a stream of articles, not
one email blob. The daily issue is items from the digest day and the
previous UTC day. Model names in those item titles that are absent from
every posted digest (and today's delivered one) are flagged. The check
never posts them.
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
    <description>Kimi K9 is mentioned only here. Claude Code now runs nested sessions.</description>
  </item>
  <item>
    <title>Z.ai releases GLM-Legal for contract review</title>
    <link>https://alphasignal.ai/news/glm-legal</link>
    <pubDate>Wed, 14 Oct 2026 18:00:00 GMT</pubDate>
    <description>Contract review model.</description>
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
        "2026-10-15", "2026-10-14", "2026-10-14"]
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


def _item(title, excerpt, day="Thu, 15 Oct 2026 09:00:00 GMT"):
    return (f"<item><title>{title}</title>"
            f"<link>https://alphasignal.ai/news/x</link>"
            f"<pubDate>{day}</pubDate>"
            f"<description>{excerpt}</description></item>")


BENCH_FEED = (
    '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
    + _item("SoSBench Catches DeepSeek-R1 Leaking Dangerous Science",
            "A safety benchmark finds DeepSeek-R1 answers hazardous prompts.")
    + _item("AA-Robotics Index: GPT-6 Astra leads",
            "Artificial Analysis ranks robotics models; Gemini 4 Pro is second.")
    + _item("Jais 3 matches Claude Sonnet 5 on Arabic benchmarks",
            "The open Arabic model rivals Claude Sonnet 5 and GPT-6 Astra.")
    + _item("xAI&#39;s Grok Voice 3.0 Tops Speech Benchmark",
            "Grok Voice 3.0 debuts at the top of the Artificial Analysis "
            "speech-to-speech index.")
    + _item("NVIDIA releases Nemotron 3.5 Super VL",
            "An open vision-language model; it beats Qwen 4 VL on charts.")
    + "</channel></rss>"
)

BENCH_HISTORY = [
    ("2026-10-14", "<b>Nemotron 3.5 Bolt</b> from NVIDIA"),
    ("2026-07-02", "<b>GPT-6 Astra</b> robotics model"),
    ("2025-01-01", "<b>Sonnet 5.5</b> follows Sonnet 5"),
]


def test_variant_words_are_part_of_model_names():
    assert monitor.extract_model_mentions(
        "NVIDIA releases Nemotron 3.5 Super VL"
    ) == ["Nemotron 3.5 Super VL"]
    assert monitor.extract_model_mentions("Qwen 4 Ultra Nano") == [
        "Qwen 4 Ultra Nano"]


def test_benchmark_titles_and_excerpt_names_do_not_false_alarm(monkeypatch):
    monkeypatch.setattr(
        monitor, "_alphasignal_feed_get", lambda url: _Feed(BENCH_FEED))
    windows = []

    def history(**k):
        windows.append(k.get("days"))
        return list(BENCH_HISTORY)

    monkeypatch.setattr(monitor, "_digest_history", history)
    flagged = monitor.run_alphasignal_crosscheck(today=TODAY, preview=True)
    assert flagged == ["Grok Voice 3.0", "Nemotron 3.5 Super VL"]
    # Full history, not the 14-day window.
    assert windows == [None]


DERIVATIVE_FEED = (
    '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
    + _item("Unsloth&apos;s Llama 3.2 Mirror Hits 800K Downloads by "
            "Removing Meta&apos;s Login Wall",
            "A re-upload of Meta's 2024 model without the license gate.")
    + _item("Unsloth Releases Qwen 4 GGUF Quants for Laptops",
            "Quantized builds of Alibaba's model.")
    + _item("Community Fork of DeepSeek-R1 Ported to Apple Silicon",
            "A fine-tuned port.")
    + _item("Meta Releases Llama 5", "Meta's new open-weight model.")
    + _item("Google Launches Gemini 5 Pro", "A new flagship model.")
    + _item("Anthropic&apos;s Claude Sonnet 6 Cuts Quantum Circuit Gates by 17x",
            "No release word, but about the model itself.")
    + "</channel></rss>"
)


def test_mirror_and_derivative_titles_do_not_false_alarm():
    flagged = monitor.alphasignal_unposted_models(
        DERIVATIVE_FEED, "", today=TODAY)
    assert flagged == ["Llama 5", "Gemini 5 Pro", "Claude Sonnet 6"]


def test_llama_3_2_mirror_title_from_2026_10_17_is_not_a_gap():
    feed = (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        + _item("Unsloth&apos;s Llama 3.2 Mirror Hits 800K Downloads by "
                "Removing Meta&apos;s Login Wall",
                "Unsloth's copy of Llama 3.2.",
                day="Sat, 17 Oct 2026 09:00:00 GMT")
        + "</channel></rss>"
    )
    assert monitor.alphasignal_unposted_models(
        feed, "", today="2026-10-17") == []


def test_crosscheck_counts_the_digest_delivered_this_run(monkeypatch):
    monkeypatch.setattr(
        monitor, "_alphasignal_feed_get", lambda url: _Feed(BENCH_FEED))
    monkeypatch.setattr(
        monitor, "_digest_history", lambda **k: list(BENCH_HISTORY))
    flagged = monitor.run_alphasignal_crosscheck(
        today=TODAY, preview=True,
        delivered="<b>Nemotron 3.5 Super VL</b> is out today")
    assert flagged == ["Grok Voice 3.0"]


def _run_main(monkeypatch, body_sent):
    calls = []

    def fake_run(today, preview_mode):
        calls.append(("run", monitor.LAST_DELIVERED_DIGEST))
        if body_sent:
            monkeypatch.setattr(
                monitor._publisher, "send_telegram",
                lambda message: type("R", (), {
                    "ok": True, "message_id": 7, "error": ""})())
            assert monitor.send_telegram_post(body_sent)
        return 0

    def fake_check(today, preview=False, delivered=None):
        calls.append(("check", delivered))
        return []

    monkeypatch.setattr(monitor, "_run_digest", fake_run)
    monkeypatch.setattr(monitor, "run_alphasignal_crosscheck", fake_check)
    monkeypatch.setattr(monitor.sys, "argv", ["monitor.py"])
    monitor.LAST_DELIVERED_DIGEST = "stale body from an earlier run"
    assert monitor.main() == 0
    return calls


def test_main_runs_crosscheck_after_a_send_with_the_delivered_body(monkeypatch):
    calls = _run_main(monkeypatch, "<b>Nemotron 3.5 Super VL</b>")
    assert calls == [("run", None), ("check", "<b>Nemotron 3.5 Super VL</b>")]


def test_main_runs_crosscheck_on_history_only_without_a_send(monkeypatch):
    calls = _run_main(monkeypatch, None)
    assert calls == [("run", None), ("check", None)]

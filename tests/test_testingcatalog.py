"""TestingCatalog RSS — early signal for model releases.

Leaks and pre-release items are dropped, not published as released.
A vendor, Hugging Face, or OpenRouter link wins over the TestingCatalog permalink.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


TODAY = "2026-10-07"

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>TestingCatalog AI News</title>
    <item>
      <title>Mistral launches Large 4 preview with 1 T parameters</title>
      <link>https://www.testingcatalog.com/mistral-launches-large-4-preview-with-1-t-parameters/</link>
      <pubDate>Tue, 06 Oct 2026 14:03:33 +0000</pubDate>
      <category>Mistral</category>
      <description>Mistral Large 4 brings multimodal reasoning to a public API. Card: https://huggingface.co/mistralai/Mistral-Large-4</description>
    </item>
    <item>
      <title>Google prepares Antigravity for Gemini 4 Argon and Concierge</title>
      <link>https://www.testingcatalog.com/google-prepares-antigravity-for-gemini-4-argon-and-concierge/</link>
      <pubDate>Wed, 07 Oct 2026 14:04:39 +0000</pubDate>
      <category>Google</category>
      <description>New Antigravity labels point to Argon rollout preparations.</description>
    </item>
    <item>
      <title>Figure plans to release Hark AI assistant this week</title>
      <link>https://www.testingcatalog.com/figure-plans-to-release-hark-ai-assistant-this-week/</link>
      <pubDate>Mon, 05 Oct 2026 14:12:06 +0000</pubDate>
      <category>AI Rumours</category>
      <description>Hark is still in development.</description>
    </item>
    <item>
      <title>Maket 2.0 brings AI floor plans and 3D home renders</title>
      <link>https://www.testingcatalog.com/maket-2-0/</link>
      <pubDate>Wed, 07 Oct 2026 12:09:17 +0000</pubDate>
      <category>Sponsored</category>
      <description>Sponsored product update.</description>
    </item>
    <item>
      <title>Claude adds editing in Google Docs, Sheets, and Slides</title>
      <link>https://www.testingcatalog.com/claude-adds-editing-in-google-docs-sheets-and-slides/</link>
      <pubDate>Wed, 07 Oct 2026 12:15:43 +0000</pubDate>
      <category>Anthropic</category>
      <description>Anthropic brings Claude into Google Workspace.</description>
    </item>
    <item>
      <title>Aleph Alpha releases open-weight Kolibri with 1M context</title>
      <link>https://www.testingcatalog.com/aleph-alpha-releases-open-weight-kolibri-with-1m-context/</link>
      <pubDate>Wed, 07 Oct 2026 09:10:00 +0000</pubDate>
      <category>AI Announcements</category>
      <description>Aleph Alpha's bilingual Kolibri model offers Apache 2.0 weights.</description>
    </item>
    <item>
      <title>Google unveils Gemini 4 Argon with SOTA score on DeepSWE</title>
      <link>https://www.testingcatalog.com/google-unveils-gemini-4-argon/</link>
      <pubDate>Wed, 30 Sep 2026 20:33:39 +0000</pubDate>
      <category>Google</category>
      <description>Google announces Gemini 4 Argon.</description>
    </item>
    <item>
      <title>OpenAI launches Dots agents powered by GPT-6 Astra</title>
      <link>https://www.testingcatalog.com/openai-launches-dots-agents/</link>
      <pubDate>Wed, 07 Oct 2026 13:33:21 +0000</pubDate>
      <category>OpenAI</category>
      <description>OpenAI introduces Dots, agents with cloud computers.</description>
    </item>
  </channel>
</rss>
"""


class _Feed:
    def __init__(self, text):
        self.text = text
        self.content = text.encode("utf-8")

    def raise_for_status(self):
        return None


def _names(models):
    return [m.name for m in models]


def test_testingcatalog_keeps_releases_and_skips_leaks(monkeypatch):
    monkeypatch.setattr(
        monitor, "_testingcatalog_get", lambda url: _Feed(FEED))
    models = monitor.fetch_testingcatalog_models(today=TODAY)
    assert _names(models) == ["Mistral Large 4", "Aleph Alpha Kolibri"]
    mistral, kolibri = models
    assert mistral.release_date == "2026-10-06"
    assert mistral.url == "https://huggingface.co/mistralai/Mistral-Large-4"
    assert "testingcatalog.com" not in mistral.url
    assert kolibri.url.startswith("https://www.testingcatalog.com/")
    assert kolibri.release_date == "2026-10-07"
    assert kolibri.source == "testingcatalog"
    blob = " ".join(_names(models)).lower()
    assert "prepares" not in blob
    assert "plans to" not in blob
    assert "docs" not in blob
    assert "dots" not in blob
    assert "launches" not in blob
    assert "releases" not in blob
    assert "gemini 4" not in blob  # Sep 30 is outside the 3-day window
    rendered = monitor.render_model_entry(kolibri, kolibri.description)
    assert rendered == (
        "<b>Aleph Alpha Kolibri</b> — "
        "<i>Aleph Alpha's bilingual Kolibri model offers Apache 2.0 weights.</i> "
        "Released Oct 7. "
        '<a href="https://www.testingcatalog.com/aleph-alpha-releases-open-weight-kolibri-with-1m-context/">'
        "→ testingcatalog.com</a>"
    )
    assert "Cited source" not in rendered
    assert "prepares" not in rendered


def test_testingcatalog_prefers_an_existing_primary_source(monkeypatch):
    monkeypatch.setattr(
        monitor, "_testingcatalog_get", lambda url: _Feed(FEED))
    existing = monitor.ModelRelease(
        name="mistralai/Mistral-Large-4",
        provider="Mistral",
        source="huggingface",
        url="https://huggingface.co/mistralai/Mistral-Large-4",
        description="Frontier multimodal model.",
        release_date="2026-10-06",
    )
    models = monitor.fetch_testingcatalog_models(existing=[existing], today=TODAY)
    assert all("Mistral" not in m.name for m in models)
    assert existing.url == "https://huggingface.co/mistralai/Mistral-Large-4"
    assert "Kolibri" in " ".join(_names(models))


def test_testingcatalog_namespaced_feed_still_filters(monkeypatch):
    # The live feed declares dc: and media: on the root. Category has to be
    # read from that tree; re-serializing one item drops the xmlns.
    namespaced = """<?xml version="1.0" encoding="UTF-8"?>
    <rss xmlns:dc="http://purl.org/dc/elements/1.1/"
         xmlns:media="http://search.yahoo.com/mrss/" version="2.0">
      <channel>
        <item>
          <title>Google prepares Antigravity for Gemini 4 Argon</title>
          <link>https://www.testingcatalog.com/google-prepares-antigravity/</link>
          <pubDate>Wed, 07 Oct 2026 14:04:39 +0000</pubDate>
          <category>Google</category>
          <dc:creator>Alexey</dc:creator>
          <description>Rollout preparations.</description>
          <media:content url="https://example.com/a.png" medium="image"/>
        </item>
        <item>
          <title>Mistral launches Large 4 with 1 T parameters</title>
          <link>https://www.testingcatalog.com/mistral-large-4/</link>
          <pubDate>Tue, 06 Oct 2026 14:03:33 +0000</pubDate>
          <category>Mistral</category>
          <description>Public API.</description>
        </item>
      </channel>
    </rss>
    """
    monkeypatch.setattr(monitor, "_testingcatalog_get", lambda url: _Feed(namespaced))
    models = monitor.fetch_testingcatalog_models(today=TODAY)
    assert _names(models) == ["Mistral Large 4"]


def test_testingcatalog_skips_a_release_it_cannot_name(monkeypatch):
    vague = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Acme releases update with new weights</title>
        <link>https://www.testingcatalog.com/acme-update/</link>
        <pubDate>Wed, 07 Oct 2026 12:00:00 +0000</pubDate>
        <category>Acme</category>
        <description>An update with new weights.</description>
      </item>
    </channel></rss>
    """
    monkeypatch.setattr(monitor, "_testingcatalog_get", lambda url: _Feed(vague))
    assert monitor.fetch_testingcatalog_models(today=TODAY) == []


def test_testingcatalog_failure_is_empty_and_recorded(monkeypatch):
    def boom(url):
        raise RuntimeError("catalog down")

    monkeypatch.setattr(monitor, "_testingcatalog_get", boom)
    assert monitor.fetch_testingcatalog_models(today=TODAY) == []
    reason = monitor._consume_source_error("TestingCatalog")
    assert "catalog down" in reason

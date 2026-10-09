"""Coverage gaps from the 2026-10-04 audit.

Three leaks, each with a regression test:

1. Hugging Face repo createdAt is not the launch. Kumo Tabular (repo created
   Sep 1, public release Sep 28) and AstaBrief 8B (repo created Feb 9, weights
   Oct 2) were dropped as stale back-catalog the first time they were seen.
2. Mid-tier open-weight models outside KNOWN_ORGS never became candidates
   (Naive-N0.5-Flash). pplx-decider was a candidate the writer then omitted
   with no log line.
3. Parallel search slots returned listicle pages. Lab RSS and news pages
   replace those slots.
"""
import gzip
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


TODAY = "2026-10-04"


class _Resp:
    def __init__(self, payload=None, text=""):
        self._payload = payload if payload is not None else {}
        self.text = text
        self.content = text.encode("utf-8") if text else b""

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _model(name, release_date, signal, created, source="huggingface-org"):
    return monitor.ModelRelease(
        name=name,
        provider="Test",
        source=source,
        url=f"https://huggingface.co/{name}",
        description="x",
        release_date=release_date,
        recency_signal=signal,
        repo_created_at=created,
        likes=100,
        downloads=1000,
    )


# ── 1. Recency signal, not repo createdAt ──────────────────────────────


def test_kumo_uses_release_commit_not_repo_created_at():
    # nvidia/Kumo-Tabular: createdAt 2026-09-01, public commit "Release Kumo
    # Tabular" 2026-09-28, later README edits 2026-10-02. Audit dropped it as
    # stale because the fetcher stored createdAt.
    payload = {
        "id": "nvidia/Kumo-Tabular",
        "createdAt": "2026-09-01T07:00:05.000Z",
        "lastModified": "2026-10-02T07:37:36.000Z",
    }
    commits = [
        {"date": "2026-10-02T07:37:36.000Z", "title": "Update README.md"},
        {"date": "2026-10-02T07:34:43.000Z", "title": "Document FP16 inference"},
        {"date": "2026-09-28T00:00:00.000Z", "title": "Release Kumo Tabular"},
    ]
    date, signal, created = monitor.hf_model_release_date(
        payload, today=TODAY, commits=commits)
    assert date == "2026-09-28"
    assert signal == "first_public_release"
    assert created == "2026-09-01"
    assert monitor.is_stale_release(date, today=TODAY) is False


def test_astabrief_weights_cluster_survives_old_repo(capsys):
    # allenai/AstaBrief_8B: repo created 2026-02-09, safetensors landed 2026-10-02.
    payload = {
        "createdAt": "2026-02-09T17:13:01.000Z",
        "lastModified": "2026-10-03T18:55:16.000Z",
    }
    commits = [
        {"date": "2026-10-03T18:55:16.000Z", "title": "Align the inference example"},
        {"date": "2026-10-02T22:46:53.000Z", "title": "Adding safetensors variant of this model"},
        {"date": "2026-10-02T04:21:41.000Z", "title": "Update README.md"},
        {"date": "2026-02-09T17:13:01.000Z", "title": "initial commit"},
    ]
    date, signal, created = monitor.hf_model_release_date(
        payload, today=TODAY, commits=commits)
    assert date == "2026-10-02"
    assert signal == "first_public_release"
    assert created == "2026-02-09"
    model = _model("allenai/AstaBrief_8B", date, signal, created)
    assert monitor.stale_drop_reason(model, today=TODAY) is None
    kept = monitor.drop_stale_models([model], set(), today=TODAY)
    assert [m.name for m in kept] == ["allenai/AstaBrief_8B"]
    assert "AstaBrief_8B" not in capsys.readouterr().err


def test_last_modified_keeps_first_seen_model_when_repo_is_older():
    # No commit list yet (the common fetcher path). lastModified is the
    # relevant signal. An old createdAt must not drop a first sighting.
    payload = {
        "createdAt": "2026-09-01T07:00:05.000Z",
        "lastModified": "2026-10-02T07:37:36.000Z",
    }
    date, signal, created = monitor.hf_model_release_date(payload, today=TODAY)
    assert (date, signal, created) == ("2026-10-02", "lastModified", "2026-09-01")
    model = _model("nvidia/Kumo-Tabular", date, signal, created)
    assert monitor.stale_drop_reason(model, today=TODAY) is None


def test_readme_only_bump_is_not_a_new_release():
    payload = {
        "createdAt": "2024-03-01T00:00:00.000Z",
        "lastModified": "2026-10-03T00:00:00.000Z",
    }
    commits = [
        {"date": "2026-10-03T00:00:00.000Z", "title": "Update README.md"},
        {"date": "2024-03-01T00:00:00.000Z", "title": "initial commit"},
    ]
    date, signal, _created = monitor.hf_model_release_date(
        payload, today=TODAY, commits=commits)
    assert date == "2024-03-01"
    assert signal == "createdAt"
    assert monitor.is_stale_release(date, today=TODAY) is True


def test_back_catalog_drop_logs_the_signal(capsys):
    model = _model(
        "moonshotai/Kimi-VL", "2025-04-09", "createdAt", "2025-04-09")
    reason = monitor.stale_drop_reason(model, today="2026-06-11")
    assert reason is not None
    assert "createdAt" in reason
    assert "2025-04-09" in reason
    assert "lastModified" in reason
    seen = set()
    kept = monitor.drop_stale_models([model], seen, today="2026-06-11")
    assert kept == []
    assert "moonshotai/Kimi-VL" in seen
    err = capsys.readouterr().err
    assert "dropped candidate kind=filter name=moonshotai/Kimi-VL" in err
    assert "stale" in err
    assert "Dropping 1 stale back-catalog" in err


def test_org_fetch_stores_last_modified_for_kumo(monkeypatch):
    payload = [{
        "id": "nvidia/Kumo-Tabular",
        "author": "nvidia",
        "createdAt": "2026-09-01T07:00:05.000Z",
        "lastModified": "2026-10-02T07:37:36.000Z",
        "downloads": 0,
        "likes": 106,
        "tags": ["tabular-foundation-model"],
        "pipeline_tag": None,
    }]
    monkeypatch.setattr(monitor, "_http_get", lambda *a, **k: _Resp(payload))
    models = monitor.fetch_org_models("nvidia")
    assert len(models) == 1
    assert models[0].release_date == "2026-10-02"
    assert models[0].recency_signal == "lastModified"
    assert models[0].repo_created_at == "2026-09-01"
    assert monitor.is_stale_release(models[0].release_date, today=TODAY) is False


def test_refine_uses_commit_cluster_before_the_stale_drop(monkeypatch):
    model = _model("nvidia/Kumo-Tabular", "2026-10-02", "lastModified", "2026-09-01")
    commits = [
        {"date": "2026-10-02T07:37:36.000Z", "title": "Update README.md"},
        {"date": "2026-09-28T00:00:00.000Z", "title": "Release Kumo Tabular"},
    ]
    monkeypatch.setattr(monitor, "_fetch_hf_commits", lambda model_id: commits)
    refined = monitor.refine_stale_repo_dates([model], today=TODAY)
    assert refined[0].release_date == "2026-09-28"
    assert refined[0].recency_signal == "first_public_release"
    assert monitor.stale_drop_reason(refined[0], today=TODAY) is None


# ── 2. New-on-HF pass, not limited to known orgs ───────────────────────


def test_unknown_org_cliff_still_applies_outside_the_new_pass():
    # The existing noise filter must keep rejecting modest unknown orgs.
    # The new pass opts out of that cliff and applies its own floor.
    assert monitor.is_noise_model(
        "NaiveAI/Naive-N0.5-Flash", "NaiveAI", ["text-generation"],
        downloads=1920, likes=165) is True
    assert monitor.is_noise_model(
        "NaiveAI/Naive-N0.5-Flash", "NaiveAI", ["text-generation"],
        downloads=1920, likes=165, engagement_floor=False) is False


def test_new_hf_pass_finds_naive_and_pplx_and_logs_rejections(monkeypatch, capsys):
    rows = [
        {
            "id": "NaiveAI/Naive-N0.5-Flash",
            "author": "NaiveAI",
            "likes": 165,
            "downloads": 1920,
            "createdAt": "2026-09-27T14:14:32.000Z",
            "pipeline_tag": "text-generation",
            "tags": ["text-generation"],
        },
        {
            "id": "perplexity-ai/pplx-decider-v1-27b",
            "author": "perplexity-ai",
            "likes": 75,
            "downloads": 793,
            "createdAt": "2026-10-01T21:24:11.000Z",
            "pipeline_tag": "text-classification",
            "tags": ["text-classification"],
        },
        {
            "id": "newlab/Late-Launch-8B",
            "author": "newlab",
            "likes": 80,
            "downloads": 600,
            "createdAt": "2026-02-09T00:00:00.000Z",
            "pipeline_tag": "text-generation",
            "tags": ["text-generation"],
        },
        {
            "id": "spam/tiny-dump",
            "author": "spam",
            "likes": 0,
            "downloads": 0,
            "createdAt": "2026-10-04T00:00:00.000Z",
            "pipeline_tag": "text-generation",
            "tags": ["text-generation"],
        },
        {
            "id": "someone/Cool-Model-GGUF",
            "author": "someone",
            "likes": 400,
            "downloads": 9000,
            "createdAt": "2026-10-01T00:00:00.000Z",
            "pipeline_tag": "text-generation",
            "tags": ["text-generation"],
        },
        {
            "id": "nocard/Big-Model",
            "author": "nocard",
            "likes": 200,
            "downloads": 1000,
            "createdAt": "2026-10-01T00:00:00.000Z",
            "pipeline_tag": "text-generation",
            "tags": ["text-generation"],
        },
        {
            "id": "backcatalog/Quiet-7B",
            "author": "backcatalog",
            "likes": 200,
            "downloads": 10000,
            "createdAt": "2024-01-01T00:00:00.000Z",
            "pipeline_tag": "text-generation",
            "tags": ["text-generation"],
        },
    ]
    details = {
        "NaiveAI/Naive-N0.5-Flash": {
            **rows[0],
            "lastModified": "2026-09-27T19:13:58.000Z",
            "cardData": {"license": "mit"},
        },
        "perplexity-ai/pplx-decider-v1-27b": {
            **rows[1],
            "lastModified": "2026-10-01T21:24:15.000Z",
            "cardData": {"license": "apache-2.0"},
        },
        "newlab/Late-Launch-8B": {
            **rows[2],
            "lastModified": "2026-10-03T00:00:00.000Z",
            "cardData": {"license": "apache-2.0"},
        },
        "nocard/Big-Model": {
            **rows[5],
            "lastModified": "2026-10-01T00:00:00.000Z",
            "cardData": {},
        },
        "backcatalog/Quiet-7B": {
            **rows[6],
            "lastModified": "2024-01-02T00:00:00.000Z",
            "cardData": {"license": "mit"},
        },
    }

    def http_get(url, source_name, **kwargs):
        if "sort=trendingScore" in url:
            return _Resp(rows)
        if url.rstrip("/").endswith("/api/trending"):
            return _Resp({"recentlyTrending": []})
        if "/api/models/" in url and "/commits/" not in url:
            model_id = url.split("/api/models/", 1)[1].split("?", 1)[0]
            return _Resp(details[model_id])
        raise AssertionError(url)

    monkeypatch.setattr(monitor, "_http_get", http_get)
    found = {m.name: m for m in monitor.fetch_hf_new_models(today=TODAY)}
    assert "NaiveAI/Naive-N0.5-Flash" in found
    assert "perplexity-ai/pplx-decider-v1-27b" in found
    assert found["newlab/Late-Launch-8B"].release_date == "2026-10-03"
    assert found["newlab/Late-Launch-8B"].recency_signal == "lastModified"
    assert "spam/tiny-dump" not in found
    assert "someone/Cool-Model-GGUF" not in found
    assert "nocard/Big-Model" not in found
    assert "backcatalog/Quiet-7B" not in found
    err = capsys.readouterr().err
    assert "dropped candidate kind=filter name=someone/Cool-Model-GGUF" in err
    assert "dropped candidate kind=filter name=nocard/Big-Model" in err
    assert "model card" in err
    assert "dropped candidate kind=filter name=backcatalog/Quiet-7B" in err
    assert "stale" in err
    assert "spam/tiny-dump" not in err


def test_writer_exclusion_logs_omitted_candidate(capsys):
    naive = monitor.ModelRelease(
        name="NaiveAI/Naive-N0.5-Flash", provider="NaiveAI",
        source="huggingface-new",
        url="https://huggingface.co/NaiveAI/Naive-N0.5-Flash",
        description="309B MoE", release_date="2026-09-27")
    decider = monitor.ModelRelease(
        name="perplexity-ai/pplx-decider-v1-27b", provider="Perplexity",
        source="huggingface-org",
        url="https://huggingface.co/perplexity-ai/pplx-decider-v1-27b",
        description="decider", release_date="2026-10-01")
    summary = (
        "<b>Naive N0.5 Flash</b> — <i>open weights</i> "
        '<a href="https://huggingface.co/NaiveAI/Naive-N0.5-Flash">→ Source</a>'
    )
    omitted = monitor.log_writer_exclusions([naive, decider], summary)
    assert omitted == ["perplexity-ai/pplx-decider-v1-27b"]
    err = capsys.readouterr().err
    assert (
        "dropped candidate kind=writer_exclusion "
        "name=perplexity-ai/pplx-decider-v1-27b" in err
    )
    assert "omitted by writer" in err
    assert "Naive-N0.5-Flash" not in err.split("writer_exclusion")[-1]


# ── 3. Lab feeds replace listicle web-search slots ─────────────────────


_DEEPMIND_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>DeepMind</title>
    <item>
      <title>Gemini 4 Argon: our next era of frontier intelligence</title>
      <link>https://deepmind.google/blog/gemini-4-argon-our-next-era-of-frontier-intelligence/</link>
      <pubDate>Wed, 30 Sep 2026 20:01:45 +0000</pubDate>
      <description>Gemini 4 Argon is our next frontier model.</description>
    </item>
    <item>
      <title>Best AI Models in October 2026</title>
      <link>https://deepmind.google/blog/best-ai-models/</link>
      <pubDate>Thu, 01 Oct 2026 00:00:00 +0000</pubDate>
      <description>A roundup.</description>
    </item>
  </channel>
</rss>
"""

_OPENAI_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title><![CDATA[Introducing dots]]></title>
      <link>https://openai.com/index/introducing-dots/</link>
      <pubDate>Tue, 29 Sep 2026 00:00:00 GMT</pubDate>
      <description><![CDATA[A new OpenAI agent.]]></description>
    </item>
  </channel>
</rss>
"""


def test_listicle_search_slots_are_gone_and_lab_feeds_are_configured():
    blob = "\n".join(monitor._discovery_search_queries("October 2026")).lower()
    for phrase in (
        "new ai model release",
        "new open-weight",
        "new coding model",
        "new multimodal",
        "hugging face newly released",
        "best ai models",
    ):
        assert phrase not in blob
    labs = " ".join(feed["lab"].lower() for feed in monitor.LAB_NEWS_FEEDS)
    for name in (
        "openai", "anthropic", "deepmind", "meta", "mistral",
        "deepseek", "qwen", "kimi", "zhipu", "xai", "reflection",
    ):
        assert name in labs, name


def test_lab_parsers_read_rss_sitemap_and_news_pages():
    rss_items = monitor.parse_rss_items(_DEEPMIND_RSS, "Google DeepMind")
    assert rss_items[0]["title"].startswith("Gemini 4 Argon")
    assert rss_items[0]["publish_date"] == "2026-09-30"
    assert rss_items[0]["url"].startswith("https://deepmind.google/")

    sitemap = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset>
      <url><loc>https://www.anthropic.com/news/claude-frontier-academy</loc>
           <lastmod>2026-10-02T17:15:18.000Z</lastmod></url>
      <url><loc>https://www.anthropic.com/about</loc>
           <lastmod>2026-10-02T00:00:00.000Z</lastmod></url>
      <url><loc>https://api-docs.deepseek.com/news/news260910</loc>
           <lastmod>2026-10-04T00:00:00.000Z</lastmod></url>
    </urlset>
    """
    anthropic = monitor.parse_sitemap_items(
        sitemap, "Anthropic", path_contains="/news/")
    assert [i["url"] for i in anthropic] == [
        "https://www.anthropic.com/news/claude-frontier-academy",
        "https://api-docs.deepseek.com/news/news260910",
    ]
    assert anthropic[0]["publish_date"] == "2026-10-02"
    deepseek = monitor.parse_sitemap_items(
        sitemap, "DeepSeek", path_contains="/news/news", slug_date=True)
    assert deepseek[0]["publish_date"] == "2026-09-10"
    assert deepseek[0]["url"].endswith("/news/news260910")

    xai = """
    <a href="/news/grok-4-7">Grok</a>
    <div><time dateTime="2026-09-21">Sep 21, 2026</time>
    <h3>Grok 4.7</h3></div>
    """
    xai_items = monitor.parse_html_news_items(xai, "xAI", "html_xai")
    assert xai_items[0]["url"] == "https://x.ai/news/grok-4-7"
    assert xai_items[0]["publish_date"] == "2026-09-21"
    assert "Grok 4.7" in xai_items[0]["title"]

    meta = (
        '<a href="https://ai.meta.com/blog/introducing-muse-spark-meta-model-api/">'
        "Introducing Muse Spark 1.1 </a>"
        '<div class="_amun">July 9, 2026</div>'
    )
    meta_items = monitor.parse_html_news_items(meta, "Meta AI", "html_meta")
    assert meta_items[0]["publish_date"] == "2026-07-09"
    assert "Muse Spark" in meta_items[0]["title"]

    kimi = (
        '<a href="/blog/kimi-k3" aria-label="Kimi K3" class="card">'
        "</a><img src=\"https://cdn.example/2026-07-31/pic.jpg\">"
    )
    kimi_items = monitor.parse_html_news_items(kimi, "Moonshot/Kimi", "html_kimi")
    assert kimi_items[0]["url"] == "https://www.kimi.ai/blog/kimi-k3"
    assert kimi_items[0]["publish_date"] == "2026-07-31"
    assert kimi_items[0]["title"] == "Kimi K3"

    zhipu = (
        '<a href="/en/news/152">GLM-5.2 is out</a>'
        "<span>2026-08-14</span>"
    )
    zhipu_items = monitor.parse_html_news_items(zhipu, "Zhipu/GLM", "html_zhipu")
    assert zhipu_items[0]["url"] == "https://www.zhipuai.cn/en/news/152"
    assert zhipu_items[0]["publish_date"] == "2026-08-14"


def test_gzip_feed_body_decodes():
    raw = gzip.compress(b"<?xml version='1.0'?><rss><channel></channel></rss>")
    assert raw[:2] == b"\x1f\x8b"
    text = monitor._feed_bytes_to_text(raw)
    assert text.startswith("<?xml")
    assert "channel" in text


def test_discover_uses_lab_feeds_and_does_not_post_listicle_search(monkeypatch):
    posted = []

    def post(url, **kwargs):
        posted.append(kwargs.get("json") or {})
        raise AssertionError("listicle web search should not run")

    def lab_get(url, source_name):
        if "deepmind.google" in url:
            return _Resp(text=_DEEPMIND_RSS)
        if "openai.com/news/rss" in url:
            return _Resp(text=_OPENAI_RSS)
        return _Resp(text="")

    monkeypatch.setattr(monitor, "DISCOVERY_ENABLED", True)
    monkeypatch.setattr(monitor, "PARALLEL_API_KEY", "pk-test")
    monkeypatch.setattr(monitor.requests, "post", post)
    monkeypatch.setattr(monitor, "_lab_feed_get", lab_get)
    block = monitor.discover_recent_releases(today=TODAY, max_age_days=14)
    assert "Gemini 4 Argon" in block
    assert "https://deepmind.google/blog/gemini-4-argon" in block
    assert "Introducing dots" in block
    assert "https://openai.com/index/introducing-dots/" in block
    assert "Best AI Models" not in block
    assert posted == []
    urls = [m.url for m in monitor.LAST_DISCOVERY_MODELS]
    assert any("gemini-4-argon" in u for u in urls)


def test_lab_feeds_run_without_a_parallel_key(monkeypatch):
    monkeypatch.setattr(monitor, "DISCOVERY_ENABLED", False)
    monkeypatch.setattr(monitor, "PARALLEL_API_KEY", "")

    def lab_get(url, source_name):
        if "deepmind.google" in url:
            return _Resp(text=_DEEPMIND_RSS)
        return _Resp(text="")

    monkeypatch.setattr(monitor, "_lab_feed_get", lab_get)
    block = monitor.discover_recent_releases(today=TODAY, max_age_days=14)
    assert "Gemini 4 Argon" in block
    assert monitor._DISCOVERY_DISABLED is True


def test_one_lab_feed_failure_keeps_the_others(monkeypatch):
    def lab_get(url, source_name):
        if "openai.com" in url:
            raise RuntimeError("openai down")
        if "deepmind.google" in url:
            return _Resp(text=_DEEPMIND_RSS)
        return _Resp(text="")

    monkeypatch.setattr(monitor, "DISCOVERY_ENABLED", False)
    monkeypatch.setattr(monitor, "PARALLEL_API_KEY", "")
    monkeypatch.setattr(monitor, "_lab_feed_get", lab_get)
    block = monitor.discover_recent_releases(today=TODAY, max_age_days=14)
    assert "Gemini 4 Argon" in block
    assert "Introducing dots" not in block

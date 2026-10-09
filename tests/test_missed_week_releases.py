"""Two models the Oct 5–8 digests missed, and the paths that should have them.

Reflection Beam (announced 2026-10-05) was not a Hugging Face miss. A Hub
search on Oct 6–9 returned no repo: the weights were promised for later in
October. The blog post is https://reflection.ai/blog/introducing-beam and
that host is not in LAB_NEWS_FEEDS, so the announcement never became a
candidate. There is no size cap that would have dropped a 501B model.

Grok Imagine Video 1.5 Lite is a closed video model. It is absent from
OpenRouter's default /models catalog (text outputs only) and from Hugging
Face. The video catalog lists it with created 2026-10-06. Artificial
Analysis's public pages forbid automated scraping; the free Data API is the
allowed AA path and stays off until MODELBYTES_ARTIFICIAL_ANALYSIS_API_KEY
is set. A media row whose ship date is older than the digest window is still
a leaderboard debut once a baseline exists.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


OCT6 = "2026-10-06"
OCT9 = "2026-10-09"
GROK_CREATED = 1791309778  # 2026-10-06T18:02:58Z

REFLECTION_INDEX = """
<html><body>
<a aria-label="Read more: Introducing Beam: Reflection’s 501B open-weight model"
   href="/blog/introducing-beam"></a>
<h2>Introducing Beam: Reflection’s 501B open-weight model</h2>
<a href="/blog/building-frontier-open-intelligencence"></a>
<h2>Building Frontier Open Intelligence</h2>
</body></html>
"""

BEAM_ARTICLE = """
<html><body>
<h1>Introducing Beam: Reflection’s 501B open-weight model</h1>
<p class="mt-4 text-tabs text-muted uppercase">October 5, 2026<span>|</span>8 min read</p>
<p>We are introducing Beam, Reflection’s first open-weight model. Beam is a
sparse Mixture-of-Experts model with 501 billion total parameters and 23
billion active. Weights follow later this month under Apache 2.0.</p>
<script>{"apiVersion":"2025-06-01"}</script>
</body></html>
"""

OLD_ARTICLE = """
<html><body>
<h1>Building Frontier Open Intelligence</h1>
<p>August 9, 2025</p>
</body></html>
"""


def _resp_json(payload):
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.json.return_value = payload
    resp.status_code = 200
    return resp


def _text_resp(text):
    class _Resp:
        def __init__(self):
            self.text = text
            self.content = text.encode("utf-8")

        def raise_for_status(self):
            return None

    return _Resp()


def test_reflection_beam_blog_is_a_candidate_on_oct_6(monkeypatch):
    def lab_get(url, source_name):
        if url.rstrip("/").endswith("reflection.ai/blog"):
            return _text_resp(REFLECTION_INDEX)
        if url.rstrip("/").endswith("/blog/introducing-beam"):
            return _text_resp(BEAM_ARTICLE)
        if "building-frontier" in url:
            return _text_resp(OLD_ARTICLE)
        return _text_resp("")

    monkeypatch.setattr(monitor, "DISCOVERY_ENABLED", False)
    monkeypatch.setattr(monitor, "PARALLEL_API_KEY", "")
    monkeypatch.setattr(monitor, "_lab_feed_get", lab_get)

    block = monitor.discover_recent_releases(today=OCT6, max_age_days=3)
    assert "Introducing Beam" in block
    assert "https://reflection.ai/blog/introducing-beam" in block
    assert "Building Frontier" not in block

    beam = next(
        m for m in monitor.LAST_DISCOVERY_MODELS
        if "introducing-beam" in (m.url or ""))
    assert beam.release_date == "2026-10-05"
    assert beam.source == "discovery"
    assert "501" in (beam.description or "")
    assert "opens in a new tab" not in (beam.description or "").lower()
    assert "lab-release" in beam.unique_traits
    assert monitor.is_significant_release(
        beam.name, "reflection", beam.unique_traits, downloads=0)
    assert monitor.categorize_model(beam) == "open_frontier"
    kept = monitor._filter_discovery_models(
        [beam], today=OCT6, max_age_days=monitor.DIGEST_FRESHNESS_DAYS)
    assert kept == [beam]
    # Oct 7 is still inside the 3-day window (announced Oct 5).
    assert monitor.digest_release_is_stale("2026-10-05", today="2026-10-07") is False


def test_reflection_org_is_significant_when_weights_land():
    # The Hub had no Beam repo this week. When reflectionai/Beam appears,
    # known-org membership must keep a 0-like day-one upload.
    assert "reflectionai" in monitor.KNOWN_ORGS
    assert "reflectionai" in monitor.MAJOR_HF_ORGS or "ReflectionAI" in monitor.MAJOR_HF_ORGS
    assert monitor.is_significant_release(
        "reflectionai/Beam", "reflectionai", ["text-generation"], downloads=0)
    assert monitor.is_noise_model(
        "reflectionai/Beam", "reflectionai", ["text-generation"],
        downloads=0, likes=0) is False


def test_openrouter_video_catalog_includes_grok_imagine_lite(monkeypatch):
    text_row = {
        "id": "x-ai/grok-4.7",
        "owned_by": "x-ai",
        "created": 1790000000,
        "context_length": 128000,
        "description": "A text model.",
        "pricing": {"prompt": "0.000002", "completion": "0.000006"},
        "architecture": {
            "input_modalities": ["text"],
            "output_modalities": ["text"],
        },
    }
    video_row = {
        "id": "x-ai/grok-imagine-video-1.5-lite",
        "owned_by": None,
        "created": GROK_CREATED,
        "context_length": 0,
        "hugging_face_id": None,
        "description": (
            "Grok Imagine Video 1.5 Lite is a faster, lower-cost video "
            "generation model. It supports text-to-video and image-to-video."
        ),
        "pricing": {"prompt": "0", "completion": "0"},
        "architecture": {
            "modality": "text+image->video",
            "input_modalities": ["text", "image"],
            "output_modalities": ["video"],
        },
    }
    calls = []

    def fake_get(url, source_name, timeout=30, **kwargs):
        params = kwargs.get("params") or {}
        calls.append(params.get("output_modalities"))
        if params.get("output_modalities") == "video":
            return _resp_json({"data": [video_row]})
        if params.get("output_modalities"):
            return _resp_json({"data": []})
        return _resp_json({"data": [text_row]})

    monkeypatch.setattr(monitor, "_http_get", fake_get)
    models = monitor.fetch_openrouter_models()
    grok = next(m for m in models if m.name.endswith("grok-imagine-video-1.5-lite"))
    assert "video" in calls
    assert grok.release_date == "2026-10-06"
    assert grok.provider == "xAI"
    assert grok.is_open_source is False
    assert grok.modality == "text-to-video"
    assert grok.pricing_input is None
    assert grok.pricing_output is None
    assert grok.context_window in (None, 0)
    assert monitor.categorize_model(grok) == "specialized"
    assert monitor.is_significant_release(
        grok.name, "x-ai", grok.unique_traits, downloads=0)
    assert monitor.digest_release_is_stale(grok.release_date, today=OCT9) is False
    # The Oct 5 digest ran before this catalog row existed (created 18:02 UTC
    # on Oct 6, cron is 16:00 UTC).
    assert monitor.digest_release_is_stale(grok.release_date, today="2026-10-05") is False
    body = monitor.build_digest_message([grok], today=OCT9)
    assert "SPECIALIZED" in body
    assert "Grok Imagine Video 1.5 Lite" in body
    assert "FREE" not in body
    assert "ALSO TRACKED" not in body


def test_video_catalog_failure_keeps_the_text_catalog(monkeypatch):
    def fake_get(url, source_name, timeout=30, **kwargs):
        params = kwargs.get("params") or {}
        if params.get("output_modalities"):
            raise RuntimeError("video catalog down")
        return _resp_json({"data": [{
            "id": "x-ai/grok-4.7",
            "owned_by": "x-ai",
            "created": GROK_CREATED,
            "context_length": 128000,
            "description": "Text.",
            "pricing": {"prompt": "0.000002", "completion": "0.000006"},
        }]})

    monkeypatch.setattr(monitor, "_http_get", fake_get)
    names = [m.name for m in monitor.fetch_openrouter_models()]
    assert names == ["x-ai/grok-4.7"]
    assert monitor._consume_source_error("OpenRouter") == ""


def test_aa_video_debut_with_old_ship_date_survives_after_baseline(monkeypatch):
    monkeypatch.setattr(monitor, "ARTIFICIAL_ANALYSIS_API_KEY", "aa-test-key")
    grok = {
        "id": "aa-grok",
        "name": "Grok Imagine Video 1.5 Lite",
        "slug": "grok-imagine-video-1-5-lite",
        "release_date": "2026-08-15",
        "model_creator": {"id": "c-xai", "name": "xAI"},
    }

    def fake_get(url, source_name, timeout=30, **kwargs):
        if url.endswith("/text-to-video-audio/models/free"):
            return _resp_json({"tier": "free", "data": [grok]})
        return _resp_json({"tier": "free", "data": []})

    monkeypatch.setattr(monitor, "_http_get", fake_get)

    first = monitor.fetch_artificial_analysis_models(seen=set(), today=OCT9)
    assert "Grok Imagine Video 1.5 Lite" not in [m.name for m in first]
    assert "aa/grok-imagine-video-1-5-lite" in monitor.LAST_AA_CATALOG_IDS

    later = monitor.fetch_artificial_analysis_models(
        seen={"aa/some-older-model"}, today=OCT9)
    model = next(m for m in later if m.name == "Grok Imagine Video 1.5 Lite")
    assert model.modality == "text-to-video"
    assert model.creator == "xAI"
    assert model.is_open_source is None or model.is_open_source is False
    assert model.release_date is None
    assert model.source == "artificial-analysis"
    assert monitor.categorize_model(model) == "specialized"
    rendered = monitor.render_model_entry(model, "")
    assert "Released Aug" not in rendered
    assert "text-to-video" in rendered
    assert "SPECIALIZED" not in rendered  # header is added by the digest, not the entry
    body = monitor.build_digest_message([model], today=OCT9)
    assert "━━━ <b>SPECIALIZED</b>" in body
    assert "Grok Imagine Video 1.5 Lite" in body
    urls = []

    def recording_get(url, source_name, timeout=30, **kwargs):
        urls.append(url)
        return fake_get(url, source_name, timeout=timeout, **kwargs)

    monkeypatch.setattr(monitor, "_http_get", recording_get)
    monitor.fetch_artificial_analysis_models(seen={"aa/some-older-model"}, today=OCT9)
    joined = " ".join(urls)
    for path in (
        "/api/v2/media/text-to-video/models/free",
        "/api/v2/media/text-to-video-audio/models/free",
        "/api/v2/media/image-to-video/models/free",
        "/api/v2/media/image-to-video-audio/models/free",
        "/api/v2/media/text-to-image/models/free",
        "/api/v2/media/text-to-speech/models/free",
        "/api/v2/media/speech-to-text/models/free",
        "/api/v2/media/speech-to-speech/models/free",
        "/api/v2/media/music/instrumental/models/free",
        "/api/v2/media/music/with-vocals/models/free",
    ):
        assert path in joined
    assert "/video/leaderboard/" not in joined

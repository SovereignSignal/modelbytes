"""Artificial Analysis free API — optional catalog source.

Fixtures are recorded response bodies. Nothing here calls the live API.
The key is optional: unset means skip, one info log, no error and no ops alert.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


TODAY = "2026-10-07"
KEY = "aa-test-key"

LANGUAGE_PAGE_1 = {
    "tier": "free",
    "pagination": {"page": 1, "page_size": 200, "total_pages": 2, "has_more": True},
    "data": [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "name": "Mistral Large 4",
            "slug": "mistral-large-4",
            "release_date": "2026-10-06",
            "model_creator": {"id": "c-mistral", "name": "Mistral"},
            "pricing": {
                "price_1m_input_tokens": 1.18,
                "price_1m_output_tokens": 3.5,
            },
            "huggingface_url": "https://huggingface.co/mistralai/Mistral-Large-4",
            "modalities": {
                "input": {"text": True, "image": True, "video": False, "speech": False},
                "output": {"text": True, "image": False, "video": False, "speech": False},
            },
        },
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "name": "Old LLM",
            "slug": "old-llm",
            "release_date": "2025-01-01",
            "model_creator": {"id": "c-old", "name": "OldLab"},
        },
        {
            "id": "33333333-3333-3333-3333-333333333333",
            "name": "gpt-oss-20B (high)",
            "slug": "gpt-oss-20b-high",
            "release_date": "2026-10-06",
            "model_creator": {"id": "c-oai", "name": "OpenAI"},
        },
    ],
}

LANGUAGE_PAGE_2 = {
    "tier": "free",
    "pagination": {"page": 2, "page_size": 200, "total_pages": 2, "has_more": False},
    "data": [
        {
            "id": "44444444-4444-4444-4444-444444444444",
            "name": "Solar Mini 4",
            "slug": "solar-mini-4",
            "release_date": "2026-10-06",
            "model_creator": {"id": "c-solar", "name": "Upstage"},
            "parameters": {"total": 21, "active": 21},
        }
    ],
}

MEDIA = {
    "text-to-image": {
        "tier": "free",
        "data": [
            {
                "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "name": "Qwen-Image-2.1",
                "slug": "qwen-image-2-1",
                "release_date": "2026-10-01",
                "model_creator": {"id": "c-qwen", "name": "Alibaba"},
            },
            {
                "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "name": "FLUX 3",
                "slug": "flux-3",
                "model_creator": {"id": "c-bfl", "name": "Black Forest Labs"},
            },
            {
                "id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "name": "Ideogram 4.5",
                "slug": "ideogram-4-5",
                "release_date": "2026-10-06",
                "model_creator": {"id": "c-ideo", "name": "Ideogram"},
                "open_weights_url": "https://huggingface.co/ideogram-ai/ideogram-4-5",
            },
        ],
    },
    "image-editing": {"tier": "free", "data": []},
    "text-to-speech": {"tier": "free", "data": []},
    "text-to-video": {"tier": "free", "data": []},
    "image-to-video": {"tier": "free", "data": []},
    "text-to-video-audio": {"tier": "free", "data": []},
    "image-to-video-audio": {"tier": "free", "data": []},
    "speech-to-text": {"tier": "free", "data": []},
    "speech-to-speech": {"tier": "free", "data": []},
    "music/instrumental": {"tier": "free", "data": []},
    "music/with-vocals": {"tier": "free", "data": []},
}


def _resp(payload):
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.json.return_value = payload
    resp.status_code = 200
    return resp


def _install_aa(monkeypatch, *, language_fails=False):
    monkeypatch.setattr(monitor, "ARTIFICIAL_ANALYSIS_API_KEY", KEY)
    calls = []

    def fake_get(url, source_name, timeout=30, **kwargs):
        calls.append((url, source_name, kwargs))
        if language_fails and "/language/" in url:
            raise RuntimeError("boom " + KEY)
        if url.endswith("/language/models/free"):
            page = int((kwargs.get("params") or {}).get("page") or 1)
            return _resp(LANGUAGE_PAGE_1 if page == 1 else LANGUAGE_PAGE_2)
        for lane, payload in MEDIA.items():
            if url.rstrip("/").endswith("/" + lane + "/models/free"):
                return _resp(payload)
        raise AssertionError("unexpected AA url " + url)

    monkeypatch.setattr(monitor, "_http_get", fake_get)
    return calls


def _names(models):
    return [m.name for m in models]


def test_aa_skipped_when_key_unset(monkeypatch, capsys):
    monkeypatch.setattr(monitor, "ARTIFICIAL_ANALYSIS_API_KEY", "")
    called = []
    monkeypatch.setattr(
        monitor, "_http_get", lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
            AssertionError("network")
        ),
    )
    alerts = []
    monkeypatch.setattr(monitor, "send_ops_alert", lambda text: alerts.append(text) or True)

    assert monitor.fetch_artificial_analysis_models(seen=set(), today=TODAY) == []
    err = capsys.readouterr().err
    assert err.count("MODELBYTES_ARTIFICIAL_ANALYSIS_API_KEY") == 1
    assert "unset" in err.lower() or "skip" in err.lower()
    assert called == []
    assert alerts == []
    assert monitor._consume_source_error("ArtificialAnalysis") == ""


def test_aa_hits_free_endpoints_and_pages(monkeypatch):
    calls = _install_aa(monkeypatch)
    monitor.fetch_artificial_analysis_models(seen=set(), today=TODAY)
    urls = [url for url, _source, _kw in calls]
    assert urls.count("https://artificialanalysis.ai/api/v2/language/models/free") == 2
    for lane in (
        "text-to-image",
        "image-editing",
        "text-to-speech",
        "text-to-video",
        "image-to-video",
        "text-to-video-audio",
        "image-to-video-audio",
        "speech-to-text",
        "speech-to-speech",
        "music/instrumental",
        "music/with-vocals",
    ):
        assert f"https://artificialanalysis.ai/api/v2/media/{lane}/models/free" in urls
    headers = calls[0][2]["headers"]
    assert headers["x-api-key"] == KEY
    assert calls[1][2]["params"]["page"] == 2


def test_aa_first_sight_keeps_fresh_dated_models_only(monkeypatch):
    _install_aa(monkeypatch)
    models = monitor.fetch_artificial_analysis_models(seen=set(), today=TODAY)
    assert _names(models) == ["Mistral Large 4", "Solar Mini 4", "Ideogram 4.5"]

    mistral = models[0]
    assert mistral.release_date == "2026-10-06"
    assert mistral.creator == "Mistral"
    assert mistral.modality == "multimodal"
    assert mistral.pricing_input == 1.18
    assert mistral.pricing_output == 3.5
    assert mistral.canonical_url == "https://huggingface.co/mistralai/Mistral-Large-4"
    assert mistral.credit_url == "https://artificialanalysis.ai/models/mistral-large-4"
    assert not (mistral.description or "").strip()
    assert "Mistral" not in (mistral.description or "")
    assert "multimodal" not in (mistral.description or "")

    solar = models[1]
    assert solar.total_parameters == "21B"
    assert solar.modality == "text"
    assert solar.creator == "Upstage"
    assert solar.url == "https://artificialanalysis.ai/models/solar-mini-4"

    ideogram = models[2]
    assert ideogram.modality == "text-to-image"
    assert ideogram.creator == "Ideogram"
    assert ideogram.release_date == "2026-10-06"
    assert ideogram.canonical_url == "https://huggingface.co/ideogram-ai/ideogram-4-5"
    assert ideogram.credit_url == "https://artificialanalysis.ai/image/models/ideogram-4-5"

    # Stale, undated, and effort-variant rows are remembered, not published.
    assert "aa/qwen-image-2-1" in monitor.LAST_AA_CATALOG_IDS
    assert "aa/flux-3" in monitor.LAST_AA_CATALOG_IDS
    assert "aa/old-llm" in monitor.LAST_AA_CATALOG_IDS
    assert "aa/gpt-oss-20b-high" in monitor.LAST_AA_CATALOG_IDS
    assert "aa/mistral-large-4" in monitor.LAST_AA_EMITTED_IDS
    assert "aa/flux-3" not in monitor.LAST_AA_EMITTED_IDS
    assert "aa/qwen-image-2-1" not in monitor.LAST_AA_EMITTED_IDS


def test_aa_new_undated_id_publishes_only_after_a_baseline(monkeypatch):
    _install_aa(monkeypatch)
    models = monitor.fetch_artificial_analysis_models(
        seen={"aa/old-llm"}, today=TODAY)
    assert "FLUX 3" in _names(models)
    flux = next(m for m in models if m.name == "FLUX 3")
    assert flux.release_date is None
    assert flux.creator == "Black Forest Labs"
    assert flux.modality == "text-to-image"
    assert flux.url == "https://artificialanalysis.ai/image/models/flux-3"
    # A language row with an old release date stays out.
    assert "Old LLM" not in _names(models)
    # A media id that was not on the baseline is a leaderboard debut. The
    # ship date (Oct 1) is outside the window, so it is not printed.
    qwen = next(m for m in models if m.name == "Qwen-Image-2.1")
    assert qwen.release_date is None
    assert qwen.modality == "text-to-image"


def test_aa_partial_failure_keeps_other_lanes_and_redacts_the_key(monkeypatch):
    _install_aa(monkeypatch, language_fails=True)
    models = monitor.fetch_artificial_analysis_models(seen=set(), today=TODAY)
    assert "Ideogram 4.5" in _names(models)
    assert "Mistral Large 4" not in _names(models)
    reason = monitor._consume_source_error("ArtificialAnalysis")
    assert "boom" in reason
    assert KEY not in reason


def test_aa_facts_render_on_the_code_built_line_not_in_prose():
    model = monitor.ModelRelease(
        name="Ideogram 4.5",
        provider="Ideogram",
        source="artificial-analysis",
        url="https://artificialanalysis.ai/image/models/ideogram-4-5",
        description="",
        release_date="2026-10-06",
        creator="Ideogram",
        modality="text-to-image",
    )
    prose = "Released Oct 6 by Ideogram, a text-to-image model."
    rendered = monitor.render_model_entry(model, prose)
    assert rendered == (
        "<b>Ideogram 4.5</b> — "
        "Released Oct 6 · Ideogram · text-to-image. "
        '<a href="https://artificialanalysis.ai/image/models/ideogram-4-5">'
        "→ Artificial Analysis</a>"
    )
    assert "🔗 Artificial Analysis" not in rendered
    assert "by Ideogram, a text-to-image" not in rendered


def test_aa_merge_fills_hf_model_and_keeps_the_hf_link():
    existing = monitor.ModelRelease(
        name="Qwen/Qwen-Image-2.1",
        provider="Qwen",
        source="huggingface",
        url="https://huggingface.co/Qwen/Qwen-Image-2.1",
        description="Image generator.",
        release_date=None,
    )
    incoming = monitor.ModelRelease(
        name="Qwen-Image-2.1",
        provider="Alibaba",
        source="artificial-analysis",
        url="https://artificialanalysis.ai/image/models/qwen-image-2-1",
        description="",
        release_date="2026-10-06",
        creator="Alibaba",
        modality="text-to-image",
        credit_url="https://artificialanalysis.ai/image/models/qwen-image-2-1",
        unique_traits=["aa-release", "aa-id:qwen-image-2-1"],
    )
    # The fixture date has to sit inside the 3-day digest window. Passing
    # today keeps the merge assertion from going stale with the calendar.
    assert monitor.absorb_extra_source(
        incoming, [existing], today="2026-10-07") is True
    assert existing.release_date == "2026-10-06"
    assert existing.creator == "Alibaba"
    assert existing.modality == "text-to-image"
    assert existing.url == "https://huggingface.co/Qwen/Qwen-Image-2.1"
    assert "aa-id:qwen-image-2-1" in existing.unique_traits
    rendered = monitor.render_model_entry(existing, existing.description)
    assert "Released Oct 6 · Alibaba · text-to-image" in rendered
    assert "→ HF</a>" in rendered
    assert "→ Artificial Analysis</a>" in rendered
    assert rendered.index("→ HF</a>") < rendered.index("→ Artificial Analysis</a>")


def test_aa_baseline_records_unemitted_ids_and_skips_preview():
    monitor.LAST_AA_CATALOG_IDS = {"aa/old-llm", "aa/flux-3"}
    monitor.LAST_AA_EMITTED_IDS = {"aa/flux-3"}
    seen = set()
    monitor._note_aa_baseline(seen, preview=True, seed=True)
    assert seen == set()
    monitor._note_aa_baseline(seen, preview=False, seed=False)
    assert seen == {"aa/old-llm"}
    monitor._note_aa_baseline(seen, preview=False, seed=True)
    assert seen == {"aa/old-llm", "aa/flux-3"}


def test_main_logs_disabled_aa_without_an_alert(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["monitor.py", "--preview"])
    monkeypatch.setattr(monitor, "try_post_pending_curated", lambda: False)
    monkeypatch.setattr(monitor, "init_database", lambda: None)
    monkeypatch.setattr(monitor, "load_seen_models", lambda: {"seed/already"})
    monkeypatch.setattr(monitor, "save_seen_models", lambda _s: None)
    monkeypatch.setattr(monitor, "ARTIFICIAL_ANALYSIS_API_KEY", "")
    monkeypatch.setattr(monitor, "DISCOVERY_ENABLED", False)
    monkeypatch.setattr(monitor, "PARALLEL_API_KEY", "")
    for name in (
        "fetch_openrouter_models",
        "fetch_ollama_models",
        "fetch_huggingface_trending",
        "fetch_major_orgs",
        "fetch_hf_text_generation",
        "fetch_testingcatalog_models",
    ):
        monkeypatch.setattr(monitor, name, lambda *args, **kwargs: [])
    alerts = []
    monkeypatch.setattr(monitor, "send_ops_alert", lambda text: alerts.append(text) or True)

    assert monitor.main() == 0
    err = capsys.readouterr().err
    assert (
        "source_health source=ArtificialAnalysis status=ok items=0 error=disabled"
        in err
    )
    assert err.count("MODELBYTES_ARTIFICIAL_ANALYSIS_API_KEY") == 1
    assert alerts == []

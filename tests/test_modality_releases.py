"""Image, video, audio, and other multimodal releases are not noise.

The 2026-10-05 production run dropped first-party models because
is_noise_model treats non-text pipelines and names like sam/whisper as
junk. They should reach the digest the same way text models do, once
likes/downloads, a model card, and the existing recency window clear.

Still dropped: GGUF/quant/LoRA-only packs, commodity classifiers and
embedders, empty cards, listicle titles, and stale back-catalog.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


TODAY = "2026-10-05"


class _Resp:
    def __init__(self, payload=None):
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _kept(name, author, tags, downloads=100_000, likes=500, **kwargs):
    assert monitor.is_noise_model(
        name, author, tags, downloads, likes, **kwargs,
    ) is False, name


def _dropped(name, author, tags, downloads=100_000, likes=500, **kwargs):
    assert monitor.is_noise_model(
        name, author, tags, downloads, likes, **kwargs,
    ) is True, name


# ── Kept: modalities and first-party / innovative releases ─────────────


def test_qwen_image_is_not_noise():
    # Pipeline text-to-image, plus the edit task image-text-to-image.
    _kept(
        "Qwen/Qwen-Image-2.1", "Qwen",
        ["text-to-image", "image-text-to-image", "image-generation", "diffusers"],
        downloads=94_556, likes=2975,
    )


def test_minimax_h3_video_is_not_noise():
    _kept(
        "MiniMaxAI/MiniMax-H3", "MiniMaxAI",
        [
            "image-text-to-video", "text-to-video", "image-to-video",
            "video-to-video", "text-to-audio", "multimodal",
        ],
        downloads=3_518_313, likes=5903,
    )


def test_ltx_video_is_not_noise_on_the_new_pass():
    # Lightricks is not a KNOWN_ORG and "LTX-2.5" ends in a digit, so the
    # org/trending structural cliff still applies. The new-on-HF pass opts
    # out of that cliff; video tags must not reject it there.
    tags = [
        "image-to-video", "text-to-video", "video-to-video",
        "image-text-to-video", "text-to-audio", "audio-to-audio",
    ]
    _dropped(
        "Lightricks/LTX-2.5", "Lightricks", tags,
        downloads=1_645_444, likes=6432,
    )
    _kept(
        "Lightricks/LTX-2.5", "Lightricks", tags,
        downloads=1_645_444, likes=6432, engagement_floor=False,
    )


def test_sam3_mask_model_is_not_noise_on_the_new_pass():
    # facebook/sam3 is mask-generation and also tagged feature-extraction.
    # The commodity tag must not veto the segmentation release. "sam3"
    # ends in a digit, so the structural cliff still applies off the new pass.
    tags = ["mask-generation", "feature-extraction", "image-segmentation", "sam3"]
    _dropped(
        "facebook/sam3", "facebook", tags,
        downloads=2_176_816, likes=3785,
    )
    _kept(
        "facebook/sam3", "facebook", tags,
        downloads=2_176_816, likes=3785, engagement_floor=False,
    )


def test_phonon_asr_is_not_noise_on_the_new_pass():
    tags = ["automatic-speech-recognition", "speech-to-text", "asr"]
    _dropped(
        "FermionResearch/Phonon-2", "FermionResearch", tags,
        downloads=3063, likes=216,
    )
    _kept(
        "FermionResearch/Phonon-2", "FermionResearch", tags,
        downloads=3063, likes=216, engagement_floor=False,
    )


def test_whisper_and_tts_and_music_are_not_noise():
    _kept(
        "openai/whisper-large-v3", "openai",
        ["automatic-speech-recognition"],
        downloads=5_000_000, likes=10_000,
    )
    _kept(
        "bosonai/higgs-audio-v3", "bosonai",
        ["text-to-speech"],
        downloads=80_000, likes=400,
    )
    _kept(
        "stabilityai/stable-audio-3", "stabilityai",
        ["music-generation"],
        downloads=80_000, likes=400,
    )
    # "vits" contains the substring "vit" but is a TTS architecture.
    _kept(
        "coqui/vits-en", "coqui", ["text-to-speech", "vits"],
        downloads=20_000, likes=200, engagement_floor=False,
    )


def test_jev_style_multimodal_and_text_releases_are_not_noise():
    # A LoRA *tag* is not a LoRA-only pack. JEV publishes as an adapter
    # and still a first-party release Sov wants in the digest.
    _kept(
        "autotrust/JEV-27B-VL", "autotrust",
        ["image-text-to-text", "multimodal", "lora", "text-generation", "vision"],
        downloads=1_278_569, likes=328,
    )
    _kept(
        "autotrust/JEV-9B", "autotrust",
        ["text-generation", "text-classification", "lora", "jev"],
        downloads=305_502, likes=117,
    )


def test_image_to_text_and_image_edit_are_not_noise():
    _kept(
        "Qwen/Qwen-Image-Edit", "Qwen",
        ["image-text-to-image"],
        downloads=50_000, likes=800,
    )
    _kept(
        "deepseek-ai/DeepSeek-OCR", "deepseek-ai",
        ["image-to-text"],
        downloads=50_000, likes=800,
    )


def test_unknown_org_engagement_floor_still_applies_to_image_models():
    # Modality is not a bypass. Modest unknown orgs still fail outside
    # the new pass; that pass keeps its own likes/downloads floor.
    tags = ["text-to-image"]
    assert monitor.is_noise_model(
        "NaiveAI/Naive-Image", "NaiveAI", tags,
        downloads=1920, likes=165,
    ) is True
    assert monitor.is_noise_model(
        "NaiveAI/Naive-Image", "NaiveAI", tags,
        downloads=1920, likes=165, engagement_floor=False,
    ) is False


# ── Still dropped: junk, commodity tasks, listicles ────────────────────


def test_gguf_and_quant_and_lora_packs_stay_noise_across_modalities():
    _dropped(
        "abenzerps/Qwen-Image-2.1-Uncensored-GGUF", "abenzerps",
        ["text-to-image", "gguf"],
        downloads=1_600_000, likes=3218,
    )
    _dropped(
        "Viggle/Qwen-Image-2.1-viggle-turbo", "Viggle",
        ["text-to-image", "gguf", "lora", "int8", "fp8"],
        downloads=286_000, likes=603,
    )
    _dropped(
        "Qwen/Qwen-Image-2.1-FP8", "Qwen",
        ["text-to-image"],
        downloads=90_000, likes=500,
    )
    _dropped(
        "Lightricks/LTX-2.5-22b-IC-LoRA-Alpha-Gen", "Lightricks",
        ["video-to-video", "lora"],
        downloads=2500, likes=85, engagement_floor=False,
    )
    _dropped(
        "akatz-ai/MiniMax-H3-Character-Swap-LoRA", "akatz-ai",
        ["video-to-video"],
        downloads=18_000, likes=297, engagement_floor=False,
    )


def test_commodity_classifiers_and_embedders_stay_noise():
    _dropped(
        "google/vit-base-patch16-224", "google",
        ["image-classification"],
    )
    _dropped(
        "ultralytics/yolo11", "ultralytics",
        ["object-detection"],
        downloads=500_000, likes=2000, engagement_floor=False,
    )
    _dropped(
        "BAAI/bge-large-en", "BAAI",
        ["feature-extraction", "sentence-similarity"],
    )
    _dropped(
        "google-bert/bert-base-uncased", "google-bert",
        ["fill-mask"],
        downloads=9_000_000, likes=2000, engagement_floor=False,
    )
    _dropped("someorg/toy-demo-sample", "someorg", ["text-to-image"])


def test_roundup_titles_stay_dropped():
    assert monitor._discovery_hit_to_model(
        "https://vendor.ai/blog/best-ai-models",
        "Best AI Models in October 2026",
        "A weekly roundup of every LLM",
        "2026-10-04",
    ) is None


# ── New-on-HF pass: floors, cards, recency ─────────────────────────────


def _hf_row(model_id, author, pipeline, tags, likes, downloads, created,
            modified=None, card=None):
    row = {
        "id": model_id,
        "author": author,
        "pipeline_tag": pipeline,
        "tags": list(tags),
        "likes": likes,
        "downloads": downloads,
        "createdAt": created,
    }
    detail = dict(row)
    if modified is not None:
        detail["lastModified"] = modified
    if card is not None:
        detail["cardData"] = card
    return row, detail


def test_new_pass_keeps_modality_releases_that_clear_floors(monkeypatch):
    specs = [
        ("Qwen/Qwen-Image-2.1", "Qwen", "text-to-image",
         ["text-to-image", "image-text-to-image"], 2975, 94_556,
         "2026-09-14T03:47:26.000Z", "2026-09-30T02:14:09.000Z"),
        ("MiniMaxAI/MiniMax-H3", "MiniMaxAI", "image-text-to-video",
         ["image-text-to-video", "text-to-video", "text-to-audio"], 5903, 3_518_313,
         "2026-10-01T00:00:00.000Z", "2026-10-03T00:00:00.000Z"),
        ("Lightricks/LTX-2.5", "Lightricks", "image-to-video",
         ["image-to-video", "text-to-video", "text-to-audio"], 6432, 1_645_444,
         "2026-07-23T07:55:24.000Z", "2026-10-02T21:01:57.000Z"),
        ("facebook/sam3", "facebook", "mask-generation",
         ["mask-generation", "feature-extraction", "image-segmentation"], 3785, 2_176_816,
         "2026-10-01T00:00:00.000Z", "2026-10-04T00:00:00.000Z"),
        ("FermionResearch/Phonon-2", "FermionResearch", "automatic-speech-recognition",
         ["automatic-speech-recognition"], 216, 3063,
         "2026-09-28T11:48:47.000Z", "2026-10-01T16:49:58.000Z"),
        ("autotrust/JEV-27B-VL", "autotrust", "image-text-to-text",
         ["image-text-to-text", "lora", "multimodal"], 328, 1_278_569,
         "2026-09-30T05:02:23.000Z", "2026-10-03T11:00:42.000Z"),
        ("autotrust/JEV-9B", "autotrust", "text-classification",
         ["text-generation", "text-classification", "lora"], 117, 305_502,
         "2026-09-23T12:08:52.000Z", "2026-10-03T07:05:07.000Z"),
    ]
    rows, details = [], {}
    for model_id, author, pipeline, tags, likes, downloads, created, modified in specs:
        row, detail = _hf_row(
            model_id, author, pipeline, tags, likes, downloads, created,
            modified=modified, card={"license": "apache-2.0"},
        )
        rows.append(row)
        details[model_id] = detail

    dropped_specs = [
        ("abenzerps/Qwen-Image-2.1-Uncensored-GGUF", "abenzerps", "text-to-image",
         ["text-to-image", "gguf"], 3218, 1_600_000,
         "2026-09-20T00:00:00.000Z", "2026-10-02T00:00:00.000Z", {"license": "other"}),
        ("Viggle/Qwen-Image-2.1-viggle-turbo", "Viggle", "text-to-image",
         ["text-to-image", "gguf", "lora"], 603, 286_000,
         "2026-09-22T00:00:00.000Z", "2026-10-02T00:00:00.000Z", {"license": "other"}),
        ("Lightricks/LTX-2.5-22b-IC-LoRA-Alpha-Gen", "Lightricks", "video-to-video",
         ["video-to-video"], 85, 2500,
         "2026-09-28T00:00:00.000Z", "2026-10-02T00:00:00.000Z", {"license": "other"}),
        ("google/vit-base-patch16-224", "google", "image-classification",
         ["image-classification"], 1000, 50_000,
         "2026-10-01T00:00:00.000Z", "2026-10-03T00:00:00.000Z", {"license": "apache-2.0"}),
        ("nocard/Image-Model", "nocard", "text-to-image",
         ["text-to-image"], 200, 1000,
         "2026-10-01T00:00:00.000Z", "2026-10-03T00:00:00.000Z", {}),
        ("backcatalog/Old-Flux", "backcatalog", "text-to-image",
         ["text-to-image"], 800, 20_000,
         "2024-01-01T00:00:00.000Z", "2024-01-02T00:00:00.000Z", {"license": "mit"}),
        ("spam/tiny-image", "spam", "text-to-image",
         ["text-to-image"], 0, 0,
         "2026-10-04T00:00:00.000Z", "2026-10-04T00:00:00.000Z", {"license": "mit"}),
    ]
    for model_id, author, pipeline, tags, likes, downloads, created, modified, card in dropped_specs:
        row, detail = _hf_row(
            model_id, author, pipeline, tags, likes, downloads, created,
            modified=modified, card=card,
        )
        rows.append(row)
        details[model_id] = detail

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
    found = {m.name for m in monitor.fetch_hf_new_models(today=TODAY)}
    for model_id, *_rest in specs:
        assert model_id in found, model_id
    for model_id, *_rest in dropped_specs:
        assert model_id not in found, model_id


def test_real_sam3_and_minimax_clocks_stay_stale(monkeypatch, capsys):
    # Live HF clocks on 2026-10-05: sam3 last touched 2025-11-20, MiniMax-H3
    # last touched 2026-08-13. Modality no longer rejects them; the 14-day
    # window still does.
    specs = [
        ("facebook/sam3", "facebook", "mask-generation",
         ["mask-generation", "feature-extraction"], 3785, 2_176_816,
         "2025-11-07T05:17:48.000Z", "2025-11-20T22:05:08.000Z"),
        ("MiniMaxAI/MiniMax-H3", "MiniMaxAI", "image-text-to-video",
         ["image-text-to-video", "text-to-video"], 5903, 3_518_313,
         "2026-07-28T10:45:18.000Z", "2026-08-13T01:46:29.000Z"),
    ]
    rows, details = [], {}
    for model_id, author, pipeline, tags, likes, downloads, created, modified in specs:
        row, detail = _hf_row(
            model_id, author, pipeline, tags, likes, downloads, created,
            modified=modified, card={"license": "other"},
        )
        rows.append(row)
        details[model_id] = detail

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
    found = {m.name for m in monitor.fetch_hf_new_models(today=TODAY)}
    assert found == set()
    err = capsys.readouterr().err
    assert "name=facebook/sam3" in err
    assert "name=MiniMaxAI/MiniMax-H3" in err
    for line in err.splitlines():
        if "facebook/sam3" in line or "MiniMax-H3" in line:
            assert "stale" in line, line
            assert "noise filter" not in line

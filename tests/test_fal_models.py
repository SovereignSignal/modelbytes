"""fal.ai public model catalog — hosted launches as digest candidates.

The fixture is a small, sanitized slice of the live catalog taken on
2026-10-16 (thumbnails on example.com, cursors "placeholder"). Nothing here
calls api.fal.ai; conftest blanks _fal_get and these tests stub it back.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


TODAY = "2026-10-16"
FIXTURE = Path(__file__).parent / "fixtures" / "fal_models_2026-10-16.json"


def _catalog():
    return json.loads(FIXTURE.read_text())


class _Page:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def _install_fal(monkeypatch, rows=None, page_size=7, fail=False):
    rows = _catalog() if rows is None else rows
    pages = [rows[i:i + page_size] for i in range(0, len(rows), page_size)] or [[]]
    calls = []
    state = {"index": 0}

    def fake_get(url, params):
        calls.append((url, dict(params)))
        if fail:
            raise RuntimeError("fal down")
        state["index"] = state["index"] + 1 if "cursor" in params else 0
        index = state["index"]
        more = index + 1 < len(pages)
        return _Page({
            "models": pages[index],
            "next_cursor": "placeholder" if more else None,
            "has_more": more,
        })

    monkeypatch.setattr(monitor, "_fal_get", fake_get)
    return calls


def _by_name(models):
    return {m.name: m for m in models}


def test_fal_pages_through_the_whole_catalog(monkeypatch):
    calls = _install_fal(monkeypatch)
    monitor.fetch_fal_models(set(), today=TODAY)
    assert len(calls) == 3
    assert calls[0] == (monitor.FAL_MODELS_URL, {"limit": 100})
    assert calls[1][1] == {"limit": 100, "cursor": "placeholder"}


def test_fal_page_cap_stops_a_runaway_cursor(monkeypatch):
    calls = []

    def endless(url, params):
        calls.append(params)
        return _Page({"models": [], "next_cursor": "placeholder", "has_more": True})

    monkeypatch.setattr(monitor, "_fal_get", endless)
    assert monitor.fetch_fal_models(set(), today=TODAY) == []
    assert len(calls) == monitor._FAL_PAGE_CAP


def test_fal_groups_endpoints_into_one_launch_each(monkeypatch):
    _install_fal(monkeypatch)
    models = monitor.fetch_fal_models(set(), today=TODAY)
    names = sorted(m.name for m in models)
    assert names == sorted([
        "Krea 3",
        "NVIDIA Nemotron 3.5 Super",
        "Mirelo SFX2.0",
        "FLUX.3 Klein",
        "Bytedance Omnihuman V2",
        "Abot World 0",
    ])
    by = _by_name(models)
    krea = by["Krea 3"]
    assert krea.provider == "Krea"
    assert krea.source == "fal"
    assert krea.release_date == "2026-10-14"
    assert krea.url.startswith("https://fal.ai/models/krea/v3/")
    assert krea.credit_url == krea.url
    assert krea.is_open_source is None
    assert "fal-release" in krea.unique_traits
    assert "fal-id:fal/krea-v3" in krea.unique_traits
    assert "fal-id:fal/krea/v3" in krea.unique_traits
    nemo = by["NVIDIA Nemotron 3.5 Super"]
    assert nemo.provider == "NVIDIA"
    assert nemo.modality == "multimodal"
    assert by["FLUX.3 Klein"].provider == "Black Forest Labs"
    assert by["FLUX.3 Klein"].modality == "text-to-image"
    assert by["Mirelo SFX2.0"].provider == "Mirelo"
    assert by["Bytedance Omnihuman V2"].provider == "ByteDance"
    assert by["Bytedance Omnihuman V2"].modality == "image-to-video"


def test_fal_skips_tools_training_old_families_and_stale_launches(monkeypatch):
    _install_fal(monkeypatch)
    names = " ".join(m.name for m in monitor.fetch_fal_models(set(), today=TODAY))
    # kind=training, router, and a stale launch (Nano Banana Pro, Flux 3 Image).
    for skipped in ("Trainer", "Router", "Nano Banana", "Flux 3 Image"):
        assert skipped not in names
    # New endpoints on families that launched weeks ago are not launches.
    assert "H3 Max" not in names
    assert "Meshy" not in names


def test_fal_utility_rule():
    def row(endpoint, name="X", **md):
        meta = {"display_name": name, "status": "active", "kind": "inference",
                "category": "image-to-image"}
        meta.update(md)
        return {"endpoint_id": endpoint, "metadata": meta}

    assert monitor._fal_is_launch_endpoint(row("krea/v3/edit"))
    assert not monitor._fal_is_launch_endpoint(row("bria/guided-remove-background"))
    assert not monitor._fal_is_launch_endpoint(row("fal-ai/esrgan-upscaler"))
    assert not monitor._fal_is_launch_endpoint(row("fal-ai/krea-2-trainer"))
    assert not monitor._fal_is_launch_endpoint(row("fal-ai/x", kind="training"))
    assert not monitor._fal_is_launch_endpoint(row("fal-ai/x", category="workflow"))
    assert not monitor._fal_is_launch_endpoint(row("fal-ai/x", status="deprecated"))


def test_fal_path_keys_strip_task_segments():
    assert monitor._fal_path_key("krea/v3/text-to-image") == "fal/krea/v3"
    assert monitor._fal_path_key("krea/v3/realtime") == "fal/krea/v3"
    assert monitor._fal_path_key("mirelo-ai/sfx2.0/video-to-audio") == "fal/mirelo-ai/sfx2.0"
    assert (monitor._fal_path_key("blackforestlabs/flux-3/klein/edit-image")
            == "fal/blackforestlabs/flux-3/klein")
    assert monitor._fal_path_key("nvidia/nemotron-3-5-super/vision") == "fal/nvidia/nemotron-3-5-super"
    # Never strip down to the bare vendor namespace.
    assert monitor._fal_path_key("openai/text-to-speech") == "fal/openai/text-to-speech"
    assert monitor._fal_path_key("fal-ai/nano-banana-pro/edit") == "fal/nano-banana-pro"


def test_fal_vendor_from_namespace():
    assert monitor._fal_vendor("fal-ai/vidu/q4/image-to-video") == "Vidu"
    assert monitor._fal_vendor("xai/grok-imagine-video/v1.5") == "xAI"
    assert monitor._fal_vendor("kotoba-technologies/kotoba-tts/v2") == "Kotoba Technologies"


def test_fal_seen_keys_suppress_a_launch_and_baseline_records_the_rest(monkeypatch):
    _install_fal(monkeypatch)
    seen = {"fal/krea-v3"}
    models = monitor.fetch_fal_models(seen, today=TODAY)
    assert "Krea 3" not in _by_name(models)
    # Baseline: everything observed except what is still a candidate.
    monitor._note_fal_baseline(seen, preview=False)
    assert "fal/minimax/h3-max" in seen or "fal/hailuo-03-new-turbo" in seen
    assert "fal/nano-banana-pro" in seen
    assert "fal/krea/v3" in seen  # observed with the seen launch
    assert "fal/nvidia/nemotron-3-5-super" not in seen  # candidate, not posted yet
    # Next run with the same seen set: only the unpublished candidates return.
    again = monitor.fetch_fal_models(seen, today=TODAY)
    assert sorted(m.name for m in again) == sorted(m.name for m in models)


def test_fal_preview_never_writes_the_baseline(monkeypatch):
    _install_fal(monkeypatch)
    monitor.fetch_fal_models(set(), today=TODAY)
    seen = set()
    monitor._note_fal_baseline(seen, preview=True)
    assert seen == set()


def test_fal_failure_is_silent_and_recorded_for_source_health(monkeypatch, capsys):
    _install_fal(monkeypatch, fail=True)
    assert monitor.fetch_fal_models(set(), today=TODAY) == []
    assert monitor.LAST_FAL_CATALOG_IDS == set()
    assert monitor._consume_source_error("fal")
    assert "fal catalog failed" in capsys.readouterr().err


def test_fal_skips_a_launch_named_in_a_recent_digest(monkeypatch):
    _install_fal(monkeypatch)
    models = monitor.fetch_fal_models(
        set(), today=TODAY, recent_names=["FLUX.3 Klein"])
    assert "FLUX.3 Klein" not in _by_name(models)


def test_fal_row_absorbs_into_a_primary_row_without_replacing_its_url():
    primary = monitor.ModelRelease(
        name="nvidia/NVIDIA-Nemotron-3.5-Super-120B-A12B",
        provider="NVIDIA", source="huggingface",
        url="https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Super-120B-A12B",
        description="")
    fal = monitor.ModelRelease(
        name="NVIDIA Nemotron 3.5 Super", provider="NVIDIA", source="fal",
        url="https://fal.ai/models/nvidia/nemotron-3-5-super/text",
        description="Chat with Nemotron.", release_date="2026-10-15",
        modality="multimodal",
        credit_url="https://fal.ai/models/nvidia/nemotron-3-5-super/text",
        unique_traits=["fal-release", "fal-id:fal/nvidia/nemotron-3-5-super"])
    assert monitor.absorb_extra_source(fal, [primary], today=TODAY) is True
    assert primary.url.startswith("https://huggingface.co/")
    assert primary.release_date == "2026-10-15"
    assert primary.modality == "multimodal"
    assert primary.credit_url == fal.credit_url
    manifest = monitor.build_publication_manifest(
        [primary], f'<b>Nemotron</b> <a href="{primary.url}">→ HF</a>')
    assert "fal/nvidia/nemotron-3-5-super" in manifest.seen_ids


def test_fal_merge_keeps_the_row_significant():
    existing = monitor.ModelRelease(
        name="newlab/vector-model-1", provider="newlab", source="openrouter",
        url="https://openrouter.ai/models/newlab/vector-model-1", description="")
    fal = monitor.ModelRelease(
        name="Vector Model 1", provider="Newlab", source="fal",
        url="https://fal.ai/models/newlab/vector-model-1", description="",
        release_date=TODAY,
        unique_traits=["fal-release", "fal-id:fal/newlab/vector-model-1"])
    assert monitor.absorb_extra_source(fal, [existing], today=TODAY)
    assert monitor.is_significant_release(
        existing.name, "newlab", existing.unique_traits, 0)


def test_fal_entry_renders_as_hosted_not_open_weights():
    model = monitor.ModelRelease(
        name="Krea 3", provider="Krea", source="fal",
        url="https://fal.ai/models/krea/v3/edit", description="",
        release_date="2026-10-14", modality="text-to-image",
        credit_url="https://fal.ai/models/krea/v3/edit",
        unique_traits=["fal-release"])
    line = monitor.render_model_entry(model, "Fast image model.")
    assert "Open weights" not in line
    assert "⚡ API live · fal" in line
    assert line.count("https://fal.ai/models/krea/v3/edit") == 1
    assert "→ fal</a>" in line


def test_fal_launches_rank_as_significant():
    assert monitor.is_significant_release("Krea 3", "", ["fal-release"], 0)


def test_main_runs_fal_marks_only_published_launches_seen(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["monitor.py"])
    monkeypatch.setattr(monitor, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(monitor, "TELEGRAM_CHANNEL_ID", "-100XXXXXXXXXX")
    monkeypatch.setattr(monitor, "DATABASE_URL", "postgres://placeholder")
    monkeypatch.setattr(monitor, "try_post_pending_curated", lambda: False)
    monkeypatch.setattr(monitor, "init_database", lambda: None)
    monkeypatch.setattr(monitor, "load_seen_models", lambda: {"seed/already"})
    saved = []
    monkeypatch.setattr(monitor, "save_seen_models", lambda s: saved.append(set(s)))
    monkeypatch.setattr(monitor, "DISCOVERY_ENABLED", False)
    monkeypatch.setattr(monitor, "PARALLEL_API_KEY", "")
    monkeypatch.setattr(monitor, "enrich_with_hf_cards", lambda models: None)
    monkeypatch.setattr(monitor, "_recent_digest_names", lambda *a, **k: [])
    for name in (
        "fetch_openrouter_models", "fetch_ollama_models",
        "fetch_huggingface_trending", "fetch_major_orgs",
        "fetch_hf_text_generation", "fetch_testingcatalog_models",
    ):
        monkeypatch.setattr(monitor, name, lambda *args, **kwargs: [])
    _install_fal(monkeypatch)
    handed = []

    def writer(models, *a, **k):
        handed.extend(models)
        return ("🤖 <b>ModelBytes Digest</b>\n\n━━━ <b>SPECIALIZED</b> 🎯\n"
                "<b>Krea 3</b> — <i>Fast image model.</i> "
                '<a href="https://fal.ai/models/krea/v3/realtime">→ fal</a>\n')

    monkeypatch.setattr(monitor, "summarize_models", writer)
    monkeypatch.setattr(monitor, "validate_digest_for_publish",
                        lambda body, mode: (body, [], []))
    monkeypatch.setattr(monitor, "send_telegram_post", lambda body: True)
    for name in ("mark_posted_digest", "record_publish_run", "ping_heartbeat",
                 "_forward_published_releases", "_push_ai_wire",
                 "emit_writer_health"):
        monkeypatch.setattr(monitor, name, lambda *a, **k: None)
    monkeypatch.setattr(monitor, "send_slack_post", lambda body: True)
    monkeypatch.setattr(monitor, "send_ops_alert", lambda text: True)

    assert monitor._run_digest(TODAY, preview_mode=False) == 0
    assert "Krea 3" in {m.name for m in handed}
    assert "source_health source=fal status=ok" in capsys.readouterr().err
    final = saved[-1]
    assert "Krea 3" in final
    assert "fal/krea-v3" in final
    # Handed to the writer but not printed: stays eligible.
    assert "FLUX.3 Klein" not in final
    assert not ({"fal/flux-3-klein", "fal/blackforestlabs/flux-3/klein"} & final)
    # Back-catalog recorded silently.
    assert "fal/nano-banana-pro" in final


def test_fal_hosted_vendor_namespace_joins_the_vendor_family():
    # fal-ai/meshy/v6 (Feb) and meshy/v6/retexture (Oct) are one family.
    assert (monitor._fal_path_key("fal-ai/meshy/v6/text-to-3d")
            == monitor._fal_path_key("meshy/v6/retexture") == "fal/meshy/v6")
    rows = [
        {"endpoint_id": "fal-ai/meshy/v6/text-to-3d",
         "metadata": {"display_name": "Meshy 6", "category": "text-to-3d",
                      "status": "active", "date": "2026-02-09T00:00:00Z"}},
        {"endpoint_id": "meshy/v6/retexture",
         "metadata": {"display_name": "Meshy 6 Retexture", "category": "3d-to-3d",
                      "status": "active", "date": "2026-10-14T00:00:00Z"}},
    ]
    assert len(monitor._fal_launches(rows)) == 1


def test_fal_name_cleanup_keeps_more_than_the_vendor():
    assert monitor._fal_clean_name("Krea 3 Text to Image", True) == "Krea 3"
    assert monitor._fal_clean_name("FLUX.3 Klein Image Editing", True) == "FLUX.3 Klein"
    assert monitor._fal_clean_name("NVIDIA Nemotron 3.5 Super Video", True) == "NVIDIA Nemotron 3.5 Super"
    assert monitor._fal_clean_name("Kotoba Text-to-Speech", False) == "Kotoba Text-to-Speech"
    # A single-endpoint launch keeps a trailing variant word.
    assert monitor._fal_clean_name("Flux 3 Image", False) == "Flux 3 Image"
    # The task strip stands even when the variant strip would leave a bare
    # vendor (fal heygen/heygen-video, 2026-10-20).
    assert monitor._fal_clean_name("HeyGen Video Text to Video", True) == "HeyGen Video"
    assert monitor._fal_clean_name("Kling 4.0 Video Editing", True) == "Kling 4.0"

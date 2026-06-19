"""Hardening for the fallback digest path: filter fine-tune variant spam (C),
guarantee content links (D), and an honest surfaced/scanned footer (E)."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


def _model(name, **kw):
    """Build a ModelRelease with sensible defaults for digest tests."""
    fields = dict(
        provider="TestProvider",
        source="huggingface",
        url="https://huggingface.co/" + name,
        description="A capable open model with strong benchmarks.",
    )
    fields.update(kw)
    return monitor.ModelRelease(name=name, **fields)


# ── C: fine-tune / RL variant spam must be filtered, even from known orgs ──

def test_sft_data_size_variants_are_noise_even_from_known_orgs():
    # open-thoughts is a KNOWN_ORG, so it bypasses the engagement gate; the
    # SFT data-size variants (the 2026-06-08 spam) must still be filtered.
    assert monitor.is_noise_model(
        "open-thoughts/OpenThinkerAgent-32B-SFT-100K", "open-thoughts", []) is True
    assert monitor.is_noise_model(
        "open-thoughts/OpenThinkerAgent-32B-SFT-3.16K", "open-thoughts", []) is True


def test_dpo_and_rl_variants_are_noise():
    assert monitor.is_noise_model(
        "bigorg/Model-7B-DPO", "bigorg", [], downloads=999999, likes=99999) is True
    assert monitor.is_noise_model(
        "bigorg/Model-7B-GRPO", "bigorg", [], downloads=999999, likes=99999) is True


def test_base_model_not_filtered_by_the_variant_rule():
    # The base release (no -SFT-/-DPO-) from a known org must still pass.
    assert monitor.is_noise_model(
        "open-thoughts/OpenThinkerAgent-32B", "open-thoughts", []) is False


# ── D: the deterministic template must carry content links ──

def test_also_tracked_entries_link_to_content():
    # A model categorized as "other" lands in ALSO TRACKED; it must be linked.
    m = _model("acme/Mystery-Model-1", url="https://huggingface.co/acme/Mystery-Model-1")
    with patch.object(monitor, "categorize_model", return_value="other"):
        msg = monitor.build_digest_message([m])
    assert "ALSO TRACKED" in msg
    assert '<a href="https://huggingface.co/acme/Mystery-Model-1">' in msg


def test_section_entries_use_html_links_not_raw_urls():
    m = _model("acme/Premier-1", canonical_url="https://acme.ai/premier-1")
    with patch.object(monitor, "categorize_model", return_value="open_frontier"):
        msg = monitor.build_digest_message([m])
    assert '<a href="https://acme.ai/premier-1">' in msg
    assert "🔗 https://" not in msg  # no bare URL dumps


# ── E: honest "Surfaced N · scanned M" footer on the LLM path ──

def test_count_surfaced_models_counts_entries_not_headers():
    body = (
        "<b>🔓 Premier Open</b>\n"
        '<b>Model A</b> — Released Jun 1. Great. <a href="https://u.example">→ Src</a>\n'
        '<b>Model B</b> — Released Jun 2. Good. <a href="https://u.example">→ Src</a>\n'
        "<b>🏠 Local Ready</b>\n"
        "• <b>Model C</b> — runs local <a href=\"u\">→ HF</a>\n"
    )
    assert monitor._count_surfaced_models(body) == 3


def test_llm_footer_reports_surfaced_and_scanned():
    # Pass 3 models but the LLM surfaces only 1 → footer must say 1 · 3.
    models = [_model(f"acme/Model-{i}") for i in range(1, 4)]
    fake = MagicMock()
    fake.json.return_value = {
        "choices": [{"message": {"content": (
            "<b>🔓 Premier Open</b>\n"
            # link must be a provided candidate URL (link-verification strips constructed ones)
            '<b>Model 1</b> — Released Jun 1. The standout. <a href="https://huggingface.co/acme/Model-1">→ Source</a>'
        )}}]
    }
    fake.raise_for_status = lambda: None
    with patch.object(monitor, "LLM_API_KEY", "test-key"), \
         patch.object(monitor.requests, "post", return_value=fake):
        msg = monitor.summarize_models(models)
    assert "📊 Surfaced 1 · scanned 3 today" in msg
    assert "models tracked today" not in msg.lower()


def test_empty_llm_body_falls_back_to_template():
    # GLM-style reasoning models can return an empty content field; the digest
    # must fall back to the deterministic template, not ship a blank body.
    models = [_model("acme/Model-1")]
    fake = MagicMock()
    fake.json.return_value = {"choices": [{"message": {"content": "   "}}]}
    fake.raise_for_status = lambda: None
    with patch.object(monitor, "LLM_API_KEY", "test-key"), \
         patch.object(monitor.requests, "post", return_value=fake):
        msg = monitor.summarize_models(models)
    assert "ModelBytes Digest" in msg
    assert "items tracked today" in msg  # template footer, not an empty body


def test_validate_accepts_the_new_surfaced_footer():
    body = (
        "🤖 <b>ModelBytes Digest</b>\n<i>Monday, June 08, 2026</i>\n\n"
        '<b>🔓 Premier Open</b>\n<b>Model A</b> — Good. <a href="https://u.example">→ Source</a>\n\n'
        "📊 Surfaced 1 · scanned 5 today"
    )
    _, warnings, errors = monitor.validate_digest_for_publish(body)
    assert errors == []
    assert not any("footer is missing" in w for w in warnings)


# ── 2026-06-11 incident hardening: quant suffixes + stale back-catalog ──

def test_quant_serving_suffixes_are_noise():
    # command-a-plus-05-2026-w4a4 / -fp8 leaked into the 06-11 fallback digest:
    # serving/quant builds of an already-released model, not new releases.
    assert monitor.is_noise_model(
        "CohereLabs/command-a-plus-05-2026-w4a4", "CohereLabs", [],
        downloads=999999, likes=9999) is True
    assert monitor.is_noise_model(
        "CohereLabs/command-a-plus-05-2026-fp8", "CohereLabs", [],
        downloads=999999, likes=9999) is True


def test_speculative_decoding_variants_are_noise():
    # Eagle3 = speculative-decoding draft head, a derivative artifact.
    assert monitor.is_noise_model(
        "moonshotai/Kimi-K2.5-Thinking-Eagle3", "moonshotai", [],
        downloads=999999, likes=9999) is True


def test_base_release_with_version_not_filtered_by_quant_rule():
    assert monitor.is_noise_model(
        "CohereLabs/command-a-plus-05-2026", "CohereLabs", []) is False


def test_stale_release_gate():
    # New-org backfill: when the supervisor adds an org, its whole back-catalog
    # is "unseen" — a 2025 model must not appear in a 2026 "new today" digest.
    assert monitor.is_stale_release("2025-04-09", today="2026-06-11") is True
    assert monitor.is_stale_release("2026-01-01", today="2026-06-11") is True
    assert monitor.is_stale_release("2026-06-05", today="2026-06-11") is False
    assert monitor.is_stale_release(None, today="2026-06-11") is False  # unknown date: keep
    assert monitor.is_stale_release("garbage", today="2026-06-11") is False  # unparseable: keep


# ── 2026-06-13 inline-preview leaks: QAT-mobile packaging, NVFP4, abliterations ──

def test_qat_mobile_packaging_is_noise():
    for name in ("google/gemma-4-E2B-it-qat-mobile-ct",
                 "google/gemma-4-E4B-it-qat-mobile-transformers"):
        assert monitor.is_noise_model(name, "google", [],
                                      downloads=999999, likes=9999) is True, name


def test_nvfp4_quant_is_noise():
    assert monitor.is_noise_model(
        "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-NVFP4", "nvidia", [],
        downloads=999999, likes=9999) is True


def test_abliteration_and_uncensored_finetunes_are_noise():
    for name in ("OBLITERATUS/Gemma-4-12B-OBLITERATED",
                 "someorg/Llama-4-abliterated",
                 "HauhauCS/Qwen3.6-35B-A3B-Uncensored-Aggressive"):
        assert monitor.is_noise_model(name, name.split("/")[0], [],
                                      downloads=999999, likes=9999) is True, name


def test_base_instruct_release_not_caught_by_new_patterns():
    # The real base model from a known org must still pass.
    assert monitor.is_noise_model("google/gemma-4-12B-it", "google", []) is False


# ── 2026-06-13: GGUF always-noise + preview is side-effect-free ──

def test_gguf_is_noise_even_from_known_orgs():
    # unsloth is a KNOWN_ORG; its GGUF repackages used to pass the noise filter,
    # then trip the publish-QA quant gate and BLOCK the whole digest (dark
    # channel). A GGUF is never a primary release here → always noise.
    assert monitor.is_noise_model(
        "unsloth/diffusiongemma-26B-A4B-it-GGUF", "unsloth", []) is True
    assert monitor.is_noise_model(
        "bartowski/SomeModel-7B-GGUF", "bartowski", []) is True


def test_preview_with_qa_error_sends_no_ops_alert(monkeypatch, tmp_path, capsys):
    # A preview run whose fallback digest trips a QA error must NOT alert the
    # operator or write to the DB — it just prints what WOULD block.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["monitor.py", "--preview"])
    monkeypatch.setattr(monitor, "try_post_pending_curated", lambda: False)
    monkeypatch.setattr(monitor, "init_database", lambda: None)
    monkeypatch.setattr(monitor, "load_seen_models", lambda: {"seed/x"})
    monkeypatch.setattr(monitor, "save_seen_models", lambda s: None)
    for f in ["fetch_ollama_models", "fetch_huggingface_trending",
              "fetch_major_orgs", "fetch_hf_text_generation"]:
        monkeypatch.setattr(monitor, f, lambda: [])
    # One model that will produce a digest; force a QA error via summarize.
    m = monitor.ModelRelease(name="acme/Model-1", provider="acme",
                             source="huggingface", url="https://hf.co/acme/Model-1",
                             description="x", is_open_source=True)
    monkeypatch.setattr(monitor, "fetch_openrouter_models", lambda: [m])
    monkeypatch.setattr(monitor, "summarize_models",
                        lambda models, *a, **k: "<b>Bad</b> — <i>x</i> <script>alert</script>")
    alerts, runs = [], []
    monkeypatch.setattr(monitor, "send_ops_alert", lambda t: alerts.append(t) or True)
    monkeypatch.setattr(monitor, "record_publish_run", lambda *a, **k: runs.append(a) or True)
    monkeypatch.setattr(monitor, "ping_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "send_telegram_post", lambda m: (_ for _ in ()).throw(AssertionError("preview must not send")))

    rc = monitor.main()
    assert rc == 0
    assert alerts == [], f"preview sent ops alerts: {alerts}"
    assert runs == [], f"preview wrote publish_runs: {runs}"


# ── LLM model fallback chain (2026-06-16: Ollama catalog churn resilience) ──

def test_llm_falls_through_to_secondary_model(monkeypatch):
    # Primary returns empty (model vanished/degraded) → secondary model is tried
    # and its output is used, rather than dropping to the bare template.
    monkeypatch.setattr(monitor, "LLM_API_KEY", "k")
    monkeypatch.setattr(monitor, "LLM_MODEL", "primary-model")
    monkeypatch.setattr(monitor, "LLM_MODEL_FALLBACK", "backup-model")

    def fake_post(url, json, headers, timeout):
        model = json["model"]
        fake = MagicMock(); fake.raise_for_status = lambda: None
        if model == "primary-model":
            fake.json.return_value = {"choices": [{"message": {"content": ""}}]}
        else:
            fake.json.return_value = {"choices": [{"message": {"content":
                "<b>X</b> — <i>y</i> <a href=\"u\">→ S</a>"}}]}
        return fake
    monkeypatch.setattr(monitor.requests, "post", fake_post)

    msg = monitor.summarize_models([_model("acme/X")])
    assert "ModelBytes Digest" in msg
    assert monitor.LAST_LLM_MODEL == "backup-model"


def test_llm_template_when_all_models_fail(monkeypatch):
    monkeypatch.setattr(monitor, "LLM_API_KEY", "k")
    monkeypatch.setattr(monitor, "LLM_MODEL", "primary-model")
    monkeypatch.setattr(monitor, "LLM_MODEL_FALLBACK", "backup-model")
    def boom(url, json, headers, timeout):
        raise RuntimeError("model not found")
    monkeypatch.setattr(monitor.requests, "post", boom)
    msg = monitor.summarize_models([_model("acme/X")])
    assert "items tracked today" in msg  # deterministic template
    assert monitor.LAST_LLM_MODEL is None


def test_primary_used_when_it_works(monkeypatch):
    monkeypatch.setattr(monitor, "LLM_API_KEY", "k")
    monkeypatch.setattr(monitor, "LLM_MODEL", "primary-model")
    monkeypatch.setattr(monitor, "LLM_MODEL_FALLBACK", "backup-model")
    calls = []
    def fake_post(url, json, headers, timeout):
        calls.append(json["model"])
        fake = MagicMock(); fake.raise_for_status = lambda: None
        fake.json.return_value = {"choices": [{"message": {"content":
            "<b>X</b> — <i>y</i> <a href=\"u\">→ S</a>"}}]}
        return fake
    monkeypatch.setattr(monitor.requests, "post", fake_post)
    monitor.summarize_models([_model("acme/X")])
    assert calls == ["primary-model"]  # backup never called when primary works
    assert monitor.LAST_LLM_MODEL == "primary-model"


# ── Exit code on deterministic blocks (2026-06-19 incident) ────────────────

def test_qa_blocked_fallback_exits_zero_not_one(monkeypatch, tmp_path):
    # 2026-06-19 incident: a fallback digest tripped the stale-release gate,
    # main() returned 1, and Railway marked the job Crashed and re-ran it 3×
    # (re-firing the same ops alert each time, per the ON_FAILURE policy). A
    # QA block is a correct FINAL decision — the same fetch will trip the same
    # gate on retry — so it must exit 0. The ops alert + the 'blocked'
    # publish_run row + the heartbeat /fail are the complete record.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["monitor.py"])  # live (non-preview)
    monkeypatch.setattr(monitor, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(monitor, "TELEGRAM_CHANNEL_ID", "c")
    monkeypatch.setattr(monitor, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(monitor, "try_post_pending_curated", lambda: False)
    monkeypatch.setattr(monitor, "init_database", lambda: None)
    monkeypatch.setattr(monitor, "load_seen_models", lambda: {"seed/x"})
    monkeypatch.setattr(monitor, "save_seen_models", lambda s: None)
    for f in ["fetch_ollama_models", "fetch_huggingface_trending",
              "fetch_major_orgs", "fetch_hf_text_generation"]:
        monkeypatch.setattr(monitor, f, lambda: [])
    m = _model("acme/Stale-Model")
    monkeypatch.setattr(monitor, "fetch_openrouter_models", lambda: [m])
    # Force a QA error via a malformed (<script>) summary body.
    monkeypatch.setattr(monitor, "summarize_models",
                        lambda models, *a, **k: "<b>Bad</b> <script>alert(1)</script>")
    monkeypatch.setattr(monitor, "discover_recent_releases", lambda *a, **k: "")
    monkeypatch.setattr(monitor, "_recent_digest_names", lambda *a, **k: [])
    monkeypatch.setattr(monitor, "enrich_with_hf_cards", lambda models: None)
    alerts, runs, heartbeats = [], [], []
    monkeypatch.setattr(monitor, "send_ops_alert", lambda t: alerts.append(t) or True)
    monkeypatch.setattr(monitor, "record_publish_run", lambda *a, **k: runs.append(a) or True)
    monkeypatch.setattr(monitor, "ping_heartbeat", lambda *a, **k: heartbeats.append(a))
    monkeypatch.setattr(monitor, "send_telegram_post",
                        lambda m: (_ for _ in ()).throw(AssertionError("QA block must not send")))

    rc = monitor.main()
    assert rc == 0, f"QA block must exit 0 (not crash Railway); got {rc}"
    assert alerts, "QA block must still send the ops alert"
    assert runs, "QA block must still record a blocked publish_run"
    assert heartbeats and heartbeats[0][0] is False, "QA block must heartbeat /fail"


def test_empty_state_refused_seed_exits_zero(monkeypatch, tmp_path):
    # Same logic for the empty-models-table refused-seed path: deterministic,
    # already alerted, retry won't help. Exit 0 to avoid the Railway crash-loop.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["monitor.py"])
    monkeypatch.setattr(monitor, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(monitor, "TELEGRAM_CHANNEL_ID", "c")
    monkeypatch.setattr(monitor, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(monitor, "ALLOW_SEED", False)
    monkeypatch.setattr(monitor, "try_post_pending_curated", lambda: False)
    monkeypatch.setattr(monitor, "init_database", lambda: None)
    monkeypatch.setattr(monitor, "load_seen_models", lambda: set())  # empty → refused
    monkeypatch.setattr(monitor, "save_seen_models", lambda s: None)
    for f in ["fetch_ollama_models", "fetch_huggingface_trending",
              "fetch_major_orgs", "fetch_hf_text_generation"]:
        monkeypatch.setattr(monitor, f, lambda: [])
    monkeypatch.setattr(monitor, "fetch_openrouter_models", lambda: [])
    alerts, runs = [], []
    monkeypatch.setattr(monitor, "send_ops_alert", lambda t: alerts.append(t) or True)
    monkeypatch.setattr(monitor, "record_publish_run", lambda *a, **k: runs.append(a) or True)
    monkeypatch.setattr(monitor, "ping_heartbeat", lambda *a, **k: None)

    rc = monitor.main()
    assert rc == 0, f"refused-seed must exit 0; got {rc}"
    assert alerts, "refused-seed must still alert"
    assert runs, "refused-seed must still record a blocked publish_run"

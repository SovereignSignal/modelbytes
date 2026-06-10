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
        '<b>Model A</b> — Released Jun 1. Great. <a href="u">→ Src</a>\n'
        '<b>Model B</b> — Released Jun 2. Good. <a href="u">→ Src</a>\n'
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
            '<b>Model 1</b> — Released Jun 1. The standout. <a href="u">→ Source</a>'
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
        '<b>🔓 Premier Open</b>\n<b>Model A</b> — Good. <a href="u">→ Source</a>\n\n'
        "📊 Surfaced 1 · scanned 5 today"
    )
    _, warnings, errors = monitor.validate_digest_for_publish(body)
    assert errors == []
    assert not any("footer is missing" in w for w in warnings)

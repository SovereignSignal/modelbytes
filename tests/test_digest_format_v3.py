"""Format v3 rendering tests for build_digest_message.

Spec: docs/superpowers/specs/2026-06-10-builder-digest-format-v3-design.md.
The deterministic template must render the same identity tiers the curator
uses (OPEN FRONTIER / CLOSED FRONTIER / SPECIALIZED / LOCAL), carry a per-entry
⚡/📦 availability tag derived from the source, and count "items" (not
"models") in the footer — so fallback days are visually indistinguishable
in structure from curated days.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


def _model(name, source="huggingface", provider="", **kw):
    return monitor.ModelRelease(
        name=name,
        provider=provider or monitor._resolve_provider(name.split("/")[0], name),
        source=source,
        url=f"https://example.com/{name}",
        description=kw.pop("description", "A test model."),
        is_open_source=kw.pop("is_open_source", None),
        unique_traits=kw.pop("unique_traits", []),
        likes=kw.pop("likes", 0),
        downloads=kw.pop("downloads", 0),
        **kw,
    )


def _sample_models():
    return [
        _model("meta-llama/Llama-3.3-70B-Instruct"),                      # open_frontier, HF
        _model("openai/gpt-4o", source="openrouter"),                     # closed_frontier, API
        _model("llama3.2", source="ollama", provider="Ollama"),           # local
    ]


def test_v3_tier_headers_render():
    msg = monitor.build_digest_message(_sample_models())
    assert "━━━ <b>OPEN FRONTIER</b> 🔓" in msg
    assert "━━━ <b>CLOSED FRONTIER</b> 🔒" in msg
    assert "━━━ <b>LOCAL</b> 🏠" in msg
    # Old v2 headers must be gone
    assert "PREMIER OPEN" not in msg
    assert "CLOSED GIANTS" not in msg
    assert "LOCAL READY" not in msg


def test_v3_empty_tiers_hidden():
    msg = monitor.build_digest_message([_model("meta-llama/Llama-3.3-70B-Instruct")])
    assert "OPEN FRONTIER" in msg
    assert "CLOSED FRONTIER" not in msg
    assert "SPECIALIZED" not in msg


def test_v3_availability_tags():
    msg = monitor.build_digest_message(_sample_models())
    # openrouter source → live API tag; HF → downloadable weights; ollama → pull-ready
    assert "⚡ API live · OpenRouter" in msg
    assert "📦 Open weights · HF" in msg
    assert "📦 Ollama pull-ready" in msg


def test_v3_footer_counts_items():
    msg = monitor.build_digest_message(_sample_models())
    assert "items tracked today" in msg
    assert "models tracked today" not in msg


def test_v3_footer_accepted_by_validation():
    msg = monitor.build_digest_message(_sample_models())
    _, warnings, errors = monitor.validate_digest_for_publish(msg)
    assert not errors
    assert "tracked-model footer is missing" not in warnings

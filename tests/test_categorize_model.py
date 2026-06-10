"""Golden tests for categorize_model — the most editorial-significant function.

Closes the audit B13 gap that the PR curator's review of PR #8 (grok-4 fix)
flagged: categorize_model has no tests despite being the function the supervisor
routine edits autonomously. A future supervisor auto-commit that accidentally
moved a string between tier lists would silently change channel output.

Format v3 (2026-06-10, docs/superpowers/specs/2026-06-10-builder-digest-format-v3-design.md):
identity tiers — open_frontier / closed_frontier / specialized / local / other.
The old reasoning/coding/image_gen/audio sub-tiers all collapse into
"specialized"; local_ready is now "local". WATCH is curator-only (the
deterministic pipeline can't see announced-but-unshipped models).

Cases prioritize the ones most likely to be broken by supervisor edits:
- open_frontier: open-weight major-lab models (Llama, Qwen, DeepSeek, Mistral)
- closed_frontier: openai / anthropic / google / xAI proprietary
- the grok-4 case specifically (the bug PR #8 fixed; this test prevents
  re-introduction by a future tier-list edit)
- specialized: one canonical case each for reasoning, coding, image, audio names
- local: ollama source
- other: high-engagement unknown org fallback
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


def _model(
    name: str,
    provider: str = "",
    source: str = "huggingface",
    is_open_source=None,
    likes: int = 0,
    downloads: int = 0,
    unique_traits=None,
) -> monitor.ModelRelease:
    return monitor.ModelRelease(
        name=name,
        provider=provider or monitor._resolve_provider(name.split("/")[0], name),
        source=source,
        url=f"https://example.com/{name}",
        description="",
        is_open_source=is_open_source,
        unique_traits=unique_traits or [],
        likes=likes,
        downloads=downloads,
    )


# ---------- open_frontier ----------

def test_meta_llama_3_3_open_frontier():
    assert monitor.categorize_model(_model("meta-llama/Llama-3.3-70B-Instruct")) == "open_frontier"


def test_qwen3_open_frontier():
    assert monitor.categorize_model(_model("Qwen/Qwen3-72B")) == "open_frontier"


def test_deepseek_v3_open_frontier():
    assert monitor.categorize_model(_model("deepseek-ai/DeepSeek-V3")) == "open_frontier"


def test_mistral_provider_open_frontier():
    """Even without 'mistral' in the name, mistralai provider → open_frontier."""
    m = _model("mistralai/some-new-release")
    assert monitor.categorize_model(m) == "open_frontier"


# ---------- closed_frontier ----------

def test_gpt_4_closed_frontier():
    assert monitor.categorize_model(_model("openai/gpt-4o")) == "closed_frontier"


def test_claude_3_closed_frontier():
    assert monitor.categorize_model(_model("anthropic/claude-3-opus")) == "closed_frontier"


def test_gemini_2_closed_frontier():
    assert monitor.categorize_model(_model("google/gemini-2-flash")) == "closed_frontier"


def test_grok_4_must_be_closed_frontier():
    """Audit B13 / PR #8 regression guard: grok-4 is xAI's proprietary closed
    model. Before PR #8 fixed it, 'grok' was in the premier list and substring-
    matched 'grok-4', sending it to the open tier. A future edit that re-adds
    'grok' (without -2/-3 specificity) to the open list would re-introduce
    the bug.
    """
    # Note: provider extraction from 'x-ai/grok-4' returns "xAI" from
    # PROVIDER_NAMES; lowercased "xai" is NOT in the closed-providers list
    # (which has "openai", "anthropic", "google"). So this test relies on
    # the NAME-based match against the closed list containing 'grok-4'.
    assert monitor.categorize_model(_model("x-ai/grok-4")) == "closed_frontier"


def test_grok_3_open_frontier():
    """grok-3 should match the open list (open weights), distinct from grok-4."""
    assert monitor.categorize_model(_model("x-ai/grok-3")) == "open_frontier"


# ---------- specialized (reasoning / coding / image / audio names) ----------

def test_reasoning_name_specialized():
    m = _model("research-lab/agent-reasoning-bench")
    assert monitor.categorize_model(m) == "specialized"


def test_codestral_specialized():
    """codestral with a neutral provider → specialized (coding keyword).
    mistralai as provider would win the open_frontier provider check first."""
    m = _model("nonprofit-coder/codestral-variant", provider="Nonprofit")
    assert monitor.categorize_model(m) == "specialized"


def test_flux_specialized():
    m = _model("black-forest-labs/FLUX.1-dev", provider="Black Forest Labs")
    assert monitor.categorize_model(m) == "specialized"


def test_supertone_specialized():
    m = _model("Supertone/Supertonic-v2", provider="Supertone")
    assert monitor.categorize_model(m) == "specialized"


# ---------- local / other ----------

def test_ollama_source_local():
    m = _model("llama3.2", source="ollama", provider="Ollama")
    assert monitor.categorize_model(m) == "local"


def test_high_engagement_unknown_other():
    m = _model("smallorg/some-novel-model", provider="smallorg", likes=600)
    assert monitor.categorize_model(m) == "other"


# ---------- sig_org_map ----------

def test_tencentarc_specialized():
    """tencentarc is in sig_org_map → specialized (image), regardless of name.
    sig_org_map relies on the RAW author slug, not the display name."""
    m_raw = monitor.ModelRelease(
        name="tencentarc/something",
        provider="tencentarc",  # raw slug, not display name
        source="huggingface",
        url="https://example.com/tencentarc/something",
        description="",
        is_open_source=None,
        unique_traits=[],
        likes=0,
        downloads=0,
    )
    assert monitor.categorize_model(m_raw) == "specialized"

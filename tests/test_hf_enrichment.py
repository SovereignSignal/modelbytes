"""HuggingFace model-card enrichment for the inline path (2026-06-13).

The inline digest writes from API metadata + the model's training knowledge but
doesn't browse the web, so hard-facts (params/license/context/benchmarks) were
thin vs the retired Claude curator. fetch_hf_card pulls each top candidate's HF
model-card metadata so deepseek-v4-pro has real specs to write from. Must be
bounded and degrade silently — a card fetch failure produces a thinner entry,
never a crash.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


_SAMPLE_CARD = {
    "id": "google/gemma-4-12B-it",
    "cardData": {
        "license": "gemma",
        "model-index": [{
            "name": "gemma-4-12B-it",
            "results": [
                {"dataset": {"name": "MMLU"}, "metrics": [{"type": "acc", "value": 85.2}]},
                {"dataset": {"name": "GSM8K"}, "metrics": [{"type": "acc", "value": 92.1}]},
            ],
        }],
    },
    "safetensors": {"total": 12000000000},
    "config": {"max_position_embeddings": 131072},
}


def _resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    return r


def test_format_param_count():
    assert monitor._format_param_count(12000000000) == "12B"
    assert monitor._format_param_count(760000000) == "760M"
    assert monitor._format_param_count(0) is None
    assert monitor._format_param_count(None) is None


def test_fetch_hf_card_extracts_facts(monkeypatch):
    monkeypatch.setattr(monitor, "_http_get", lambda url, name, **kw: _resp(_SAMPLE_CARD))
    card = monitor.fetch_hf_card("google/gemma-4-12B-it")
    assert card["license"] == "gemma"
    assert card["total_parameters"] == "12B"
    assert card["context_window"] == 131072
    assert "MMLU" in card["benchmarks"] and "85.2" in card["benchmarks"]
    assert "GSM8K" in card["benchmarks"]


def test_fetch_hf_card_graceful_on_failure(monkeypatch):
    def boom(url, name, **kw):
        raise RuntimeError("404")
    monkeypatch.setattr(monitor, "_http_get", boom)
    assert monitor.fetch_hf_card("x/y") == {}


def test_fetch_hf_card_partial_metadata(monkeypatch):
    # A sparse card (license only) must not invent the missing fields.
    monkeypatch.setattr(monitor, "_http_get",
                        lambda url, name, **kw: _resp({"cardData": {"license": "apache-2.0"}}))
    card = monitor.fetch_hf_card("x/y")
    assert card == {"license": "apache-2.0"}


def test_enrich_fills_gaps_without_overwriting(monkeypatch):
    monkeypatch.setattr(monitor, "fetch_hf_card",
                        lambda mid: {"license": "gemma", "total_parameters": "12B",
                                     "context_window": 131072, "benchmarks": "MMLU 85.2"})
    m = monitor.ModelRelease(
        name="google/gemma-4-12B-it", provider="Google", source="huggingface-org",
        url="https://huggingface.co/google/gemma-4-12B-it", description="x",
        license="Already-Set")  # pre-existing license must NOT be overwritten
    monitor.enrich_with_hf_cards([m])
    assert m.license == "Already-Set"          # preserved
    assert m.total_parameters == "12B"          # filled
    assert m.context_window == 131072           # filled
    assert m.card_facts == "MMLU 85.2"          # benchmarks attached


def test_enrich_skips_non_hf_and_is_graceful(monkeypatch):
    calls = []
    monkeypatch.setattr(monitor, "fetch_hf_card", lambda mid: calls.append(mid) or {})
    api = monitor.ModelRelease(name="openai/gpt-x", provider="OpenAI",
                               source="openrouter", url="u", description="x")
    monitor.enrich_with_hf_cards([api])
    assert calls == []  # openrouter source → no HF card fetch


def test_enriched_facts_reach_the_llm_prompt(monkeypatch):
    captured = {}
    class FakeResp:
        def raise_for_status(self): return None
        def json(self): return {"choices": [{"message": {"content": "x"}}]}
    monkeypatch.setattr(monitor, "LLM_API_KEY", "k")
    monkeypatch.setattr(monitor.requests, "post",
                        lambda url, **kw: captured.update(kw) or FakeResp())
    m = monitor.ModelRelease(name="google/gemma-4-12B-it", provider="Google",
                             source="huggingface-org", url="u", description="x",
                             total_parameters="12B", card_facts="MMLU 85.2, GSM8K 92.1")
    monitor.summarize_models([m])
    prompt = captured["json"]["messages"][0]["content"]
    assert "MMLU 85.2" in prompt


# ── data-trust guards (2026-06-13 preview surfaced a 676K-for-a-32B-model bug) ──

def test_param_size_from_name():
    assert monitor._param_size_from_name("open-thoughts/OpenThinkerAgent-32B") == "32B"
    assert monitor._param_size_from_name("google/gemma-4-12B-it") == "12B"
    assert monitor._param_size_from_name("x/DiffusionGemma-26B-A4B-it") == "26B"  # total, not active
    assert monitor._param_size_from_name("nvidia/NV-KERMT-70M-v2") == "70M"
    assert monitor._param_size_from_name("MiniMaxAI/MiniMax-M3") is None  # no size token


def test_param_size_from_name_lowercase():
    # 2026-06-22 incident: real HF IDs are almost always lowercase ('tmax-27b',
    # 'qwen35-9b'), but the parser only matched UPPERCASE B/M — so it returned
    # None for nearly every real model, the system marked params 'unknown',
    # and the LLM hallucinated specs ('27B' model shown as '3 M params').
    assert monitor._param_size_from_name("allenai/tmax-27b") == "27B"
    assert monitor._param_size_from_name("allenai/tmax-8b") == "8B"
    assert monitor._param_size_from_name("allenai/tmax-2b") == "2B"
    assert monitor._param_size_from_name("allenai/qwen35-9b-termigen") == "9B"
    assert monitor._param_size_from_name("qwen/qwen3-7b-instruct") == "7B"
    # mixed case still works, uppercase still works, largest token wins
    assert monitor._param_size_from_name("x/Foo-1B-70B") == "70B"
    assert monitor._param_size_from_name("x/Mini-760m") == "760M"
    # a trailing 'b' inside a word must not false-match (e.g. 'web', 'lab')
    assert monitor._param_size_from_name("x/derivelab") is None
    assert monitor._param_size_from_name("x/something-web") is None


def test_is_noise_model_coerces_string_engagement():
    # is_noise_model does `likes < 100` directly; a fetcher passing a string
    # (e.g. HF returning "123" once) would crash the fallback path with
    # TypeError, taking the whole publish down. Coerce to int defensively.
    # Known org (allenai) bypasses the engagement gate, so use an unknown org
    # to exercise the branch that reads likes/downloads.
    assert monitor.is_noise_model("someorg/whatever-9b", "someorg", [],
                                  downloads="5000", likes="50") in (True, False)
    # and None must not crash either (treated as 0)
    assert monitor.is_noise_model("someorg/whatever-9b", "someorg", [],
                                  downloads=None, likes=None) in (True, False)


def test_name_size_beats_partial_safetensors(monkeypatch):
    # 32B in the name, but safetensors reports a 676K adapter → name must win.
    monkeypatch.setattr(monitor, "_http_get", lambda url, name, **kw: _resp({
        "id": "open-thoughts/OpenThinkerAgent-32B",
        "safetensors": {"total": 676000},
        "cardData": {"license": "apache-2.0"},
    }))
    card = monitor.fetch_hf_card("open-thoughts/OpenThinkerAgent-32B")
    assert card["total_parameters"] == "32B"


def test_safetensors_used_when_name_has_no_size(monkeypatch):
    monkeypatch.setattr(monitor, "_http_get", lambda url, name, **kw: _resp({
        "id": "MiniMaxAI/MiniMax-M3", "safetensors": {"total": 427000000000},
    }))
    assert monitor.fetch_hf_card("MiniMaxAI/MiniMax-M3")["total_parameters"] == "427B"


def test_placeholder_license_suppressed(monkeypatch):
    monkeypatch.setattr(monitor, "_http_get", lambda url, name, **kw: _resp({
        "cardData": {"license": "other"}, "safetensors": {"total": 3800000000}}))
    card = monitor.fetch_hf_card("nvidia/LocateAnything-3B")
    assert "license" not in card

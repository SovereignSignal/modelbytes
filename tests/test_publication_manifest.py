"""Seen state follows the publication manifest, not the selected set.

2026-10-14 review, finding 1: main() selected up to 15 candidates while
summarize_models() rendered at most 12, and after a successful send the
whole selection was marked seen — so a capped, omitted, or truncated model
was skipped forever. Only the models present in the delivered text become
seen; the rest stay eligible for the next run.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor

WORDS = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf",
         "Hotel", "India", "Juliet", "Kilo", "Lima", "Mike", "November",
         "Oscar"]


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _model(word: str) -> monitor.ModelRelease:
    name = f"meta-llama/Llama-5-{word}-8B"
    return monitor.ModelRelease(
        name=name,
        provider="Meta",
        source="huggingface",
        url=f"https://huggingface.co/{name}",
        description="Test model",
        release_date=_today(),
        is_open_source=True,
    )


def _candidates():
    return [_model(w) for w in WORDS]


def _writer(words, prose="A useful open model for builders."):
    body = "TAKE: NONE\nOPEN FRONTIER\n" + "\n".join(
        f"ITEM: Llama 5 {w} 8B\nPROSE: {prose}" for w in words)
    return lambda model, prompt, max_tokens=None: body


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Live-mode main() with every network edge stubbed. Real renderer + QA."""
    state = {"seen": {"sentinel/seed"}, "saved": [], "sent": [],
             "fetch": _candidates}

    monkeypatch.setattr(sys, "argv", ["monitor.py"])
    monkeypatch.setattr(monitor, "LLM_API_KEY", "")
    monkeypatch.setattr(monitor, "ENRICH_HF_CARDS", False)
    monkeypatch.setattr(monitor, "run_alphasignal_crosscheck", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "try_post_pending_curated", lambda: False)
    monkeypatch.setattr(monitor, "init_database", lambda: None)
    monkeypatch.setattr(monitor, "load_seen_models", lambda: set(state["seen"]))
    monkeypatch.setattr(monitor, "fetch_openrouter_models", lambda: state["fetch"]())
    for name in ("fetch_ollama_models", "fetch_huggingface_trending",
                 "fetch_major_orgs", "fetch_hf_text_generation"):
        monkeypatch.setattr(monitor, name, lambda: [])
    monkeypatch.setattr(monitor, "fetch_artificial_analysis_models", lambda *a, **k: [])
    monkeypatch.setattr(monitor, "fetch_testingcatalog_models", lambda *a, **k: [])
    monkeypatch.setattr(monitor, "fetch_artificial_analysis_x_models", lambda *a, **k: [])
    monkeypatch.setattr(monitor, "discover_recent_releases", lambda *a, **k: "")
    monkeypatch.setattr(monitor, "_recent_digest_names", lambda today: [])
    monkeypatch.setattr(monitor, "mark_posted_digest", lambda *a, **k: True)
    monkeypatch.setattr(monitor, "send_slack_post", lambda message: False)
    monkeypatch.setattr(monitor, "_push_ai_wire", lambda *a, **k: None)

    def _save(models):
        state["saved"].append(set(models))
        state["seen"] = set(models)

    def _send(message):
        state["sent"].append(message)
        return True

    monkeypatch.setattr(monitor, "save_seen_models", _save)
    monkeypatch.setattr(monitor, "send_telegram_post", _send)
    (tmp_path / "pending").mkdir()
    monkeypatch.chdir(tmp_path)
    return state


def _names(words):
    return {f"meta-llama/Llama-5-{w}-8B" for w in words}


def test_selection_and_renderer_share_one_cap():
    out = monitor.summarize_models(_candidates(), "", [], today=_today())
    rendered = monitor.build_publication_manifest(_candidates(), out)
    assert len(rendered.models) == monitor.DIGEST_LIMIT


def test_fifteen_candidates_only_twelve_delivered_become_seen(harness):
    assert monitor.DIGEST_LIMIT == 12
    assert monitor.main() == 0
    seen = harness["saved"][-1]
    assert _names(WORDS[:12]) <= seen
    assert not (_names(WORDS[12:]) & seen), "unpublished candidates were burned"

    # The 3 stay eligible: the next ordinary run publishes them.
    assert monitor.main() == 0
    assert len(harness["sent"]) == 2
    for word in WORDS[12:]:
        assert f"Llama 5 {word} 8B" in harness["sent"][1]
    assert _names(WORDS) <= harness["saved"][-1]


def test_writer_omissions_stay_eligible(harness, monkeypatch):
    monkeypatch.setattr(monitor, "LLM_API_KEY", "k")
    monkeypatch.setattr(monitor, "_call_llm", _writer(WORDS[:9]))
    assert monitor.main() == 0
    seen = harness["saved"][-1]
    assert _names(WORDS[:9]) <= seen
    assert not (_names(WORDS[9:]) & seen)


def test_length_limited_output_marks_only_the_delivered_prefix(harness, monkeypatch):
    prose = ("A dense open model for builders who run local inference on a "
             "single accelerator and need predictable latency under load "
             "across long agentic sessions with tool calls and retrieval, "
             "tuned for structured output and multilingual code review. ") * 2
    monkeypatch.setattr(monitor, "LLM_API_KEY", "k")
    monkeypatch.setattr(monitor, "_call_llm", _writer(WORDS[:12], prose=prose.strip()))
    assert monitor.main() == 0
    sent = harness["sent"][-1]
    assert len(sent) > monitor.TELEGRAM_MAX_CHARS
    delivered = monitor._truncate_for_telegram(sent)
    shown = [w for w in WORDS[:12] if f"Llama 5 {w} 8B" in delivered]
    cut = [w for w in WORDS[:12] if w not in shown]
    assert shown and cut, "fixture must straddle the 4096-char cut"
    seen = harness["saved"][-1]
    assert _names(shown) <= seen
    assert not (_names(cut + WORDS[12:]) & seen)


def test_published_family_entry_marks_its_variants_seen():
    variants = [
        monitor.ModelRelease(
            name=f"someorg/qwen35-9b-{tag}", provider="someorg",
            source="huggingface", url=f"https://huggingface.co/someorg/qwen35-9b-{tag}",
            description="variant", downloads=10, release_date=_today())
        for tag in ("math", "law", "medical")
    ]
    family = monitor.collapse_variants(variants)
    assert len(family) == 1
    assert set(family[0].variants) == {v.name for v in variants}
    body = (f"<b>{family[0].name}</b> — <i>family.</i> "
            f'<a href="{family[0].url}">→ HF</a>')
    manifest = monitor.build_publication_manifest(family, body)
    assert manifest.seen_ids == {family[0].name} | {v.name for v in variants}
    assert monitor.build_publication_manifest(family, "unrelated").seen_ids == set()

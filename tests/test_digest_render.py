"""Deterministic digest rendering, freshness window, and Take guards.

The writer supplies the Take and each item's one-line prose. Params, license,
release date, availability, and link labels are rendered in code so a fallback
model cannot change the format. Fixtures quote the public channel text from
digests #217–#225 (Sep 28–Oct 6, 2026).
"""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


def _model(name, **kw):
    fields = dict(
        provider=kw.pop("provider", "Test"),
        source=kw.pop("source", "huggingface"),
        url=kw.pop("url", "https://huggingface.co/" + name),
        description=kw.pop("description", "A capable open model."),
    )
    fields.update(kw)
    return monitor.ModelRelease(name=name, **fields)


def test_link_label_uses_host_not_source():
    assert monitor.link_label("https://huggingface.co/Hcompany/Holo4-27B") == "HF"
    assert monitor.link_label("https://hf.co/google/gemma") == "HF"
    assert monitor.link_label("https://openrouter.ai/models/openai/gpt-6.1-sol") == "OpenRouter"
    assert monitor.link_label("https://mistral.ai/news/mistral-large-4/") == "mistral.ai"
    assert monitor.link_label("https://www.promptzone.com/ai-model-releases") == "promptzone.com"
    assert monitor.link_label("https://tech-insider.org/chatgpt-vs-claude") == "tech-insider.org"
    assert monitor.link_label("https://ollama.com/library/llama3.2") == "Ollama"


def test_metadata_line_is_one_fixed_order():
    model = _model(
        "BAAI/AREX-2",
        total_parameters="27.4B",
        license="Apache 2.0",
        release_date="2026-09-29",
        context_window=8192,
        description="ignored",
    )
    line = monitor.metadata_line(model)
    assert line == "27.4B total · 8K context · Apache-2.0 · Released Sep 29"


def test_metadata_skips_missing_fields_and_placeholder_license():
    # #221 Ling 3.1 Flash: prose said "Free" and the metadata said
    # "License Closed/API". Closed/API is a placeholder, not a license.
    model = _model(
        "inclusionai/ling-3.1-flash",
        source="openrouter",
        url="https://openrouter.ai/models/inclusionai/ling-3.1-flash",
        is_open_source=False,
        license="Closed/API",
        pricing_input=0,
        pricing_output=0,
        total_parameters="560B",
        active_parameters="25B",
        context_window=262144,
        release_date="2026-10-02",
    )
    line = monitor.metadata_line(model)
    assert line.startswith("560B total / 25B active · 262K context · FREE · Released Oct 2")
    assert "Closed" not in line
    assert "License" not in line


def test_diarization_prose_does_not_repeat_metadata():
    # #223: "Speaker diarization model on Gemma 4 E4B, Apache-2.0, 8B total
    # params. 8B params, Apache-2.0."
    model = _model(
        "google/DiarizationLM-Gemma-4-E4B-v1",
        total_parameters="8B",
        license="apache-2.0",
        release_date="2026-10-04",
        description=(
            "Speaker diarization model on Gemma 4 E4B, Apache-2.0, "
            "8B total params. 8B params, Apache-2.0."
        ),
    )
    prose = monitor.clean_item_prose(model.description, model)
    assert prose == "Speaker diarization model on Gemma 4 E4B."
    rendered = monitor.render_model_entry(model, model.description)
    assert "<i>Speaker diarization model on Gemma 4 E4B.</i>" in rendered
    assert rendered.count("8B") == 1
    assert rendered.count("Apache-2.0") == 1
    assert "→ HF</a>" in rendered
    assert "→ Source" not in rendered
    assert "Released Oct 4." in rendered
    assert "📦 Open weights · HF." in rendered


def test_mistral_conflicting_context_is_dropped_from_prose():
    # #225: description said "a 512K window" while the card said 524K context.
    model = _model(
        "mistralai/mistral-large-4",
        source="openrouter",
        url="https://openrouter.ai/models/mistralai/mistral-large-4",
        canonical_url="https://mistral.ai/news/mistral-large-4/",
        context_window=524288,
        pricing_input=0.68,
        pricing_output=2.09,
        release_date="2026-10-06",
        description=(
            "Frontier-class reasoning, coding, and agentic work at "
            "$0.68/$2.09 per 1M tokens, with a 512K window that swallows whole repos."
        ),
    )
    prose = monitor.clean_item_prose(model.description, model)
    assert "512" not in prose
    assert "Frontier-class reasoning, coding, and agentic work." in prose
    rendered = monitor.render_model_entry(model, model.description)
    assert "512" not in rendered
    assert "524K context" in rendered
    assert "→ mistral.ai</a>" in rendered
    assert rendered.index("524K context") < rendered.index("→ mistral.ai")
    # Period sits between the availability tag and the link (#225 dropped it).
    assert "OpenRouter. <a href=" in rendered


def test_benchmark_scores_are_not_rendered():
    # #224 dumped "JevBench public 0.7229 · boardgame 0.822 · contractnli 0.872"
    # into the metadata line. Benchmarks stay off the rendered line.
    model = _model(
        "StrandsAgents/strands-decider-2B",
        total_parameters="2B",
        license="apache-2.0",
        release_date="2026-10-05",
        card_facts="JevBench public 0.7229, boardgame 0.822, contractnli 0.872",
        description=(
            "An agent decider shrunk to 2B so routing calls run on your own hardware. "
            "JevBench public 0.7229."
        ),
    )
    rendered = monitor.render_model_entry(model, model.description)
    assert "0.7229" not in rendered
    assert "JevBench" not in rendered
    assert "2B total" in rendered
    assert "card_facts" not in rendered


def test_two_writer_orders_render_the_same_metadata():
    model = _model(
        "BAAI/AREX-2",
        total_parameters="27.4B",
        license="Apache-2.0",
        release_date="2026-09-29",
        description="BAAI's image-text-to-text model under Apache 2.0.",
    )
    # #217 order vs #219 order — the writer used both in the same week.
    a = monitor.render_model_entry(
        model, "Open multimodal weights. 27B total · CC BY-NC 4.0 · Released Sep 24.")
    b = monitor.render_model_entry(
        model, "Open multimodal weights. Released Sep 29 · 27.4B params · Apache 2.0.")
    for rendered in (a, b):
        assert "27.4B total · Apache-2.0 · Released Sep 29." in rendered
        assert "<i>Open multimodal weights.</i>" in rendered
        assert "CC BY-NC" not in rendered
        assert "27B total" not in rendered


def test_take_italics_are_applied_in_code():
    models = [_model("xai/grok-4.6", release_date="2026-09-28")]
    plain = "Open weights and a managed Grok landed in the same week."
    kept, reason = monitor.guard_take(plain, models, today="2026-09-28")
    assert reason is None
    assert kept == plain
    html = monitor.format_take_html(plain)
    assert html == f"<i>{plain}</i>"
    # Already-italic input is not double-wrapped.
    assert monitor.format_take_html(f"<i>{plain}</i>") == f"<i>{plain}</i>"


def test_take_dropped_when_it_names_an_unlisted_model():
    # #219: "OpenAI's shelved GPT-6.1 Astra" — Astra was not an item.
    # GPT-6.1 Sol was. Astra was a listicle codename the day before.
    models = [
        _model("BAAI/AREX-2", provider="BAAI", release_date="2026-09-29"),
        _model("openai/gpt-6.1-sol", provider="OpenAI",
               source="openrouter",
               url="https://openrouter.ai/models/openai/gpt-6.1-sol",
               release_date="2026-09-29"),
        _model("inception/mercury-voice", provider="Inception",
               release_date="2026-09-29"),
    ]
    take = ("Today's releases split between incremental closed upgrades and "
            "open multimodal weights, while OpenAI's shelved GPT-6.1 Astra "
            "signals safety gates tightening at the top.")
    kept, reason = monitor.guard_take(take, models, today="2026-09-30")
    assert kept == ""
    assert reason
    assert "Astra" in reason


def test_take_kept_when_it_only_names_listed_items():
    # #217's Take mentions Grok, which is in the digest, and no other product.
    models = [
        _model("Hcompany/Holo4-27B", release_date="2026-09-24"),
        _model("xai/grok-4.6", provider="xAI", release_date="2026-09-28",
               source="openrouter",
               url="https://openrouter.ai/x-ai/grok-4.6"),
    ]
    take = ("Today's pattern is open multimodal and RL-tuned weights landing "
            "alongside a managed Grok, which means builders get cheaper local "
            "options for vision and a high-capacity API fallback in the same week.")
    kept, reason = monitor.guard_take(take, models, today="2026-09-28")
    assert reason is None
    assert kept == take


def test_take_dropped_when_a_number_conflicts():
    # #225 Take repeated "512K" while Mistral Large 4's context is 524288.
    models = [_model(
        "mistralai/mistral-large-4",
        provider="Mistral",
        context_window=524288,
        release_date="2026-10-06",
        source="openrouter",
        url="https://openrouter.ai/models/mistralai/mistral-large-4",
    )]
    take = ("Mistral Large 4 is the day's only frontier drop — everything else "
            "new is open-weight and narrow, so the practical move is wiring a "
            "cheap 512K-context API into your agent loop.")
    kept, reason = monitor.guard_take(take, models, today="2026-10-06")
    assert kept == ""
    assert reason
    assert "512K" in reason


def test_digest_window_drops_four_day_old_date_keeps_three():
    # Monday still covers Friday (age 3). Thursday on Monday is age 4.
    summary = (
        '<b>Catch</b> — <i>d</i>. Released Sep 24. <a href="https://x/1">→ S</a>\n'
        '<b>Edge</b> — <i>d</i>. Released Sep 25. <a href="https://x/2">→ S</a>\n'
        '<b>New</b> — <i>d</i>. Released Sep 26. <a href="https://x/3">→ S</a>')
    cleaned, dropped = monitor._strip_stale_entries(summary, today="2026-09-28")
    assert any(label.startswith("Catch") for label in dropped)
    assert "Catch" not in cleaned
    assert "Edge" in cleaned  # Sep 25 is 3 days before Sep 28
    assert "New" in cleaned


def test_channel_stale_examples_are_outside_the_digest_window():
    # #217 Holo4 "Released Sep 24" on the Sep 28 digest (4 days).
    assert monitor.digest_release_is_stale("2026-09-24", today="2026-09-28") is True
    # #225 Holotron4 "Released Sep 28" and LTX 2.5 "Released Oct 2" on Oct 6.
    assert monitor.digest_release_is_stale("2026-09-28", today="2026-10-06") is True
    assert monitor.digest_release_is_stale("2026-10-02", today="2026-10-06") is True
    # #221 Gemini 4 Argon "Released Sep 30" on Oct 2 is 2 days — still news.
    assert monitor.digest_release_is_stale("2026-09-30", today="2026-10-02") is False
    # Unknown dates are not evidence of staleness.
    assert monitor.digest_release_is_stale(None, today="2026-10-06") is False
    # Catalog backfill / release-forwarding keep the wider default window.
    assert monitor.is_stale_release("2026-09-24", today="2026-09-28") is False


def test_stale_models_are_omitted_from_the_template():
    fresh = _model("google/gemma-4-12B-it", release_date="2026-10-06",
                   total_parameters="12B", license="apache-2.0",
                   is_open_source=True,
                   description="Open weights a builder can run locally.")
    stale = _model("Hcompany/Holotron4-30B-A3B", release_date="2026-09-28",
                   total_parameters="30B")
    msg = monitor.build_digest_message([fresh, stale], today="2026-10-06")
    assert "Holotron" not in msg
    assert "Gemma 4 12B" in msg
    assert "12B total · Apache-2.0 · Released Oct 6." in msg
    assert "→ HF</a>" in msg
    assert "→ Source" not in msg


def test_listicle_hosts_are_not_discovery_candidates():
    astra = _model(
        "Astra",
        source="discovery",
        url="https://tech-insider.org/chatgpt-vs-claude-vs-gemini-vs-grok-reasoning-2026",
        description="Gemini's September update",
        release_date="2026-10-06",
    )
    primary = _model(
        "mistralai/mistral-large-4",
        source="discovery",
        url="https://mistral.ai/news/mistral-large-4/",
        description="Mistral announces Mistral Large 4.",
        release_date="2026-10-06",
    )
    kept = monitor._filter_discovery_models(
        [astra, primary], recent_names=[], seen=set(), today="2026-10-06",
        max_age_days=monitor.DIGEST_FRESHNESS_DAYS)
    assert [m.name for m in kept] == ["mistralai/mistral-large-4"]


def test_prompt_forbids_unlisted_take_claims_and_invented_numbers(monkeypatch):
    captured = {}

    class FR:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "TAKE: NONE\n"}}]}

    def fake_post(url, json, headers, timeout):
        captured["prompt"] = json["messages"][0]["content"]
        return FR()

    monkeypatch.setattr(monitor, "LLM_API_KEY", "k")
    monkeypatch.setattr(monitor.requests, "post", fake_post)
    monitor.summarize_models(
        [_model("acme/Model-1", release_date="2026-10-06")],
        today="2026-10-06")
    prompt = captured["prompt"]
    assert "Take line" in prompt
    assert "only models you also list" in prompt
    assert "Do not infer or invent parameter counts" in prompt
    assert "listicle" in prompt.lower()
    assert "3 day" in prompt.lower() or "3-day" in prompt.lower()


def test_summarize_rewrites_writer_html_the_same_way_for_both_models(monkeypatch):
    """#225 drift: deepseek dropped the Take italics and the period before
    the link. A second writer that emits the older order must render identically.
    """
    model = _model(
        "google/DiarizationLM-Gemma-4-E4B-v1",
        total_parameters="8B",
        license="Apache-2.0",
        release_date="2026-10-04",
        url="https://huggingface.co/google/DiarizationLM-Gemma-4-E4B-v1",
    )
    bodies = [
        # Writer A, #223 shape: facts in the italic sentence AND again after it.
        (
            "<i>October opens with small specialised models.</i>\n\n"
            "━━━ <b>SPECIALIZED</b> 🎯\n"
            "<b>DiarizationLM Gemma 4 E4B</b> — <i>Speaker diarization model on "
            "Gemma 4 E4B, Apache-2.0, 8B total params.</i> 8B params, Apache-2.0. "
            'Released Oct 4. 📦 Open weights · HF. <a href="https://huggingface.co/google/DiarizationLM-Gemma-4-E4B-v1">→ Source</a>'
        ),
        # Writer B, #225 shape: no Take italics, no period, different fact order.
        (
            "October opens with small specialised models.\n\n"
            "━━━ <b>SPECIALIZED</b> 🎯\n"
            "<b>DiarizationLM Gemma 4 E4B</b> — Speaker diarization model on "
            "Gemma 4 E4B, Apache-2.0, 8B total params. "
            'Released Oct 4. License Apache-2.0. '
            '<a href="https://huggingface.co/google/DiarizationLM-Gemma-4-E4B-v1">→ Source</a>'
        ),
    ]
    rendered = []
    for body in bodies:
        fake = MagicMock()
        fake.raise_for_status = lambda: None
        fake.json.return_value = {"choices": [{"message": {"content": body}}]}
        monkeypatch.setattr(monitor, "LLM_API_KEY", "k")
        monkeypatch.setattr(monitor, "LLM_MODEL_FALLBACK", None)
        monkeypatch.setattr(monitor.requests, "post", lambda *a, **k: fake)
        msg = monitor.summarize_models([model], today="2026-10-04")
        rendered.append(msg)
    for msg in rendered:
        assert "<i>October opens with small specialised models.</i>" in msg
        assert "<i>Speaker diarization model on Gemma 4 E4B.</i>" in msg
        assert "8B total · Apache-2.0 · Released Oct 4." in msg
        assert "→ HF</a>" in msg
        assert "→ Source" not in msg
        assert msg.count("8B") == 1
        assert msg.count("Apache-2.0") == 1
    # The entry line is identical even though the writers disagreed.
    def _entry(msg):
        return next(line for line in msg.splitlines() if "DiarizationLM" in line)
    assert _entry(rendered[0]) == _entry(rendered[1])


def test_structured_blocks_render_the_same_entry_shape(monkeypatch):
    # The prompt asks for TAKE/ITEM/PROSE. That path must match the HTML rewrite.
    model = _model(
        "google/gemma-4-12B-it",
        total_parameters="12B",
        license="apache-2.0",
        release_date="2026-10-06",
        url="https://huggingface.co/google/gemma-4-12B-it",
        is_open_source=True,
    )
    body = (
        "TAKE: Open weights are the whole day.\n"
        "ITEM: Gemma 4 12B\n"
        "PROSE: Builders can run it without an API key.\n"
    )
    fake = MagicMock()
    fake.raise_for_status = lambda: None
    fake.json.return_value = {"choices": [{"message": {"content": body}}]}
    monkeypatch.setattr(monitor, "LLM_API_KEY", "k")
    monkeypatch.setattr(monitor, "LLM_MODEL_FALLBACK", None)
    monkeypatch.setattr(monitor.requests, "post", lambda *a, **k: fake)
    msg = monitor.summarize_models([model], today="2026-10-06")
    assert "<i>Open weights are the whole day.</i>" in msg
    assert "<i>Builders can run it without an API key.</i>" in msg
    assert "12B total · Apache-2.0 · Released Oct 6." in msg
    assert "→ HF</a>" in msg
    assert "━━━ <b>OPEN FRONTIER</b> 🔓" in msg
    assert "→ Source" not in msg


def test_summarize_drops_stale_and_bad_take_from_oct6_shape(monkeypatch, capsys):
    # #225 on Oct 6: Holotron4 (Sep 28) and LTX 2.5 (Oct 2) are outside the
    # window. The Take's 512K contradicts the 524K context field.
    mistral = _model(
        "mistralai/mistral-large-4",
        provider="Mistral",
        source="openrouter",
        url="https://openrouter.ai/models/mistralai/mistral-large-4",
        canonical_url="https://mistral.ai/news/mistral-large-4/",
        context_window=524288,
        pricing_input=0.68,
        pricing_output=2.09,
        release_date="2026-10-06",
        description="Frontier reasoning with a 512K window.",
    )
    holo = _model(
        "Hcompany/Holotron4-30B-A3B",
        total_parameters="30B",
        release_date="2026-09-28",
        url="https://huggingface.co/Hcompany/Holotron4-30B-A3B",
    )
    ltx = _model(
        "Lightricks/LTX-2.5",
        release_date="2026-10-02",
        url="https://huggingface.co/Lightricks/LTX-2.5",
        description="Image-to-video successor.",
    )
    body = (
        "Mistral Large 4 is the day's only frontier drop, so wire a cheap "
        "512K-context API into your agent loop.\n\n"
        "━━━ <b>OPEN FRONTIER</b> 🔓\n"
        "<b>Mistral Large 4</b> — <i>Frontier reasoning with a 512K window.</i> "
        "524K context · Released Oct 6. ⚡ API live · OpenRouter "
        '<a href="https://mistral.ai/news/mistral-large-4/">→ Source</a>\n'
        "<b>Holotron4 30B A3B</b> — <i>Sparse image-text-to-text model.</i> "
        "Released Sep 28. 📦 Open weights · HF "
        '<a href="https://huggingface.co/Hcompany/Holotron4-30B-A3B">→ Source</a>\n'
        "━━━ <b>SPECIALIZED</b> 🎯\n"
        "<b>LTX 2.5</b> — <i>Image-to-video successor.</i> Released Oct 2. "
        '<a href="https://huggingface.co/Lightricks/LTX-2.5">→ Source</a>'
    )
    fake = MagicMock()
    fake.raise_for_status = lambda: None
    fake.json.return_value = {"choices": [{"message": {"content": body}}]}
    monkeypatch.setattr(monitor, "LLM_API_KEY", "k")
    monkeypatch.setattr(monitor, "LLM_MODEL_FALLBACK", None)
    monkeypatch.setattr(monitor.requests, "post", lambda *a, **k: fake)
    msg = monitor.summarize_models(
        [mistral, holo, ltx], today="2026-10-06")
    err = capsys.readouterr().err
    assert "512K" not in msg
    assert "524K context" in msg
    assert "Holotron" not in msg
    assert "LTX" not in msg
    assert "→ mistral.ai</a>" in msg
    assert "→ Source" not in msg
    assert "Dropped Take" in err
    assert "Mistral Large 4" in msg


def _tier_block(msg, title):
    match = re.search(
        rf"━━━ <b>{re.escape(title)}</b>[^\n]*\n(.*?)(?=\n━━━ |\n📊 |\Z)",
        msg,
        re.S,
    )
    assert match, f"{title} section missing:\n{msg}"
    return match.group(1)


def test_unrecognized_also_tracked_header_is_remapped(monkeypatch, capsys):
    """#226 filed 3 of 4 models under a made-up ALSO TRACKED header.

    Items under a header outside the standard set move into the section
    their model data already implies. Nothing is dropped. The body that
    would be posted no longer contains the made-up header, so the
    unrecognized-tier warning does not fire on it.
    """
    closed = _model(
        "openai/gpt-4o",
        source="openrouter",
        url="https://openrouter.ai/models/openai/gpt-4o",
        release_date="2026-10-07",
        is_open_source=False,
    )
    frontier = _model(
        "Qwen/Qwen3-72B",
        release_date="2026-10-07",
        is_open_source=True,
        url="https://huggingface.co/Qwen/Qwen3-72B",
    )
    local = _model(
        "tiny-local-7b",
        provider="Ollama",
        source="ollama",
        url="https://ollama.com/library/tiny-local-7b",
        release_date="2026-10-07",
    )
    image = _model(
        "acme/Shelf-Classifier",
        modality="image",
        release_date="2026-10-07",
        url="https://huggingface.co/acme/Shelf-Classifier",
    )
    mystery = _model(
        "acme/Mystery-Widget",
        release_date="2026-10-07",
        url="https://huggingface.co/acme/Mystery-Widget",
    )
    # Writer kept a real tier, then invented ALSO TRACKED for the rest.
    # One name matches no candidate and must still be published.
    body = (
        "<i>Narrow tools, not a frontier wave.</i>\n"
        "\n"
        "━━━ <b>CLOSED FRONTIER</b> 🔒\n"
        "\n"
        "<b>GPT 4o</b> — <i>Closed API point release.</i>\n"
        "\n"
        "━━━ <b>ALSO TRACKED</b>\n"
        "\n"
        "<b>Qwen3 72B</b> — <i>Open flagship weights.</i>\n"
        "<b>Tiny Local 7b</b> — <i>Runs on a laptop.</i>\n"
        "<b>Shelf Classifier</b> — <i>Image classifier for a catalog.</i>\n"
        "<b>Mystery Widget</b> — <i>A small tool model with no public tier.</i>\n"
        "<b>Unmatched Gadget</b> — <i>Kept even when no candidate matches.</i>\n"
    )
    fake = MagicMock()
    fake.raise_for_status = lambda: None
    fake.json.return_value = {"choices": [{"message": {"content": body}}]}
    monkeypatch.setattr(monitor, "LLM_API_KEY", "k")
    monkeypatch.setattr(monitor, "LLM_MODEL_FALLBACK", None)
    monkeypatch.setattr(monitor.requests, "post", lambda *a, **k: fake)
    msg = monitor.summarize_models(
        [closed, frontier, local, image, mystery], today="2026-10-07")
    err = capsys.readouterr().err

    assert "ALSO TRACKED" not in msg
    assert "Qwen3 72B" in _tier_block(msg, "OPEN FRONTIER")
    assert "GPT 4o" in _tier_block(msg, "CLOSED FRONTIER")
    assert "Qwen3" not in _tier_block(msg, "CLOSED FRONTIER")
    specialized = _tier_block(msg, "SPECIALIZED")
    assert "Shelf Classifier" in specialized
    assert "Mystery Widget" in specialized
    assert "Unmatched Gadget" in specialized
    assert "Tiny Local 7b" in _tier_block(msg, "LOCAL")
    for name in ("Qwen3 72B", "Tiny Local 7b", "Shelf Classifier",
                 "Mystery Widget", "Unmatched Gadget", "GPT 4o"):
        assert name in msg

    _, warnings, errors = monitor.validate_digest_for_publish(msg, mode="curated")
    assert not any("unrecognized tier header" in w for w in warnings)
    assert not any("ALSO TRACKED" in w for w in warnings)
    assert errors == []

    remap_lines = [line for line in err.splitlines() if "Remapped unrecognized section" in line]
    assert len(remap_lines) == 1
    logged = remap_lines[0]
    assert "ALSO TRACKED" in logged
    assert "Qwen3 72B" in logged and "OPEN FRONTIER" in logged
    assert "Tiny Local 7b" in logged and "LOCAL" in logged
    assert "Mystery Widget" in logged and "SPECIALIZED" in logged
    assert "Unmatched Gadget" in logged and "SPECIALIZED" in logged


def test_structured_other_bucket_is_a_standard_section(monkeypatch):
    """TAKE/ITEM/PROSE still renders. 'other' is not posted as ALSO TRACKED."""
    model = _model(
        "acme/Mystery-Widget",
        release_date="2026-10-07",
        url="https://huggingface.co/acme/Mystery-Widget",
    )
    body = (
        "TAKE: NONE\n"
        "ITEM: Mystery Widget\n"
        "PROSE: A small tool model with no public tier.\n"
    )
    fake = MagicMock()
    fake.raise_for_status = lambda: None
    fake.json.return_value = {"choices": [{"message": {"content": body}}]}
    monkeypatch.setattr(monitor, "LLM_API_KEY", "k")
    monkeypatch.setattr(monitor, "LLM_MODEL_FALLBACK", None)
    monkeypatch.setattr(monitor.requests, "post", lambda *a, **k: fake)
    msg = monitor.summarize_models([model], today="2026-10-07")
    assert "ALSO TRACKED" not in msg
    assert "Mystery Widget" in _tier_block(msg, "SPECIALIZED")
    _, warnings, _errors = monitor.validate_digest_for_publish(msg, mode="curated")
    assert not any("unrecognized tier header" in w for w in warnings)


def test_no_blank_line_directly_under_section_header(monkeypatch):
    """Header, then the first item. The blank line under the header is gone."""
    model = _model(
        "google/gemma-4-12B-it",
        total_parameters="12B",
        license="apache-2.0",
        release_date="2026-10-07",
        url="https://huggingface.co/google/gemma-4-12B-it",
        is_open_source=True,
    )
    body = (
        "TAKE: NONE\n"
        "ITEM: Gemma 4 12B\n"
        "PROSE: Builders can run it without an API key.\n"
    )
    fake = MagicMock()
    fake.raise_for_status = lambda: None
    fake.json.return_value = {"choices": [{"message": {"content": body}}]}
    monkeypatch.setattr(monitor, "LLM_API_KEY", "k")
    monkeypatch.setattr(monitor, "LLM_MODEL_FALLBACK", None)
    monkeypatch.setattr(monitor.requests, "post", lambda *a, **k: fake)
    msg = monitor.summarize_models([model], today="2026-10-07")
    assert "━━━ <b>OPEN FRONTIER</b> 🔓\n<b>Gemma 4 12B</b>" in msg
    assert "━━━ <b>OPEN FRONTIER</b> 🔓\n\n" not in msg

    image = _model(
        "acme/Shelf-Classifier",
        modality="image",
        release_date="2026-10-07",
        url="https://huggingface.co/acme/Shelf-Classifier",
    )
    html_body = (
        "<i>One specialised model.</i>\n"
        "\n"
        "━━━ <b>SPECIALIZED</b> 🎯\n"
        "\n"
        "<b>Shelf Classifier</b> — <i>Image classifier for a catalog.</i>\n"
    )
    fake.json.return_value = {"choices": [{"message": {"content": html_body}}]}
    html_msg = monitor.summarize_models([image], today="2026-10-07")
    assert "━━━ <b>SPECIALIZED</b> 🎯\n<b>Shelf Classifier</b>" in html_msg
    assert "━━━ <b>SPECIALIZED</b> 🎯\n\n" not in html_msg

    template = monitor.build_digest_message([model], today="2026-10-07")
    assert "━━━ <b>OPEN FRONTIER</b> 🔓\n<b>" in template
    assert "━━━ <b>OPEN FRONTIER</b> 🔓\n\n" not in template


def test_prompt_lists_only_standard_section_headers(monkeypatch):
    captured = {}

    class FR:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "TAKE: NONE\n"}}]}

    def fake_post(url, json, headers, timeout):
        captured["prompt"] = json["messages"][0]["content"]
        return FR()

    monkeypatch.setattr(monitor, "LLM_API_KEY", "k")
    monkeypatch.setattr(monitor.requests, "post", fake_post)
    monitor.summarize_models(
        [_model("acme/Model-1", release_date="2026-10-07")],
        today="2026-10-07")
    prompt = captured["prompt"]
    for header in ("OPEN FRONTIER", "CLOSED FRONTIER", "SPECIALIZED", "LOCAL", "WATCH"):
        assert header in prompt
    assert "do not invent" in prompt.lower()
    assert "ALSO TRACKED" in prompt

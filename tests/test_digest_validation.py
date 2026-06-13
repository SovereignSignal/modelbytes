"""Pre-publish factual QA for curated and fallback digests."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


@pytest.fixture(autouse=True)
def _zaya_fact_in_window(monkeypatch):
    """Facts now expire (release + 45d). These tests exercise the correction
    MECHANISM, not the calendar — pin the fact active so the suite doesn't
    rot when ZAYA's window closes (expiry itself is covered in
    test_content_gates.py::test_expired_fact_no_longer_rewrites_copy)."""
    monkeypatch.setattr(monitor, "_fact_active", lambda fact, today=None: True)


def test_validate_digest_corrects_zaya_active_parameter_claim():
    body = (
        "🤖 ModelBytes Digest\n"
        "Friday, May 22, 2026\n\n"
        "ZAYA1-8B (Zyphra) — Apache 2.0, May 6. "
        "Small MoE with 8B active parameters trained entirely on AMD hardware. "
        "https://www.zyphra.com/post/zaya1-8b\n\n"
        "Total: 1 models tracked today"
    )

    normalized, warnings, errors = monitor.validate_digest_for_publish(body)

    assert errors == []
    assert "8B active parameters" not in normalized
    assert "8.4B total / 760M active parameters" in normalized
    assert "corrected ZAYA1-8B" in " ".join(warnings)


def test_pending_curated_posts_corrected_body(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    today = monitor.datetime.now(monitor.timezone.utc).strftime("%Y-%m-%d")
    pending_dir = tmp_path / "pending"
    pending_dir.mkdir()
    body = (
        "🤖 ModelBytes Digest\n"
        "Friday, May 22, 2026\n\n"
        "ZAYA1-8B — Apache 2.0, 8B active parameters. "
        "https://www.zyphra.com/post/zaya1-8b\n\n"
        "Total: 1 models tracked today"
    )
    (pending_dir / f"{today}.txt").write_text(body)

    sent = []
    marks = []
    monkeypatch.setattr(monitor, "init_posted_digest_store", lambda: False)
    monkeypatch.setattr(monitor, "has_posted_digest", lambda date_str: False)
    monkeypatch.setattr(monitor, "send_telegram_post", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(monitor, "mark_posted_digest", lambda *args: marks.append(args) or True)

    assert monitor.try_post_pending_curated() is True
    assert "8B active parameters" not in sent[0]
    assert "760M active parameters" in sent[0]
    assert marks[0][3] == sent[0]


def test_summarize_models_supplies_known_facts_to_llm(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "1 models tracked today"}}]}

    def fake_post(url, json, headers, timeout):
        captured["prompt"] = json["messages"][0]["content"]
        return FakeResponse()

    monkeypatch.setattr(monitor, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(monitor.requests, "post", fake_post)

    model = monitor.ModelRelease(
        name="Zyphra/ZAYA1-8B",
        provider="Zyphra",
        source="huggingface-org",
        url="https://huggingface.co/Zyphra/ZAYA1-8B",
        description="Reasoning MoE",
        release_date="2026-05-06",
        is_open_source=True,
    )

    monitor.summarize_models([model])
    prompt = captured["prompt"]

    assert "License: Apache 2.0" in prompt
    assert "Total params: 8.4B" in prompt
    assert "Active params: 760M" in prompt
    assert "Confidence: high" in prompt
    assert "Do not infer or invent parameter counts" in prompt

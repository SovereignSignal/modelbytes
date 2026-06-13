"""Busy-day overflow must not silently drop significant models.

When more than DIGEST_LIMIT new models arrive, only the posted top-N and any
*insignificant* overflow get marked seen. Significant-but-unposted models stay
unseen so they resurface on a later run instead of being lost forever.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


def _model(name: str, provider: str = "Test", downloads: int = 0) -> monitor.ModelRelease:
    return monitor.ModelRelease(
        name=name,
        provider=provider,
        source="openrouter",
        url=f"https://example.com/{name}",
        description="Test model",
        downloads=downloads,
        is_open_source=True,
    )


def test_significant_overflow_stays_unseen(monkeypatch, tmp_path):
    # 16 significant (author meta-llama) + 1 noise; cap is DIGEST_LIMIT (15).
    sig = [_model(f"meta-llama/Sig-{i:02d}") for i in range(monitor.DIGEST_LIMIT + 1)]
    noise = _model("randojoe/quiet-thing", provider="randojoe")
    candidates = sig + [noise]

    monkeypatch.setattr(sys, "argv", ["monitor.py"])  # live mode, not --preview
    monkeypatch.setattr(monitor, "try_post_pending_curated", lambda: False)
    monkeypatch.setattr(monitor, "init_database", lambda: None)
    # Non-empty seen set => not a first run, so selection logic runs.
    monkeypatch.setattr(monitor, "load_seen_models", lambda: {"sentinel/seed"})
    monkeypatch.setattr(monitor, "fetch_openrouter_models", lambda: list(candidates))
    monkeypatch.setattr(monitor, "fetch_ollama_models", lambda: [])
    monkeypatch.setattr(monitor, "fetch_huggingface_trending", lambda: [])
    monkeypatch.setattr(monitor, "fetch_major_orgs", lambda: [])
    monkeypatch.setattr(monitor, "fetch_hf_text_generation", lambda: [])

    saved = []
    monkeypatch.setattr(monitor, "save_seen_models", lambda models: saved.append(set(models)))
    monkeypatch.setattr(monitor, "send_telegram_post", lambda message: True)
    monkeypatch.setattr(monitor, "summarize_models", lambda models: "digest body")
    monkeypatch.setattr(monitor, "validate_digest_for_publish", lambda m, mode="curated": (m, [], []))
    monkeypatch.setattr(monitor, "mark_posted_digest", lambda *a, **k: True)
    (tmp_path / "pending").mkdir()
    monkeypatch.chdir(tmp_path)

    assert monitor.main() == 0
    assert saved, "save_seen_models was never called"
    seen = saved[-1]

    # The 16th significant model overflowed the cap but must NOT be marked seen.
    assert "meta-llama/Sig-15" not in seen, "significant overflow was silently dropped"
    # The first 15 significant models were posted and should be marked seen.
    assert "meta-llama/Sig-00" in seen
    # Insignificant overflow IS marked seen so we don't re-scan noise forever.
    assert "randojoe/quiet-thing" in seen

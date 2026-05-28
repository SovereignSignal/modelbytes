"""Preview mode should render without sending or mutating state."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


def _release(name: str = "openai/gpt-4o-test") -> monitor.ModelRelease:
    return monitor.ModelRelease(
        name=name,
        provider="OpenAI",
        source="openrouter",
        url=f"https://example.com/{name}",
        description="Test model",
        is_open_source=False,
    )


def test_preview_mode_renders_on_empty_database(monkeypatch, capsys):
    """A fresh DB should not trigger first-run seeding in preview mode."""
    monkeypatch.setattr(sys, "argv", ["monitor.py", "--preview"])
    monkeypatch.setattr(monitor, "try_post_pending_curated", lambda: False)
    monkeypatch.setattr(monitor, "init_database", lambda: None)
    monkeypatch.setattr(monitor, "load_seen_models", lambda: set())
    monkeypatch.setattr(monitor, "fetch_openrouter_models", lambda: [_release()])
    monkeypatch.setattr(monitor, "fetch_ollama_models", lambda: [])
    monkeypatch.setattr(monitor, "fetch_huggingface_trending", lambda: [])
    monkeypatch.setattr(monitor, "fetch_major_orgs", lambda: [])
    monkeypatch.setattr(monitor, "fetch_hf_text_generation", lambda: [])

    saved = []
    sent = []
    monkeypatch.setattr(monitor, "save_seen_models", lambda models: saved.append(models))
    monkeypatch.setattr(monitor, "send_telegram_post", lambda message: sent.append(message))
    monkeypatch.setattr(monitor, "summarize_models", lambda models: "preview digest")

    assert monitor.main() == 0
    out = capsys.readouterr().out
    assert "=== PREVIEW ===" in out
    assert "preview digest" in out
    assert "First run" not in out
    assert saved == []
    assert sent == []

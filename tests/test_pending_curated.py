"""Tests for the pending-curated-file fast-path."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


@pytest.fixture(autouse=True)
def _disable_idempotency_db(monkeypatch):
    monkeypatch.setattr(monitor, "init_posted_digest_store", lambda: False)


def _write_pending(tmp_path: Path, date_str: str, content: str) -> Path:
    pending_dir = tmp_path / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    f = pending_dir / f"{date_str}.txt"
    f.write_text(content)
    return f


def test_try_post_pending_returns_false_when_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pending").mkdir()
    monkeypatch.setattr(monitor, "has_posted_digest", lambda date_str: False)
    result = monitor.try_post_pending_curated()
    assert result is False


def test_try_post_pending_posts_when_file_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pending").mkdir()
    today = monitor.datetime.now(monitor.timezone.utc).strftime("%Y-%m-%d")
    body = "🤖 ModelBytes Digest test body\n\n3 models tracked today"
    _write_pending(tmp_path, today, body)

    sent = []
    monkeypatch.setattr(monitor, "send_telegram_post",
                        lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(monitor, "has_posted_digest", lambda date_str: False)
    monkeypatch.setattr(monitor, "mark_posted_digest", lambda *args: True)

    result = monitor.try_post_pending_curated()
    assert result is True
    assert len(sent) == 1
    assert "ModelBytes Digest" in sent[0]


def test_try_post_pending_skips_empty_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pending").mkdir()
    today = monitor.datetime.now(monitor.timezone.utc).strftime("%Y-%m-%d")
    _write_pending(tmp_path, today, "")

    monkeypatch.setattr(monitor, "send_telegram_post", lambda msg: True)
    monkeypatch.setattr(monitor, "has_posted_digest", lambda date_str: False)
    result = monitor.try_post_pending_curated()
    assert result is False


def test_try_post_pending_returns_false_when_send_fails(tmp_path, monkeypatch):
    """If Telegram send fails, return False so fallback can try."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pending").mkdir()
    today = monitor.datetime.now(monitor.timezone.utc).strftime("%Y-%m-%d")
    _write_pending(tmp_path, today, "🤖 ModelBytes Digest test")

    monkeypatch.setattr(monitor, "send_telegram_post", lambda msg: False)
    monkeypatch.setattr(monitor, "has_posted_digest", lambda date_str: False)
    result = monitor.try_post_pending_curated()
    assert result is False


def test_try_post_pending_skips_when_date_already_marked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pending").mkdir()
    today = monitor.datetime.now(monitor.timezone.utc).strftime("%Y-%m-%d")
    _write_pending(tmp_path, today, "🤖 ModelBytes Digest test")

    sent = []
    monkeypatch.setattr(monitor, "send_telegram_post",
                        lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(monitor, "has_posted_digest", lambda date_str: True)

    result = monitor.try_post_pending_curated()
    assert result is True
    assert sent == []


def test_try_post_pending_marks_date_after_success(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pending").mkdir()
    today = monitor.datetime.now(monitor.timezone.utc).strftime("%Y-%m-%d")
    body = "🤖 ModelBytes Digest test"
    _write_pending(tmp_path, today, body)

    marks = []
    monkeypatch.setattr(monitor, "send_telegram_post", lambda msg: True)
    monkeypatch.setattr(monitor, "has_posted_digest", lambda date_str: False)
    monkeypatch.setattr(monitor, "mark_posted_digest",
                        lambda *args: marks.append(args) or True)

    result = monitor.try_post_pending_curated()
    assert result is True
    assert marks == [(today, "curated", f"pending/{today}.txt", body)]

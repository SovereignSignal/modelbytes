"""Published-digest history: fact-consistency and already-covered names.

Railway cron is ephemeral, so pending/<date>.txt write-back does not survive
to tomorrow. posted_digests.body is the durable copy of what readers saw
(2026-08-20 coverage/quality plan). pending/*.txt remains a fallback for
--preview, tests, and the baked-in May/June corpus.
"""
from pathlib import Path
from unittest.mock import patch

import monitor


PRIOR = (
    "<b>MiniMax M3</b> — <i>x.</i> 229.9B total / 9.8B active. "
    '<a href="https://a.b">→ S</a>'
)
TODAY = (
    "🤖 <b>ModelBytes Digest</b>\n"
    "<b>MiniMax M3</b> — <i>x.</i> ~428B total / ~23B active.\n"
)


def test_fact_consistency_flags_drift_from_db_when_pending_missing(tmp_path):
    with patch.object(
        monitor, "load_recent_digest_bodies",
        return_value=[("2026-06-09", PRIOR)],
    ):
        warnings = monitor._check_fact_consistency(
            TODAY, pending_dir=tmp_path / "no-pending", today="2026-06-12")
    assert any("MiniMax M3" in w and "229.9" in w for w in warnings)


def test_fact_consistency_db_body_wins_over_stale_pending(tmp_path):
    pend = tmp_path / "pending"
    pend.mkdir()
    (pend / "2026-06-09.txt").write_text(
        "<b>MiniMax M3</b> — <i>x.</i> 229.9B total / 9.8B active.")
    db_body = (
        "<b>MiniMax M3</b> — <i>x.</i> 428B total / 23B active. "
        "(corrects our Jun 9 figure)"
    )
    with patch.object(
        monitor, "load_recent_digest_bodies",
        return_value=[("2026-06-09", db_body)],
    ):
        warnings = monitor._check_fact_consistency(
            TODAY, pending_dir=pend, today="2026-06-12")
    # DB says 428B already (with a correction marker in that prior post).
    # Today's 428B vs DB 428B is not drift; pending's 229.9B must not win.
    assert not any("229.9" in w for w in warnings)


def test_recent_digest_names_reads_db_bodies(tmp_path):
    with patch.object(
        monitor, "load_recent_digest_bodies",
        return_value=[
            ("2026-06-15", "<b>Kimi K2.7 Code</b> — <i>z</i>"),
            ("2026-06-14", "<b>Gemma 4 12B</b> — <i>y</i>"),
        ],
    ):
        names = monitor._recent_digest_names(
            today="2026-06-16", pending_dir=tmp_path / "no-pending")
    assert "Kimi K2.7 Code" in names
    assert "Gemma 4 12B" in names


def test_recent_digest_names_merges_pending_for_dates_db_lacks(tmp_path):
    pend = tmp_path / "pending"
    pend.mkdir()
    (pend / "2026-06-14.txt").write_text(
        "<b>Gemma 4 12B</b> — <i>y</i>")
    with patch.object(
        monitor, "load_recent_digest_bodies",
        return_value=[("2026-06-15", "<b>Kimi K2.7 Code</b> — <i>z</i>")],
    ):
        names = monitor._recent_digest_names(
            today="2026-06-16", pending_dir=pend)
    assert "Kimi K2.7 Code" in names
    assert "Gemma 4 12B" in names

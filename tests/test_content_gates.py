"""Content trust gates (design-pass 2026-06-12).

validate_digest_for_publish was ornamental — only empty-body and one ZAYA regex
blocked. These tests pin the new contract:
- ERRORS are reserved for channel-harm (malformed HTML that would 400, non-https
  links, floods on the fallback path, empty body). Blocking a curated digest
  sends the reader something WORSE (the fallback), so format drift is a WARNING.
- WARNINGS catch v3 grammar drift, aggregator sourcing, footer miscounts, and
  cross-day fact contradictions — surfaced to the operator, never censored.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


GOOD = """🤖 <b>ModelBytes Digest</b>
<i>Friday, June 12, 2026</i>

<i>The take line.</i>

━━━ <b>OPEN FRONTIER</b> 🔓
<b>Model A</b> — <i>The differentiator sentence.</i> 70B total / 7B active. ⚡ API live: $1/$2 per 1M. <a href="https://vendor.ai/blog">→ Vendor</a>

Total: 1 items tracked today"""


def _validate(body, mode="curated"):
    return monitor.validate_digest_for_publish(body, mode=mode)


# ── channel-harm errors ──

def test_good_curated_digest_has_no_errors():
    _, warnings, errors = _validate(GOOD)
    assert errors == []


def test_disallowed_tag_is_an_error():
    bad = GOOD.replace("<b>Model A</b>", "<script>x</script><b>Model A</b>")
    _, _, errors = _validate(bad)
    assert any("script" in e for e in errors)


def test_unbalanced_bold_is_an_error():
    bad = GOOD.replace("<b>Model A</b>", "<b>Model A")
    _, _, errors = _validate(bad)
    assert any("unbalanced" in e.lower() for e in errors)


def test_stray_lt_in_prose_is_an_error():
    # 2026-06-21 incident: the LLM emitted a literal '<' in prose
    # (e.g. 'under <100B params' / '5 < 10'). Python's html.parser doesn't see
    # a tag there, so the balance check passed — but Telegram's strict parser
    # returned 'Unclosed start tag at byte offset N' and 400'd, which sent the
    # publisher down the send-failed path and left the service Crashed. Any '<'
    # that isn't part of a known tag must be treated as channel-harm.
    for stray in [
        GOOD.replace("70B total", "under <100B params"),
        GOOD.replace("70B total", "5 < 10 tokens"),
        "Plain text with a stray < and more text after it.",
        "Models under <100B params and >50B active.",
    ]:
        _, _, errors = _validate(stray, mode="fallback")
        assert errors, f"stray '<' must be rejected; body was: {stray!r}"
        assert any("stray" in e.lower() for e in errors), errors


def test_non_https_link_severity_by_mode():
    # Telegram renders http:// fine, so it's not channel-harm: machine-assembled
    # fallback content gets blocked, curated content gets a warning (blocking a
    # curated digest would publish the WORSE fallback instead).
    bad = GOOD.replace("https://vendor.ai/blog", "http://vendor.ai/blog")
    _, _, errs_fallback = _validate(bad, mode="fallback")
    _, warns_curated, errs_curated = _validate(bad, mode="curated")
    assert any("https" in e for e in errs_fallback)
    assert errs_curated == []
    assert any("https" in w for w in warns_curated)


def test_real_pending_corpus_passes_with_no_errors():
    pending = Path(__file__).resolve().parent.parent / "pending"
    for f in sorted(pending.glob("*.txt")):
        _, _, errors = _validate(f.read_text())
        assert errors == [], f"{f.name}: {errors}"


# ── drift warnings (published anyway, operator alerted) ──

def test_missing_tier_header_is_a_warning_not_error():
    bad = GOOD.replace("━━━ <b>OPEN FRONTIER</b> 🔓\n", "")
    _, warnings, errors = _validate(bad)
    assert errors == []
    assert any("tier" in w.lower() for w in warnings)


def test_entry_without_differentiator_is_a_warning():
    bad = GOOD.replace(
        "<b>Model A</b> — <i>The differentiator sentence.</i> 70B total / 7B active.",
        "<b>Model A</b> — 70B total / 7B active.")
    _, warnings, errors = _validate(bad)
    assert errors == []
    assert any("differentiator" in w.lower() for w in warnings)


def test_footer_count_mismatch_is_a_warning():
    bad = GOOD.replace("Total: 1 items", "Total: 7 items")
    _, warnings, _ = _validate(bad)
    assert any("footer" in w.lower() and "7" in w for w in warnings)


def test_aggregator_link_is_a_warning():
    bad = GOOD.replace("https://vendor.ai/blog", "https://www.techtimes.com/article")
    _, warnings, errors = _validate(bad)
    assert errors == []
    assert any("aggregator" in w.lower() for w in warnings)


# ── fallback flood tripwires (errors only in fallback mode) ──

def test_quant_name_floods_blocked_in_fallback_mode():
    bad = GOOD.replace("<b>Model A</b>", "<b>Model-A-GGUF</b>")
    _, _, errs_fallback = _validate(bad, mode="fallback")
    _, warns_curated, errs_curated = _validate(bad, mode="curated")
    assert any("quant" in e.lower() for e in errs_fallback)
    assert errs_curated == []  # curated: warn, don't censor


def test_stale_release_dates_blocked_in_fallback_mode():
    bad = GOOD.replace(
        "70B total / 7B active.",
        "Released: 2025-04-09 | 70B total / 7B active.")
    _, _, errors = _validate(bad, mode="fallback")
    assert any("stale" in e.lower() or "2025-04-09" in e for e in errors)


# ── cross-day fact consistency ──

def test_param_contradiction_with_prior_day_is_flagged(tmp_path, monkeypatch):
    pend = tmp_path / "pending"
    pend.mkdir()
    (pend / "2026-06-09.txt").write_text(
        '<b>MiniMax M3</b> — <i>x.</i> 229.9B total / 9.8B active. '
        '<a href="https://a.b">→ S</a>')
    today_body = GOOD.replace(
        "<b>Model A</b> — <i>The differentiator sentence.</i> 70B total / 7B active.",
        "<b>MiniMax M3</b> — <i>x.</i> ~428B total / ~23B active.")
    monkeypatch.chdir(tmp_path)
    _, warnings, errors = _validate(today_body)
    assert errors == []
    assert any("MiniMax M3" in w and ("229.9" in w or "fact" in w.lower())
               for w in warnings)


def test_marked_correction_is_not_flagged(tmp_path, monkeypatch):
    pend = tmp_path / "pending"
    pend.mkdir()
    (pend / "2026-06-09.txt").write_text(
        '<b>MiniMax M3</b> — <i>x.</i> 229.9B total / 9.8B active.')
    today_body = GOOD.replace(
        "<b>Model A</b> — <i>The differentiator sentence.</i> 70B total / 7B active.",
        "<b>MiniMax M3</b> — <i>x.</i> ~428B total / ~23B active (corrects our Jun 9 figure).")
    monkeypatch.chdir(tmp_path)
    _, warnings, _ = _validate(today_body)
    assert not any("MiniMax" in w for w in warnings)


# ── ModelFact expiry ──

def test_expired_fact_no_longer_rewrites_copy():
    # ZAYA released 2026-05-06; far beyond expiry the regex must stop mutating
    # new digests that legitimately mention "8B active" in another context.
    fact = monitor.ZAYA_FACT
    assert monitor._fact_active(fact, today="2026-05-20") is True   # inside window
    assert monitor._fact_active(fact, today="2026-09-01") is False  # long expired

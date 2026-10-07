#!/usr/bin/env python3
"""Monitor AI model releases from OpenRouter, Ollama, and Hugging Face.

Posts new model releases to Telegram @modelbytes channel with tiered, LLM-summarized digest.
"""

import gzip
import hashlib
import html
import json
import os
import re
import socket
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Optional, Set, Tuple
from urllib.parse import unquote, urlparse

from ss_publish import (
    Publisher,
    TelegramResult,
    telegram_html_to_mrkdwn as _ss_telegram_html_to_mrkdwn,
    truncate_for_telegram as _ss_truncate_for_telegram,
)

import release_forwarding

import psycopg2
import requests

# Database — Postgres is the only state backend.
# When DATABASE_URL is unset (local dev, --preview mode), state functions
# degrade gracefully: load returns empty set, save is a no-op.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
# Public proxy URL (Railway Postgres service). Used when DATABASE_URL points
# at *.railway.internal and we are off the private network (`railway run`
# from a laptop/Cloud VM — 2026-08-13 ops-alert incident).
DATABASE_PUBLIC_URL = os.environ.get("DATABASE_PUBLIC_URL", "")

HTTP_RETRIES = int(os.environ.get("MODELBYTES_HTTP_RETRIES", "3"))
HTTP_BACKOFF_SECONDS = float(os.environ.get("MODELBYTES_HTTP_BACKOFF_SECONDS", "1.0"))
HTTP_USER_AGENT = os.environ.get(
    "MODELBYTES_USER_AGENT",
    "ModelBytes/1.0 (+https://github.com/SovereignSignal/modelbytes)",
)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")

# Slack mirror (optional). When both are set, each published digest is also
# posted to this Slack channel. Unset = Telegram-only (no-op), so this is safe
# to ship dormant and activate later by adding the env vars.
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
MODELBYTES_SLACK_CHANNEL_ID = os.environ.get("MODELBYTES_SLACK_CHANNEL_ID", "")

# LLM Summarization
LLM_API_KEY = os.environ.get("MODELBYTES_LLM_KEY",
    os.environ.get("OPENAI_API_KEY",
    os.environ.get("OPENROUTER_API_KEY", "")))
LLM_MODEL = os.environ.get("MODELBYTES_LLM_MODEL", "gpt-4o-mini")
# Secondary model tried when the primary is unavailable/empty (Ollama Cloud's
# catalog churns — a vanished model must not dark the channel). Same endpoint+key.
LLM_MODEL_FALLBACK = os.environ.get("MODELBYTES_LLM_MODEL_FALLBACK", "")
LLM_BASE_URL = os.environ.get("MODELBYTES_LLM_URL", "https://api.openai.com/v1")
# Daily cron, no latency pressure. A frontier reasoning model (deepseek-v4-pro)
# writing a full 15-model digest needs well over the old 60s.
LLM_TIMEOUT = int(os.environ.get("MODELBYTES_LLM_TIMEOUT", "240"))

# Provider name resolution: raw org → display name
PROVIDER_NAMES = {
    "deepseek-ai": "DeepSeek",
    "deepseek": "DeepSeek",
    "meta-llama": "Meta",
    "mistralai": "Mistral AI",
    "qwen": "Alibaba",
    "alibaba": "Alibaba",
    "google": "Google",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "microsoft": "Microsoft",
    "nvidia": "NVIDIA",
    "stabilityai": "Stability AI",
    "nousresearch": "Nous Research",
    "tiiuae": "TII (UAE)",
    "01-ai": "01.AI",
    "x-ai": "xAI",
    "arcee-ai": "Arcee AI",
    "baai": "BAAI",
    "openbmb": "OpenBMB",
    "minimaxai": "MiniMax",
    "rekaai": "Reka AI",
    "xiaomi": "Xiaomi",
    "netflix": "Netflix",
    "k2-fsa": "K2-FSA",
    "baidu": "Baidu",
    "qwen-coder": "Alibaba",
    "sulphurai": "Sulphur AI",
    "supertone": "Supertone",
    "hidream-ai": "HiDream AI",
    "zyphra": "Zyphra",
    "circlestone-labs": "Circlestone Labs",
    "hcompany": "H Company",
    "moonshotai": "Moonshot AI",
    "bytedance-seed": "ByteDance",
    "bytedance-research": "ByteDance",
    "amazon": "Amazon",
    "ibm": "IBM",
    "allenai": "AI2",
    "tencentarc": "Tencent ARC",
    "resembleai": "Resemble AI",
    "adskailab": "Autodesk AI Lab",
    "lgai-exaone": "LG AI Research",
    "perplexity": "Perplexity",
    "perplexity-ai": "Perplexity",
    "cohere": "Cohere",
    "coherelabs": "Cohere",
    "ai21": "AI21 Labs",
    "huggingface": "Hugging Face",
    "cognitivecomputations": "Cognitive Computations",
    "unsloth": "Unsloth AI",
    "open-thoughts": "Open Thoughts",
    "inclusionai": "Inclusion AI",
    "z-ai": "Z.AI",
    "zai-org": "Z.AI",
    "bartowski": "Bartowski",
    "maziyarpanahi": "MaziyarPanahi",
    "mradermacher": "MRadermacher",
    "thebloke": "TheBloke",
    "ollama": "Ollama",
    "philschmid": "Philipp Schmid",
    "sentence-transformers": "Sentence Transformers",
    "bosonai": "Boson AI",
    "sapientinc": "Sapient Intelligence",
    "tencent": "Tencent",
    "sakanaai": "Sakana AI",
    "internlm": "Shanghai AI Lab",
    "meituan-longcat": "Meituan",
    "poolside": "Poolside",
    "ai-sage": "Sber",
    "thinkingmachines": "Thinking Machines",
    "black-forest-labs": "Black Forest Labs",
    "skt": "SK Telecom",
    "wan-ai": "Alibaba",
    "sarvamai": "Sarvam AI",
    "meta-models": "Meta",
    "jetbrains": "JetBrains",
}

# Known significant orgs — never noise-filter these
KNOWN_ORGS = {
    "meta-llama", "mistralai", "qwen", "alibaba", "google", "anthropic",
    "openai", "deepseek-ai", "deepseek", "microsoft", "nvidia",
    "stabilityai", "sentence-transformers", "bartowski",
    "nousresearch", "tiiuae", "01-ai", "philschmid",
    "cognitivecomputations", "thebloke", "ollama", "unsloth",
    "maziyarpanahi", "mradermacher", "ibm", "allenai",
    "x-ai", "z-ai", "zai-org", "arcee-ai", "openbmb",
    "minimaxai", "netflix", "k2-fsa", "xiaomi", "rekaai",
    "baai", "huggingface", "baidu", "perplexity", "cohere", "coherelabs", "ai21",
    "sulphurai", "supertone", "hidream-ai", "zyphra",
    "circlestone-labs", "hcompany", "moonshotai", "bytedance-seed", "bytedance-research",
    "amazon", "perplexity-ai", "inclusionai",
    "tencentarc", "resembleai", "adskailab", "open-thoughts",
    "lgai-exaone", "bosonai", "sapientinc",
    "tencent", "sakanaai",
    "internlm", "meituan-longcat", "poolside", "ai-sage",
    "thinkingmachines", "black-forest-labs",
    "skt", "wan-ai", "sarvamai", "meta-models", "jetbrains",
}


@dataclass
class ModelRelease:
    name: str
    provider: str
    source: str
    url: str
    description: str
    context_window: Optional[int] = None
    pricing_input: Optional[float] = None
    pricing_output: Optional[float] = None
    architecture: Optional[str] = None
    release_date: Optional[str] = None
    is_open_source: Optional[bool] = None
    performance_scores: dict = None
    unique_traits: List[str] = None
    downloads: int = 0
    likes: int = 0
    license: Optional[str] = None
    total_parameters: Optional[str] = None
    active_parameters: Optional[str] = None
    canonical_url: Optional[str] = None
    confidence: str = "medium"
    validation_notes: List[str] = None
    # Short benchmark/fact string pulled from the HF model card (inline path).
    card_facts: Optional[str] = None
    # Which clock we treated as the release (lastModified, first_public_release,
    # trending, createdAt). repo_created_at is the HF repo birth date, which is
    # often days or months before the public launch.
    recency_signal: Optional[str] = None
    repo_created_at: Optional[str] = None

    def __post_init__(self):
        if self.performance_scores is None:
            self.performance_scores = {}
        if self.unique_traits is None:
            self.unique_traits = []
        if self.validation_notes is None:
            self.validation_notes = []


@dataclass(frozen=True)
class ModelFact:
    canonical_name: str
    aliases: Tuple[str, ...]
    canonical_url: str
    release_date: str
    license: str
    total_parameters: str
    active_parameters: Optional[str]
    confidence: str = "high"
    # Facts are a freshness window, not a life sentence: past expiry the
    # normalizer/warnings stop firing, so an old regex can't mutate copy that
    # legitimately mentions similar numbers in a new context. Defaults to
    # release_date + 45 days when unset.
    expires: Optional[str] = None


FACT_DEFAULT_TTL_DAYS = 45


def _fact_active(fact: "ModelFact", today: str = None) -> bool:
    if not fact.expires:
        # Reuse the one date-window implementation (handles timestamp-suffixed
        # and unparseable dates identically) instead of a second copy.
        return not is_stale_release(fact.release_date, today=today,
                                    max_age_days=FACT_DEFAULT_TTL_DAYS)
    ref = (datetime.strptime(today, "%Y-%m-%d").date() if today
           else datetime.now(timezone.utc).date())
    try:
        return ref <= datetime.strptime(fact.expires, "%Y-%m-%d").date()
    except ValueError:
        return True


KNOWN_MODEL_FACTS: Tuple[ModelFact, ...] = (
    ModelFact(
        canonical_name="ZAYA1-8B",
        aliases=("zaya1-8b", "zyphra/zaya1-8b"),
        canonical_url="https://www.zyphra.com/post/zaya1-8b",
        release_date="2026-05-06",
        license="Apache 2.0",
        total_parameters="8.4B",
        active_parameters="760M",
    ),
    ModelFact(
        canonical_name="DeepSeek V4-Pro",
        aliases=("deepseek v4-pro", "deepseek-v4-pro", "deepseek-ai/deepseek-v4-pro"),
        canonical_url="https://api-docs.deepseek.com/news/news260424",
        release_date="2026-04-24",
        license="MIT",
        total_parameters="1.6T",
        active_parameters="49B",
    ),
    ModelFact(
        canonical_name="DeepSeek V4-Flash",
        aliases=("deepseek v4-flash", "deepseek-v4-flash", "deepseek-ai/deepseek-v4-flash"),
        canonical_url="https://api-docs.deepseek.com/news/news260424",
        release_date="2026-04-24",
        license="MIT",
        total_parameters="284B",
        active_parameters="13B",
    ),
    ModelFact(
        canonical_name="Nemotron 3 Nano Omni",
        aliases=("nemotron 3 nano omni", "nemotron-3-nano-omni"),
        canonical_url=(
            "https://developer.nvidia.com/blog/"
            "nvidia-nemotron-3-nano-omni-powers-multimodal-agent-reasoning-in-a-single-efficient-open-model"
        ),
        release_date="2026-04-28",
        license="NVIDIA Open Model License",
        total_parameters="30B",
        active_parameters="3B",
    ),
)
ZAYA_FACT = KNOWN_MODEL_FACTS[0]


def _is_private_host_unreachable(exc: BaseException) -> bool:
    """True when Postgres failed because *.railway.internal did not resolve.

    Distinguishes off-network DNS (safe to retry DATABASE_PUBLIC_URL) from
    auth/permission errors on a host we *did* reach (must not retry — that
    would hide a real credential problem).
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    if "railway.internal" not in text:
        return False
    needles = (
        "could not translate host name",
        "name or service not known",
        "temporary failure in name resolution",
        "nodename nor servname",
        "getaddrinfo failed",
    )
    return any(n in text for n in needles)


def _db_connect(**kwargs):
    """psycopg2.connect against DATABASE_URL, with a public-URL fallback.

    `railway run` injects the private DATABASE_URL (postgres.railway.internal).
    That hostname only resolves inside Railway's private network; off-network
    it raises OperationalError and — before 2026-08-13 — the crash handler
    ops-alerted. If DATABASE_PUBLIC_URL is set, retry it on DNS failure only.
    """
    urls = [u for u in (DATABASE_URL, DATABASE_PUBLIC_URL) if u]
    # Dedup if someone set both to the same public URL.
    seen, ordered = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    if not ordered:
        raise psycopg2.OperationalError("DATABASE_URL is not set")
    last = None
    for i, url in enumerate(ordered):
        try:
            return psycopg2.connect(url, **kwargs)
        except Exception as e:
            last = e
            if i + 1 < len(ordered) and _is_private_host_unreachable(e):
                print("DATABASE_URL host unreachable off-network; "
                      "retrying DATABASE_PUBLIC_URL", file=sys.stderr)
                continue
            raise
    raise last


def init_database():
    """Create the models table if it doesn't exist. No-op without DATABASE_URL."""
    if not DATABASE_URL:
        return
    conn = _db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS models (
                    id SERIAL PRIMARY KEY,
                    model_id VARCHAR(255) UNIQUE NOT NULL,
                    name VARCHAR(500),
                    provider VARCHAR(255),
                    source VARCHAR(50),
                    url TEXT,
                    description TEXT,
                    context_window INTEGER,
                    pricing_input NUMERIC(10,6),
                    pricing_output NUMERIC(10,6),
                    architecture VARCHAR(100),
                    release_date DATE,
                    is_open_source BOOLEAN,
                    unique_traits TEXT[],
                    discovered_at TIMESTAMP DEFAULT NOW(),
                    last_updated TIMESTAMP DEFAULT NOW()
                )
            """)
            _ensure_posted_digests_table(cur)
            _ensure_publish_runs_table(cur)
        conn.commit()
    finally:
        conn.close()


def _ensure_posted_digests_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS posted_digests (
            post_date DATE PRIMARY KEY,
            source VARCHAR(50) NOT NULL,
            digest_path TEXT,
            message_hash VARCHAR(64),
            body TEXT,
            posted_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    # Existing production tables were created without body; ADD COLUMN is
    # idempotent. The published HTML is what fact-consistency and
    # already-covered need — the hash alone cannot reconstruct it, and
    # pending/<date>.txt does not survive the ephemeral Railway cron.
    cur.execute(
        "ALTER TABLE posted_digests ADD COLUMN IF NOT EXISTS body TEXT"
    )


def init_posted_digest_store() -> bool:
    """Create the post-idempotency table. Failure should not block posting."""
    if not DATABASE_URL:
        return False
    try:
        conn = _db_connect()
        try:
            with conn.cursor() as cur:
                _ensure_posted_digests_table(cur)
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        print(f"Post idempotency store unavailable: {e}", file=sys.stderr)
        return False


def has_posted_digest(date_str: str) -> bool:
    """Return True if a digest for this UTC date is already recorded as posted."""
    if not DATABASE_URL:
        return False
    try:
        conn = _db_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM posted_digests WHERE post_date = %s LIMIT 1",
                    (date_str,),
                )
                return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception as e:
        print(f"Could not check posted digest ledger: {e}", file=sys.stderr)
        return False


def mark_posted_digest(date_str: str, source: str, digest_path: str, message: str) -> bool:
    """Record a successful post for this UTC date. Returns False on best-effort failure."""
    if not DATABASE_URL:
        return False
    message_hash = hashlib.sha256(message.encode("utf-8")).hexdigest() if message else None
    try:
        conn = _db_connect()
        try:
            with conn.cursor() as cur:
                _ensure_posted_digests_table(cur)
                cur.execute(
                    """
                    INSERT INTO posted_digests
                        (post_date, source, digest_path, message_hash, body)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (post_date) DO NOTHING
                    """,
                    (date_str, source, digest_path, message_hash, message or None),
                )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        print(f"Could not mark digest posted for {date_str}: {e}", file=sys.stderr)
        return False


def _posted_date_str(value) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value)[:10]


def load_recent_digest_bodies(today: str = None, days: int = 14) -> List[Tuple[str, str]]:
    """Published digest HTML from posted_digests, newest first, today excluded.

    Empty list when DATABASE_URL is unset or on any failure — callers fall
    back to pending/*.txt. Never raises.
    """
    if not DATABASE_URL:
        return []
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        conn = _db_connect()
        try:
            with conn.cursor() as cur:
                _ensure_posted_digests_table(cur)
                cur.execute(
                    """
                    SELECT post_date, body
                    FROM posted_digests
                    WHERE body IS NOT NULL AND btrim(body) <> ''
                      AND post_date <> %s::date
                    ORDER BY post_date DESC
                    LIMIT %s
                    """,
                    (today, days),
                )
                return [
                    (_posted_date_str(post_date), body)
                    for post_date, body in cur.fetchall()
                ]
        finally:
            conn.close()
    except Exception as e:
        print(f"Could not load posted digest bodies: {e}", file=sys.stderr)
        return []


def _digest_history(today: str = None, days: int = 14,
                    pending_dir: Path = None) -> List[Tuple[str, str]]:
    """[(date, body), ...] newest first, `today` excluded.

    Postgres posted_digests.body wins for a given date (what readers saw).
    pending/*.txt fills dates the DB has no body for (baked-in corpus,
    --preview, tests).
    """
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    by_date = {}
    for date_str, body in load_recent_digest_bodies(today=today, days=days):
        d = str(date_str)[:10]
        if d == today or not (body or "").strip():
            continue
        by_date[d] = body
    pending_dir = pending_dir or Path("pending")
    if pending_dir.is_dir():
        for path in pending_dir.glob("*.txt"):
            d = path.stem
            if d == today or d in by_date:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if text.strip():
                by_date[d] = text
    return sorted(by_date.items(), reverse=True)[:days]


# ── Ops layer: run records, admin alerts, heartbeat ─────────────────────────
# Contract (design-pass 2026-06-12): never raises — a broken alert must never
# break publishing — never logs secrets, and every run leaves a publish_runs
# row when a DB is configured.

ADMIN_CHAT_ID = os.environ.get("MODELBYTES_ADMIN_CHAT_ID", "")
OPS_SLACK_CHANNEL_ID = os.environ.get("MODELBYTES_OPS_SLACK_CHANNEL_ID", "")
HEARTBEAT_URL = os.environ.get("MODELBYTES_HEARTBEAT_URL", "").rstrip("/")
ALLOW_SEED = os.environ.get("MODELBYTES_ALLOW_SEED") == "1"

# The shared publish core (ss_publish, vendored at ./ss_publish). One Publisher
# constructed from ModelBytes' env vars; the send/mirror/ops functions below
# delegate to it. Config-driven so the core stays testable and this channel
# keeps its own env-var names, ops banner, and placeholder mapping.
_publisher = Publisher(
    telegram_token=TELEGRAM_BOT_TOKEN,
    telegram_channel_id=TELEGRAM_CHANNEL_ID,
    slack_token=SLACK_BOT_TOKEN,
    slack_channel_id=MODELBYTES_SLACK_CHANNEL_ID,
    ops_telegram_chat_id=ADMIN_CHAT_ID,
    ops_slack_channel_id=OPS_SLACK_CHANNEL_ID,
    disable_preview=True,  # ModelBytes: keep the digest channel clean (no link cards)
    ops_banner="🚨 ModelBytes ops:",
    secret_values=tuple(s for s in (TELEGRAM_BOT_TOKEN, SLACK_BOT_TOKEN, DATABASE_URL, DATABASE_PUBLIC_URL) if s),
)


def _redact_secrets(text: str) -> str:
    # ModelBytes-specific placeholder mapping (<token>, <database-url>) — kept
    # as-is rather than delegating to the shared redact_secrets (which uses a
    # single <redacted> placeholder). The distinct placeholders are part of
    # this channel's ops readability and several tests assert on them.
    out = str(text)
    if TELEGRAM_BOT_TOKEN:
        out = out.replace(TELEGRAM_BOT_TOKEN, "<token>")
    if DATABASE_URL:
        out = out.replace(DATABASE_URL, "<database-url>")
    if DATABASE_PUBLIC_URL:
        out = out.replace(DATABASE_PUBLIC_URL, "<database-url>")
    if SLACK_BOT_TOKEN:
        out = out.replace(SLACK_BOT_TOKEN, "<token>")
    return out


def send_ops_alert(text: str) -> bool:
    """Tell the operator something went wrong (or degraded). Best-effort.

    Routes to a private Telegram chat (MODELBYTES_ADMIN_CHAT_ID) when set,
    else a Slack ops channel (MODELBYTES_OPS_SLACK_CHANNEL_ID). Returns False
    when undeliverable; never raises.

    Delegates the Telegram-then-Slack routing to the shared publish core
    (ss_publish), which isolates each path so a Telegram outage is exactly
    when the Slack fallback fires. Redaction happens inside the core via the
    secret_values passed at construction.
    """
    return _publisher.send_ops_alert(text)


# Per-source health. Visibility only — never changes which models are fetched,
# filtered, or posted. State is a small JSON file under the image's state/
# directory (Dockerfile already creates /app/state). Railway's cron filesystem
# is ephemeral and this repo does not mount a volume, so the file survives a
# run on a machine with a durable disk and is missing on the next cron
# container. Missing and corrupt files start fresh. No new env var and no
# schema change.
SOURCE_HEALTH_PATH = Path(__file__).resolve().parent / "state" / "source_health.json"
SOURCE_HEALTH_ALERT_AFTER = timedelta(hours=24)
_SOURCE_ERROR_LIMIT = 200
_SOURCE_ERRORS = {}
_DISCOVERY_DISABLED = False


def _short_source_error(exc: BaseException) -> str:
    """One-line reason, capped, with known secrets removed."""
    text = _redact_secrets(f"{type(exc).__name__}: {exc}")
    for secret in (
        PARALLEL_API_KEY,
        LLM_API_KEY,
        TELEGRAM_BOT_TOKEN,
        SLACK_BOT_TOKEN,
        DATABASE_URL,
        DATABASE_PUBLIC_URL,
    ):
        if secret:
            text = text.replace(secret, "<redacted>")
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+",
        r"\1<redacted>",
        text,
    )
    text = re.sub(r"(?i)((?:api[_-]?key|token)\s*[:=]\s*)\S+", r"\1<redacted>", text)
    text = " ".join(text.split())
    if len(text) > _SOURCE_ERROR_LIMIT:
        text = text[: _SOURCE_ERROR_LIMIT - 3] + "..."
    return text


def _remember_source_error(source: str, exc: BaseException) -> str:
    """Record a swallowed source failure. Later failures keep the first reason."""
    reason = _short_source_error(exc)
    slot = _SOURCE_ERRORS.get(source)
    if slot is None:
        _SOURCE_ERRORS[source] = {"reason": reason, "extra": 0}
    else:
        slot["extra"] = int(slot.get("extra") or 0) + 1
    return reason


def _consume_source_error(source: str) -> str:
    slot = _SOURCE_ERRORS.pop(source, None)
    if not slot:
        return ""
    reason = slot.get("reason") or ""
    extra = int(slot.get("extra") or 0)
    if extra:
        suffix = f" (+{extra} more)"
        keep = _SOURCE_ERROR_LIMIT - len(suffix)
        reason = (reason[:keep] if keep > 0 else "") + suffix
    return reason[:_SOURCE_ERROR_LIMIT]


def classify_source_health(items: int, error: str) -> str:
    """ok when the source returned anything; error when it failed empty; else empty."""
    if int(items or 0) > 0:
        return "ok"
    if (error or "").strip():
        return "error"
    return "empty"


def format_source_health_line(source: str, status: str, items: int, error: str = "") -> str:
    err = (error or "").strip() or "-"
    return f"source_health source={source} status={status} items={int(items)} error={err}"


def format_writer_health_line(candidates: int, included: int,
                               link_dropped: int, stale_dropped: int) -> str:
    return (
        f"source_health writer candidates={int(candidates)} included={int(included)} "
        f"link_dropped={int(link_dropped)} stale_dropped={int(stale_dropped)}"
    )


def _parse_health_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _blank_source_record() -> dict:
    return {
        "last_ok": None,
        "consecutive_failures": 0,
        "consecutive_empties": 0,
        "unhealthy_since": None,
        "last_alert_at": None,
    }


def _coerce_source_record(raw) -> dict:
    rec = _blank_source_record()
    if not isinstance(raw, dict):
        return rec
    if _parse_health_time(raw.get("last_ok")):
        rec["last_ok"] = raw.get("last_ok")
    if _parse_health_time(raw.get("unhealthy_since")):
        rec["unhealthy_since"] = raw.get("unhealthy_since")
    if _parse_health_time(raw.get("last_alert_at")):
        rec["last_alert_at"] = raw.get("last_alert_at")
    for key in ("consecutive_failures", "consecutive_empties"):
        try:
            rec[key] = max(0, int(raw.get(key) or 0))
        except (TypeError, ValueError):
            rec[key] = 0
    return rec


def load_source_health(path: Path = None) -> dict:
    """Return persisted per-source health, or {} if the file is missing or corrupt."""
    path = Path(path) if path is not None else SOURCE_HEALTH_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        print("source_health state unreadable — starting fresh", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print("source_health state unreadable — starting fresh", file=sys.stderr)
        return {}
    return data


def save_source_health(state: dict, path: Path = None) -> None:
    path = Path(path) if path is not None else SOURCE_HEALTH_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        print(f"source_health state write failed: {exc}", file=sys.stderr)


def _apply_source_status(rec: dict, status: str, now: datetime) -> None:
    iso = now.astimezone(timezone.utc).isoformat()
    if status == "ok":
        rec["last_ok"] = iso
        rec["consecutive_failures"] = 0
        rec["consecutive_empties"] = 0
        rec["unhealthy_since"] = None
        return
    if status == "error":
        rec["consecutive_failures"] += 1
        rec["consecutive_empties"] = 0
    else:
        rec["consecutive_empties"] += 1
        rec["consecutive_failures"] = 0
    if _parse_health_time(rec.get("unhealthy_since")) is None:
        rec["unhealthy_since"] = iso


def _source_alert_due(rec: dict, now: datetime) -> bool:
    since = _parse_health_time(rec.get("unhealthy_since"))
    if since is None or now - since < SOURCE_HEALTH_ALERT_AFTER:
        return False
    last = _parse_health_time(rec.get("last_alert_at"))
    if last is not None and now - last < SOURCE_HEALTH_ALERT_AFTER:
        return False
    return True


def _fire_source_health_alert(source: str, status: str, rec: dict) -> bool:
    """One admin ping per source per 24h, or a WARN line when no admin chat is configured.

    Uses the existing MODELBYTES_ADMIN_CHAT_ID → send_ops_alert path. Does not
    invent a destination. Returns True when the alert was delivered or logged.
    """
    since = rec.get("unhealthy_since") or ""
    if ADMIN_CHAT_ID:
        text = (
            f"Source {source} has been {status} for 24h+ "
            f"(since {since}; "
            f"consecutive_failures={rec.get('consecutive_failures')}, "
            f"consecutive_empties={rec.get('consecutive_empties')})."
        )
        try:
            delivered = send_ops_alert(text)
        except Exception as exc:
            print(
                "source_health ALERT WARN "
                f"source={source} status={status} since={since} "
                f"error={_short_source_error(exc)}",
                file=sys.stderr,
            )
            return False
        if not delivered:
            print(
                "source_health ALERT WARN "
                f"source={source} status={status} since={since} error=undelivered",
                file=sys.stderr,
            )
            return False
        print(
            f"source_health ALERT source={source} status={status} since={since}",
            file=sys.stderr,
        )
        return True
    print(
        "source_health ALERT WARN "
        f"source={source} status={status} since={since} "
        f"consecutive_failures={rec.get('consecutive_failures')} "
        f"consecutive_empties={rec.get('consecutive_empties')}",
        file=sys.stderr,
    )
    return True


def flush_source_health(rows, *, preview: bool = False, now: datetime = None,
                        path: Path = None) -> list:
    """Log one health line per row. Persist streaks and maybe alert unless preview."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    path = Path(path) if path is not None else SOURCE_HEALTH_PATH
    lines = []
    state = {} if preview else load_source_health(path)
    dirty = False
    for row in rows or []:
        source = row.get("source") or "unknown"
        items = int(row.get("items") or 0)
        error = (row.get("error") or "").strip()
        track = row.get("track", True)
        if not track:
            status = row.get("status") or "ok"
            line = format_source_health_line(source, status, items, error or "disabled")
            print(line, file=sys.stderr)
            lines.append(line)
            continue
        status = classify_source_health(items, error)
        line = format_source_health_line(source, status, items, error)
        print(line, file=sys.stderr)
        lines.append(line)
        if preview:
            continue
        rec = _coerce_source_record(state.get(source))
        _apply_source_status(rec, status, now)
        if status != "ok" and _source_alert_due(rec, now):
            if _fire_source_health_alert(source, status, rec):
                rec["last_alert_at"] = now.isoformat()
        state[source] = rec
        dirty = True
    if dirty and not preview:
        save_source_health(state, path)
    return lines


def emit_writer_health(candidates: int, included: int) -> str:
    """One line: candidates handed to the writer vs entries in the digest body."""
    line = format_writer_health_line(
        candidates, included, LAST_LINK_DROPPED, LAST_STALE_DROPPED,
    )
    print(line, file=sys.stderr)
    return line


def ping_heartbeat(ok: bool, message: str = "") -> None:
    """Dead-man's switch: ping MODELBYTES_HEARTBEAT_URL (e.g. healthchecks.io)
    on every run; /fail on failures. The external service alerts when pings
    stop entirely — the one failure class in-process alerts can't catch
    (cron never fired, container never started). No-op without the env var;
    never raises."""
    if not HEARTBEAT_URL:
        return
    url = HEARTBEAT_URL if ok else f"{HEARTBEAT_URL}/fail"
    try:
        requests.post(url, data=_redact_secrets(message)[:1000].encode("utf-8"),
                      timeout=10)
    except Exception as e:
        print(_redact_secrets(f"Heartbeat ping failed: {e}"), file=sys.stderr)


def _ensure_publish_runs_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS publish_runs (
            id SERIAL PRIMARY KEY,
            run_at TIMESTAMPTZ DEFAULT NOW(),
            post_date DATE NOT NULL,
            mode VARCHAR(30) NOT NULL,
            status VARCHAR(20) NOT NULL,
            models_found INTEGER,
            models_emitted INTEGER,
            message_chars INTEGER,
            telegram_message_id BIGINT,
            slack_ok BOOLEAN,
            error TEXT
        )
    """)


_publish_runs_ensured = False


def record_publish_run(post_date: str, mode: str, status: str,
                       models_found: int = None, models_emitted: int = None,
                       message_chars: int = None, telegram_message_id=None,
                       slack_ok=None, error: str = None) -> bool:
    """One row per run — posted, blocked, failed, skipped, no-models, seeded —
    so 'why was yesterday weird' is a SQL query, not a log archaeology dig.
    Best-effort; never raises."""
    global _publish_runs_ensured
    if not DATABASE_URL:
        return False
    try:
        conn = _db_connect(connect_timeout=10)
        try:
            with conn.cursor() as cur:
                if not _publish_runs_ensured:
                    _ensure_publish_runs_table(cur)
                    _publish_runs_ensured = True
                cur.execute(
                    """
                    INSERT INTO publish_runs
                        (post_date, mode, status, models_found, models_emitted,
                         message_chars, telegram_message_id, slack_ok, error)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (post_date, mode, status, models_found, models_emitted,
                     message_chars, telegram_message_id, slack_ok,
                     _redact_secrets(error) if error else None),
                )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        print(_redact_secrets(f"Could not record publish run: {e}"), file=sys.stderr)
        return False


def fallback_streak() -> int:
    """Consecutive most-recent posted days whose source was not 'curated'.
    Powers escalating degradation alerts. 0 on any failure."""
    if not DATABASE_URL:
        return 0
    try:
        conn = _db_connect(connect_timeout=10)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT source FROM posted_digests ORDER BY post_date DESC LIMIT 14")
                streak = 0
                for (source,) in cur.fetchall():
                    if source == "curated":
                        break
                    streak += 1
                return streak
        finally:
            conn.close()
    except Exception:
        return 0


def load_seen_models() -> Set[str]:
    """Load the set of seen model IDs from Postgres. Empty set without DATABASE_URL."""
    if not DATABASE_URL:
        return set()
    conn = _db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT model_id FROM models")
            return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


def save_seen_models(models: Set[str]):
    """Persist the set of seen model IDs to Postgres. No-op without DATABASE_URL."""
    if not DATABASE_URL or not models:
        return
    conn = _db_connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO models (model_id, name) VALUES (%s, %s) "
                "ON CONFLICT (model_id) DO NOTHING",
                [(m, m) for m in models],
            )
        conn.commit()
    finally:
        conn.close()


def _resolve_provider(raw: str, model_id: str = "") -> str:
    key = (raw or "").lower().strip()
    if key:
        return PROVIDER_NAMES.get(key, raw)
    # Fallback: extract from model ID namespace
    if "/" in model_id:
        ns = model_id.split("/")[0].lower()
        return PROVIDER_NAMES.get(ns, ns)
    return "unknown"


def _smart_truncate(text: str, max_len: int = 150) -> str:
    """Truncate at sentence or word boundary, never mid-word."""
    if not text or len(text) <= max_len:
        return text or ""
    # Try to cut at sentence boundary
    truncated = text[:max_len]
    for boundary in ['. ', '! ', '? ', '; ', '\n']:
        idx = truncated.rfind(boundary)
        if idx > max_len * 0.5:
            return truncated[:idx + 1].strip()
    # Fallback: word boundary
    idx = truncated.rfind(' ')
    if idx > max_len * 0.3:
        return truncated[:idx].strip()
    return truncated.strip()


def _compact_match_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _fact_matches_text(fact: ModelFact, text: str) -> bool:
    haystack = (text or "").lower()
    compact = _compact_match_text(text)
    for alias in fact.aliases:
        if alias.lower() in haystack or _compact_match_text(alias) in compact:
            return True
    return False


def _known_fact_for(text: str) -> Optional[ModelFact]:
    for fact in KNOWN_MODEL_FACTS:
        if _fact_matches_text(fact, text):
            return fact
    return None


def _license_from_traits(traits: List[str]) -> Optional[str]:
    for trait in traits or []:
        if trait.startswith("license:"):
            slug = trait.split(":", 1)[1].strip()
            if slug:
                return (
                    slug.replace("-", " ").upper()
                    if slug.lower() in {"mit"}
                    else slug.replace("-", " ").title()
                )
    return None


def enrich_model_metadata(model: ModelRelease) -> ModelRelease:
    """Attach known canonical facts and confidence without inventing missing specs."""
    fact = _known_fact_for(model.name)
    if fact:
        model.release_date = model.release_date or fact.release_date
        model.license = model.license or fact.license
        model.total_parameters = model.total_parameters or fact.total_parameters
        model.active_parameters = model.active_parameters or fact.active_parameters
        model.canonical_url = model.canonical_url or fact.canonical_url
        model.confidence = fact.confidence
    else:
        model.license = model.license or _license_from_traits(model.unique_traits)
        if model.is_open_source is False and not model.license:
            model.license = "Closed/API"
        if not model.confidence:
            model.confidence = "medium"

    notes = []
    if not model.url and not model.canonical_url:
        notes.append("missing source URL")
    if not model.release_date:
        notes.append("missing release date")
    if not model.license:
        notes.append("missing license")
    if not model.total_parameters:
        notes.append("unknown total parameters")
    if not model.active_parameters:
        notes.append("unknown active parameters")
    model.validation_notes = notes

    if not (model.url or model.canonical_url) or not model.release_date:
        model.confidence = "low"
    elif notes:
        model.confidence = "medium" if model.confidence != "high" else model.confidence
    else:
        model.confidence = "high"
    return model


def prepare_models_for_digest(models: List[ModelRelease]) -> Tuple[List[ModelRelease], List[str]]:
    prepared = [enrich_model_metadata(m) for m in models]
    notes = []
    for model in prepared:
        if model.validation_notes:
            notes.append(
                f"{model.name}: {', '.join(model.validation_notes)} "
                f"(confidence={model.confidence})"
            )
    return prepared, notes


def _normalize_known_fact_claims(message: str) -> Tuple[str, List[str]]:
    """Fix high-confidence factual slips before a pending digest can publish."""
    corrections = []
    original = message
    if _fact_active(ZAYA_FACT) and _fact_matches_text(ZAYA_FACT, message):
        message = re.sub(
            r"\b8(?:\.0)?B\s+active\s+parameters\b",
            "8.4B total / 760M active parameters",
            message,
            flags=re.IGNORECASE,
        )
        message = re.sub(
            r"\b8(?:\.0)?B\s+active\s+params\b",
            "8.4B total / 760M active params",
            message,
            flags=re.IGNORECASE,
        )
        message = re.sub(
            r"\b8(?:\.0)?B\s+active\b",
            "8.4B total / 760M active",
            message,
            flags=re.IGNORECASE,
        )
    if message != original:
        corrections.append("corrected ZAYA1-8B active/total parameter wording")
    return message, corrections


# Tags Telegram's HTML parser accepts (sending others 400s the message) vs the
# narrower v3 editorial subset. Outside TELEGRAM_OK → error; inside TELEGRAM_OK
# but outside v3 → warning. Two lists so the QA layer never rejects content the
# send layer would deliver fine.
_TELEGRAM_OK_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
                     "a", "code", "pre", "span", "blockquote", "tg-spoiler"}
_V3_TAGS = {"b", "i", "a"}
_V3_TIERS = ("OPEN FRONTIER", "CLOSED FRONTIER", "SPECIALIZED", "LOCAL", "WATCH")
_AGGREGATOR_DOMAINS = (
    "techtimes.com", "tomsguide.com", "ndtv.com", "benzinga.com", "msn.com",
    "yahoo.com", "dailymail.co.uk", "businessinsider.com", "marketwatch.com",
    "digitaltrends.com", "zdnet.com",
    # Release-tracker sites that Parallel.ai cites as "sources"; warn so the
    # operator sees the writer didn't land on a primary vendor URL.
    "aireleasetracker.com", "llm-stats.com",
    # Comparison listicles and roundup blogs. #218 (Sep 29) invented
    # "Astra = Gemini" from one tech-insider.org article; the same class of
    # page supplied promptzone / benchlm / hyper.ai links in #219–#223.
    "tech-insider.org", "promptzone.com", "benchlm.ai", "hyper.ai",
)
_QUANT_NAME_RE = re.compile(r"(?i)\b(gguf|awq|gptq|onnx|imatrix|exl2)\b|-bnb-")
# An entry is a line-leading bold name — with a dash tail (curated/LLM grammar)
# or bare (deterministic template puts specs on following lines). Tier headers
# ("━━━ <b>…") and the 🤖 header line don't start with <b> so they don't match.
_ENTRY_RE = re.compile(r"^(?:• )?<b>([^<]+)</b>\s*(?:[—-]|$)", re.MULTILINE)
_HREF_RE = re.compile(r'<a href="([^"]*)"')


def _upgrade_http_url(url: str) -> str:
    """http:// → https://. Case-insensitive on the scheme; everything else
    is left untouched (javascript:/data:/relative/https)."""
    if url.lower().startswith("http://"):
        return "https://" + url.split("://", 1)[1]
    return url


def _url_scheme_variants(url: str) -> List[str]:
    """Both http and https forms of a URL, trailing slash stripped.

    The writer copies source URLs character-for-character, but discovery may
    have upgraded the scheme (or the writer may still emit http:// of an
    https:// source). Scheme-only mismatch must not look like a constructed
    URL and get stripped by _strip_unverified_links.
    """
    u = url.rstrip("/").rstrip(".,")
    variants = {u}
    if u.lower().startswith("http://"):
        variants.add("https://" + u.split("://", 1)[1])
    elif u.lower().startswith("https://"):
        variants.add("http://" + u.split("://", 1)[1])
    return list(variants)


def _href_is_provided(href: str, allowed_urls: set) -> bool:
    """True if href is a URL we handed the writer, or a path/query suffix of one.

    Exact match (scheme variants, trailing slash) plus a suffix like
    huggingface.co/org/model vs huggingface.co/org/model/tree/main. A sibling
    slug (Model-1-instruct vs Model-1) is NOT a suffix — require '/', '?', or
    '#' after the allowed URL.
    """
    candidates = {_upgrade_http_url(u).rstrip("/")
                  for u in _url_scheme_variants(href)}
    allowed_norm = {_upgrade_http_url(u).rstrip("/")
                    for u in (allowed_urls or set())}
    if candidates & allowed_norm:
        return True
    for h in candidates:
        for a in allowed_norm:
            if a and (h.startswith(a + "/") or h.startswith(a + "?")
                      or h.startswith(a + "#")):
                return True
    return False


def _upgrade_insecure_hrefs(body: str) -> Tuple[str, int]:
    """Rewrite http:// hrefs to https://.

    Telegram renders http:// fine, so this is not channel-harm — but the
    fallback QA gate treats remaining non-https as an ERROR and would take
    the whole digest dark (2026-08-13: two aggregator http:// links from
    Parallel.ai research blocked the day's post). Upgrading the scheme is
    the per-link counterpart to the stale-date scrub: one bad scheme must
    not sink the digest. javascript:/data:/relative hrefs are left for the
    gate. Returns (rewritten, count).
    """
    n = 0

    def repl(m):
        nonlocal n
        href = m.group(1)
        upgraded = _upgrade_http_url(href)
        if upgraded != href:
            n += 1
            return f'<a href="{upgraded}"'
        return m.group(0)

    return _HREF_RE.sub(repl, body), n


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def _loose_release_dates(body: str, today: str = None) -> List[str]:
    """Find release dates in either digest dialect: ISO ('Released: 2026-06-01',
    deterministic template) or prose ('Released Apr 7', LLM grammar — which has
    no year, so assume the most recent occurrence not in the future).

    `today` pins the reference date for the yearless prose form so a caller
    (the per-entry stale scrub) can agree with is_stale_release by construction;
    the whole-body gate passes None and uses the real UTC date."""
    found = list(re.findall(r"Released:? (\d{4}-\d{2}-\d{2})", body))
    ref = (datetime.strptime(today, "%Y-%m-%d").date() if today
           else datetime.now(timezone.utc).date())
    for mon, day in re.findall(r"Released:? ([A-Z][a-z]{2})[a-z]* (\d{1,2})\b", body):
        month = _MONTHS.get(mon)
        if not month:
            continue
        try:
            candidate = ref.replace(month=month, day=int(day))
        except ValueError:
            continue
        if (candidate - ref).days > 35:
            candidate = candidate.replace(year=candidate.year - 1)
        found.append(candidate.strftime("%Y-%m-%d"))
    return found


class _TagAudit(HTMLParser):
    """Count open/close of the v3 tag subset for balance; record any tag
    outside it (the caller classifies those into warn-vs-error by whether
    Telegram itself would accept them)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.open_counts = {}
        self.close_counts = {}
        self.disallowed = set()

    def handle_starttag(self, tag, attrs):
        if tag in _V3_TAGS:
            self.open_counts[tag] = self.open_counts.get(tag, 0) + 1
        else:
            self.disallowed.add(tag)

    def handle_endtag(self, tag):
        if tag in _V3_TAGS:
            self.close_counts[tag] = self.close_counts.get(tag, 0) + 1
        else:
            self.disallowed.add(tag)


def _lint_digest_structure(body: str, mode: str) -> Tuple[List[str], List[str]]:
    """Structural gate. ERROR = would harm the channel (Telegram 400s on bad
    HTML, non-https links, fallback floods). WARNING = v3 format drift —
    publishing an imperfect curated digest beats replacing it with the
    fallback, so drift is surfaced to the operator, never censored."""
    warnings, errors = [], []

    audit = _TagAudit()
    try:
        audit.feed(body)
    except Exception:
        errors.append("unparseable HTML markup")
        return warnings, errors
    for tag in sorted(audit.disallowed):
        if tag in _TELEGRAM_OK_TAGS:
            warnings.append(f"tag <{tag}> is outside the v3 subset (b/i/a)")
        else:
            errors.append(f"tag <{tag}> would 400 at Telegram")
    for tag in _V3_TAGS:
        if audit.open_counts.get(tag, 0) != audit.close_counts.get(tag, 0):
            errors.append(f"unbalanced <{tag}> tags (Telegram would reject the message)")

    # Stray '<' in prose — e.g. 'under <100B params' or '5 < 10'. Python's
    # html.parser doesn't treat a bare '<' as a tag, so the balance check above
    # misses it; Telegram's strict parser returns 'Unclosed start tag at byte
    # offset N' and 400s (incident 2026-06-21). Any '<' that isn't part of a
    # known open/close tag is channel-harm.
    _ok_tag = re.compile(
        r"</?(?:" + "|".join(sorted(_TELEGRAM_OK_TAGS)) + r")\b[^>]*>",
        re.IGNORECASE,
    )
    if "<" in _ok_tag.sub("", body):
        errors.append("stray '<' in prose (Telegram would reject: unclosed start tag)")

    for href in _HREF_RE.findall(body):
        if not href.startswith("https://"):
            # http:// has already been rewritten to https:// by
            # _upgrade_insecure_hrefs (2026-08-13). Remaining non-https
            # (javascript:/data:/relative) is still a fallback ERROR /
            # curated WARNING. Telegram renders http:// fine — not
            # channel-harm — so we rewrite rather than take the digest dark.
            (errors if mode == "fallback" else warnings).append(
                f"non-https link: {href[:80]}")
        if href.startswith(("http://", "https://")):
            domain = href.split("/", 3)[2].lower()
            if any(domain == d or domain.endswith("." + d) for d in _AGGREGATOR_DOMAINS):
                warnings.append(f"aggregator-sourced link ({domain}) — cite the primary source")

    entries = _ENTRY_RE.findall(body)
    if "━━━" in body or mode == "curated":
        if not any(t in body for t in _V3_TIERS) and entries:
            warnings.append("no recognized v3 tier header")
        for header in re.findall(r"━━━ <b>([^<]+)</b>", body):
            if header.strip() not in _V3_TIERS:
                warnings.append(f"unrecognized tier header: {header.strip()}")

    # Entry grammar: each entry block should carry an italic differentiator
    # and a link (v3 contract). Blocks are entry-start → blank line.
    for block in re.split(r"\n\s*\n", body):
        m = _ENTRY_RE.search(block)
        if not m:
            continue
        name = m.group(1).strip()
        if "<i>" not in block:
            warnings.append(f"entry '{name}' missing the italic differentiator sentence")
        if "<a href" not in block:
            warnings.append(f"entry '{name}' has no source link")

    footer_match = re.search(r"Total: (\d+) items? tracked today", body)
    if (mode == "curated" and footer_match and entries
            and int(footer_match.group(1)) != len(entries)):
        warnings.append(f"footer says {footer_match.group(1)} items but "
                        f"{len(entries)} entries found")

    if entries and not _DATELINE_RE.search(body):
        warnings.append("no parseable dateline — the deterministic date "
                        "rewrite could not run")

    # Flood/staleness/quant checks run in EVERY mode — these harms damage the
    # channel identically regardless of author. Severity differs: errors for
    # machine-assembled fallback content, warnings (→ ops alert) for curated,
    # because replacing a flawed curated digest with the fallback is worse.
    flood_sink = errors if mode == "fallback" else warnings
    if len(entries) > DIGEST_LIMIT:
        flood_sink.append(f"flood: {len(entries)} entries exceeds the "
                          f"{DIGEST_LIMIT}-model cap")
    for name in entries:
        if _QUANT_NAME_RE.search(name):
            flood_sink.append(f"quant/serving artifact leaked into digest: {name}")
    for date_str in _loose_release_dates(body):
        # Digest news window, not the 14-day catalog/forwarding window.
        if is_stale_release(date_str, max_age_days=DIGEST_FRESHNESS_DAYS):
            flood_sink.append(f"stale release date in a 'new today' digest: {date_str}")

    return warnings, errors


_PARAM_CLAIM_RE = re.compile(
    r"~?\s*([\d.]+)\s*([BMT])\s+(total|active)", re.IGNORECASE)
# Tight markers only: loose substrings like 'was ' / 'updat' match ordinary
# prose ('was trained on…') and would suppress real contradictions.
_CORRECTION_MARKERS = ("correct", "previously reported", "previously stated",
                       "revised", "updated from", "was wrong")


def _extract_fact_claims(body: str) -> Tuple[dict, dict]:
    """Return (entry name → {total/active: value}, entry name → its block)."""
    claims, blocks = {}, {}
    for block in re.split(r"\n\s*\n", body):
        m = _ENTRY_RE.search(block)
        if not m:
            continue
        name = m.group(1).strip()
        blocks[name] = block
        for value, unit, kind in _PARAM_CLAIM_RE.findall(block):
            claims.setdefault(name, {})[kind.lower()] = f"{value}{unit.upper()}"
    return claims, blocks


def _check_fact_consistency(body: str, pending_dir: Path = None,
                            today: str = None) -> List[str]:
    """Flag entries whose param claims contradict the MOST RECENT prior figure
    we published (last 14 digests, today's own post excluded) without an explicit
    correction marker. Would have caught MiniMax M3 going from 229.9B/9.8B
    (Jun 9, 11) to ~428B/23B (Jun 12) silently.

    History prefers posted_digests.body (what readers saw; survives the
    ephemeral Railway cron). pending/*.txt fills dates the DB has no body for.
    """
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_claims, today_blocks = _extract_fact_claims(body)
    if not today_claims:
        return []
    warnings = []
    resolved = set()  # (name, kind) pairs already judged against the most recent prior
    for date_str, prior_body in _digest_history(
            today=today, days=14, pending_dir=pending_dir):
        prior, _ = _extract_fact_claims(prior_body)
        for name, today_vals in today_claims.items():
            prior_vals = prior.get(name)
            if not prior_vals:
                continue
            for kind, today_v in today_vals.items():
                if (name, kind) in resolved:
                    continue
                prior_v = prior_vals.get(kind)
                if not prior_v:
                    continue
                resolved.add((name, kind))
                if prior_v != today_v:
                    block = today_blocks.get(name, "").lower()
                    if not any(mk in block for mk in _CORRECTION_MARKERS):
                        warnings.append(
                            f"fact drift for {name}: {kind} params {today_v} today "
                            f"vs {prior_v} in {date_str} — mark corrections explicitly")
    return sorted(set(warnings))


def validate_digest_for_publish(message: str, mode: str = "curated") -> Tuple[str, List[str], List[str]]:
    """Return normalized message plus warnings/errors for pre-publish QA.

    mode='curated' (default) | 'fallback' — the fallback gets stricter flood
    tripwires because its content is machine-assembled.
    """
    warnings = []
    errors = []
    normalized = (message or "").strip()
    normalized, corrections = _normalize_known_fact_claims(normalized)
    warnings.extend(corrections)
    normalized, n_upgraded = _upgrade_insecure_hrefs(normalized)
    if n_upgraded:
        warnings.append(
            f"upgraded {n_upgraded} http:// href(s) to https://")

    if not normalized:
        errors.append("digest body is empty")
        return normalized, warnings, errors
    if "ModelBytes Digest" not in normalized:
        warnings.append("digest header is missing")
    _lower = normalized.lower()
    if ("items tracked today" not in _lower
            and "models tracked today" not in _lower
            and "scanned" not in _lower):
        warnings.append("tracked-model footer is missing")

    lint_warnings, lint_errors = _lint_digest_structure(normalized, mode)
    warnings.extend(lint_warnings)
    errors.extend(lint_errors)
    warnings.extend(_check_fact_consistency(normalized))

    for fact in KNOWN_MODEL_FACTS:
        if not _fact_active(fact) or not _fact_matches_text(fact, normalized):
            continue
        if fact.canonical_url not in normalized:
            warnings.append(f"{fact.canonical_name}: canonical source URL missing")
        if fact.license and fact.license.lower() not in normalized.lower():
            warnings.append(f"{fact.canonical_name}: license not stated")
        if fact.total_parameters and fact.total_parameters.lower() not in normalized.lower():
            warnings.append(f"{fact.canonical_name}: total parameter count not stated")
        if fact.active_parameters and fact.active_parameters.lower() not in normalized.lower():
            warnings.append(f"{fact.canonical_name}: active parameter count not stated")

    if (_fact_matches_text(ZAYA_FACT, normalized)
            and re.search(r"\b8(?:\.0)?B\s+active\b", normalized, flags=re.IGNORECASE)):
        if _fact_active(ZAYA_FACT):
            errors.append("ZAYA1-8B still has an incorrect 8B-active-parameter claim")
        else:
            # Expired facts stop blocking/rewriting, but a known-bad claim
            # reappearing should never be silent.
            warnings.append("ZAYA1-8B '8B active' claim matched after fact "
                            "expiry — verify before next publish")

    return normalized, warnings, errors


def _retry_delay(response, attempt: int) -> float:
    retry_after = None
    if response is not None:
        retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(float(retry_after), 30.0)
        except ValueError:
            pass
    return min(HTTP_BACKOFF_SECONDS * attempt, 30.0)


def _http_get(url: str, source_name: str, timeout: int = 30, **kwargs):
    """GET with a consistent user-agent and light retries for flaky source APIs."""
    attempts = max(1, HTTP_RETRIES)
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("User-Agent", HTTP_USER_AGENT)
    headers.setdefault("Accept", "application/json, text/html;q=0.9, */*;q=0.8")

    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, timeout=timeout, headers=headers, **kwargs)
            status = getattr(resp, "status_code", None)
            if status in RETRYABLE_STATUS_CODES and attempt < attempts:
                delay = _retry_delay(resp, attempt)
                print(
                    f"{source_name} HTTP {status}; retrying "
                    f"{attempt + 1}/{attempts} in {delay:.1f}s.",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status is not None and status not in RETRYABLE_STATUS_CODES:
                raise
            if attempt >= attempts:
                raise
            delay = _retry_delay(getattr(e, "response", None), attempt)
            print(
                f"{source_name} request failed ({e}); retrying "
                f"{attempt + 1}/{attempts} in {delay:.1f}s.",
                file=sys.stderr,
            )
            time.sleep(delay)


def _format_context(ctx: Optional[int]) -> str:
    if not ctx:
        return ""
    if ctx >= 1_000_000:
        return f"{ctx/1_000_000:.1f}M"
    if ctx >= 1000:
        return f"{ctx/1000:.0f}k"
    return f"{ctx:,}"


# Classical-ML task tags. These are fine-tune and embedder spam, not a
# release a builder would miss. Image / video / audio / speech tags are
# absent on purpose — see _RELEASE_MODALITY_TAGS.
_COMMODITY_TASKS = frozenset({
    "image-classification",
    "object-detection",
    "audio-classification",
    "video-classification",
    "table-to-text",
    "fill-mask",
    "feature-extraction",
    "sentence-similarity",
    "zero-shot-classification",
    "token-classification",
    "translation",
    "summarization",
    "depth-estimation",
})

# Generative and multimodal releases. HF often stamps one of these beside
# a commodity tag (facebook/sam3 is mask-generation and feature-extraction).
# The release tag wins; GGUF / quant / LoRA name rules still run first.
_RELEASE_MODALITY_TAGS = frozenset({
    "text-to-image",
    "image-to-image",
    "image-text-to-image",
    "image-to-text",
    "image-text-to-text",
    "visual-question-answering",
    "document-question-answering",
    "unconditional-image-generation",
    "mask-generation",
    "image-segmentation",
    "zero-shot-image-classification",
    "image-to-3d",
    "text-to-3d",
    "text-to-video",
    "image-to-video",
    "video-to-video",
    "image-text-to-video",
    "text-to-audio-video",
    "image-to-audio-video",
    "image-text-to-audio-video",
    "video-to-audio-video",
    "audio-to-audio-video",
    "audio-to-video",
    "text-to-speech",
    "text-to-audio",
    "automatic-speech-recognition",
    "music-generation",
    "audio-to-audio",
    "voice",
    "any-to-any",
    "video-text-to-text",
})

# Substring bans for classical CV / encoder spam. Audio and multimodal
# architectures (whisper, sam, clip, vits, wav2vec) are not in this list —
# "sam" was dropping facebook/sam3 and "vit" is scrubbed out of "vits"
# below so a TTS model is not classified as a ViT.
_SPAM_ARCHITECTURES = (
    "resnet", "efficientnet", "mobilenet", "yolo", "detr",
    "bert", "roberta", "distilbert", "albert", "vit",
)


def _spam_architecture(model_lower: str) -> bool:
    scrubbed = model_lower.replace("vits", "")
    return any(arch in scrubbed for arch in _SPAM_ARCHITECTURES)


def is_noise_model(model_id: str, author: str, tags: list,
                   downloads: int = 0, likes: int = 0, *,
                   engagement_floor: bool = True) -> bool:
    """Filter out noise. Returns True = skip this model."""
    # Defensive coercion: HF/fetchers occasionally hand back a string (or None)
    # for engagement counts. A TypeError here would crash the fallback publish
    # path, so coerce to int (missing/unparseable → 0) before any comparison.
    try:
        downloads = int(downloads or 0)
    except (TypeError, ValueError):
        downloads = 0
    try:
        likes = int(likes or 0)
    except (TypeError, ValueError):
        likes = 0
    model_lower = model_id.lower()
    author_lower = (author or "").lower()
    tags_lower = [t.lower() for t in tags]
    model_name = model_id.split("/")[-1] if "/" in model_id else model_id
    model_name_lower = model_name.lower()
    author_prefix = author_lower.split("/")[0] if "/" in author_lower else author_lower

    # Junk patterns — note: '-gguf' and '-base' can be false positives for known orgs
    junk = ["tiny-random", "test", "dummy", "example", "demo", "sample",
            "random", "placeholder", "minimal", "toy-",
            "lora-", "-lora", "-loras", "_lora",
            "-onnx", "_onnx", "-awq", "-gptq",
            "-fp16", "-bf16", "-int8", "-int4",
            # Serving/quant builds and speculative-decoding draft heads —
            # derivative artifacts of an already-released model (06-11 leak:
            # command-a-plus-…-w4a4/-fp8, Kimi-…-Eagle3).
            "-fp8", "-fp4", "-w4a4", "-w8a8", "-w4a16", "-w8a16",
            "nvfp4", "qat-mobile",  # FP4 quant + QAT mobile-packaging variants
            "-eagle", "_eagle", "-mtp", "-draft-head",
            # Abliteration / "uncensored" fine-tunes — derivative artifacts of a
            # base model (06-13 inline leak: OBLITERATED, Uncensored-Aggressive).
            "obliterated", "abliterated", "uncensored",
            "_ftjob_", "-merged", ".onnx",
            "-distilled", "-distill", "_distilled", "_distill",
            "moved", "deprecated", "archived", "old", "backup",
            "_length", "stella", "text2sql", "_calculator",
            "_seed", "_bs", "_epoch", "_step", "_checkpoint",
            "-finetuned", "-finetune", "_finetuned",
            # RL / preference / SFT training variants — these are derivative
            # artifacts of a base model, not standalone releases. Filter them
            # even from KNOWN_ORGS (e.g. open-thoughts/...-SFT-100K variant spam).
            "-sft", "_sft", "-dpo", "_dpo", "-grpo", "_grpo",
            "-orpo", "-kto", "-rlhf", "-ppo", "_ppo", "-rlaif",
            "-classifier", "_classifier",
            "-email-", "-spam-", "-sentiment-",
            "_micn_", "_lr", "-bsz", "_bsz",
            "-local", "-dev", "-dev1", "-dev2", "-exp", "-exp1", "-exp2",
            "-draft", "-wip", "-wip1", "-wip2", "-wip3"]
    
    # GGUF is a quant repackage, never a primary release — even from known
    # orgs, and even when the name hides it and only the HF tag says gguf
    # (image turbo packs). A known-org GGUF used to reach publish-QA and
    # block the whole digest (unsloth/diffusiongemma-…-GGUF).
    if "-gguf" in model_lower or "_gguf" in model_lower or "gguf" in tags_lower:
        return True

    # '-base' as standalone suffix = classifier noise, but 'X-2-base' = real model
    if "-base" in model_lower:
        # Only flag as noise if '-base' is the LAST segment (classifier pattern)
        if re.search(r'-base$', model_lower) and not re.search(r'\d-base$', model_lower):
            # '-base' at end not preceded by digit = classifier noise
            # BUT known orgs releasing '-base' variants (e.g., DeepSeek-V4-Pro-Base) are fine
            if author_prefix not in KNOWN_ORGS and "deepseek" not in model_lower:
                return True
    
    if any(p in model_lower for p in junk):
        return True

    # Commodity tasks only. A release-modality tag on the same card wins
    # (mask-generation + feature-extraction, text-to-video + a side tag).
    if not any(t in _RELEASE_MODALITY_TAGS for t in tags_lower):
        if any(t in _COMMODITY_TASKS for t in tags_lower):
            return True

    if _spam_architecture(model_lower):
        return True

    # Unknown orgs: strict engagement gate. The new-on-HF pass passes
    # engagement_floor=False and applies its own likes/downloads floor, so a
    # mid-tier unknown org is not rejected here (Naive-N0.5-Flash, 165 likes).
    if engagement_floor and author_prefix not in KNOWN_ORGS:
        # Numeric/throwaway usernames
        if author_lower and re.match(r'^[a-z]*\d{3,}', author_lower):
            return True
        # Very short model names (like "co", "a", "b")
        if len(model_name) <= 2:
            return True
        # Sequential numbered variants without size markers (baobae3, baobae4)
        if re.search(r'\d+$', model_name_lower):
            if not re.search(r'-(?:\d+b|small|medium|large|xl|xxl|mini|nano|micro)', model_name_lower):
                return True
        # Require significant engagement — lower threshold for trending orgs
        # (HF trending already vouches, so just need some signal)
        if likes < 100 and downloads < 5000:
            return True
        # Mid-tier: allow if EITHER likes or downloads is decent
        if likes < 300 and downloads < 20000:
            # Still allow if the model name matches a known family
            known_families = ["qwen", "llama", "mistral", "deepseek", "gemma", "phi",
                              "yi-", "falcon", "glm", "grok", "claude", "gpt",
                              "command", "nemotron", "olmo", "solar", "granite",
                              "sulphur", "hidream", "zamba", "minicpm", "devstral",
                              "voxtral", "leanstral", "arcee"]
            if not any(f in model_lower for f in known_families):
                return True

    return False


# How old a dated release can be and still be "today's news" in the digest.
# Age is (digest day - release day). Age 3 is kept so a Friday launch is still
# in Monday's digest; age 4 is not (Holo4 on Sep 28 was 4 days old, LTX 2.5
# on Oct 6 was 4 days old). Catalog backfill and release-forwarding keep the
# 14-day default on is_stale_release — this constant is the publish window.
DIGEST_FRESHNESS_DAYS = 3


def is_stale_release(release_date, today: str = None, max_age_days: int = 14) -> bool:
    """True when a model's release date is too old to count as news.

    Guards against new-org backfill: when the supervisor adds an org to the
    fetch lists, that org's entire back-catalog is unseen by the dedup DB and
    would flood the digest as "new" (2026-06-11: Kimi-VL from 2025-04 appeared
    in a "new today" digest). Unknown or unparseable dates are kept — absence
    of a date is not evidence of staleness.

    The default window (14 days) is the catalog / release-forwarding clock.
    The digest uses DIGEST_FRESHNESS_DAYS so a release is not presented as
    today's news once it is older than that.
    """
    if not release_date:
        return False
    try:
        released = datetime.strptime(str(release_date)[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    ref = (datetime.strptime(today, "%Y-%m-%d").date() if today
           else datetime.now(timezone.utc).date())
    return (ref - released).days > max_age_days


def digest_release_is_stale(release_date, today: str = None) -> bool:
    """True when a dated release is outside the digest news window.

    Unknown or unparseable dates are kept, same as is_stale_release.
    """
    return is_stale_release(
        release_date, today=today, max_age_days=DIGEST_FRESHNESS_DAYS)


_MONTH_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# How many HF commit lookups to spend on "repo is old, lastModified is fresh"
# before the stale drop. Enough for a day's real launches (Kumo, AstaBrief)
# without walking every org's back catalog.
HF_RECENCY_COMMIT_BUDGET = 20
HF_NEW_MIN_LIKES = 50
HF_NEW_MIN_DOWNLOADS = 500
HF_NEW_DETAIL_CAP = 40
HF_NEW_TRENDING_LIMIT = 100
_COMMIT_GAP_DAYS = 30


def _day(value) -> Optional[str]:
    """Best-effort calendar day (YYYY-MM-DD) from an ISO, RSS, or 'July 9, 2026' string."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        datetime.strptime(text[:10], "%Y-%m-%d")
        return text[:10]
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            return parsed.date().isoformat()
    except (TypeError, ValueError, IndexError, OverflowError):
        pass
    match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|"
        r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|"
        r"Oct|Nov|Dec)\.?\s+(\d{1,2}),?\s+(20\d{2})\b",
        text,
        re.I,
    )
    if not match:
        return None
    month = _MONTH_NUM.get(match.group(1).lower()[:3])
    if not month:
        return None
    try:
        return datetime(int(match.group(3)), month, int(match.group(2))).date().isoformat()
    except ValueError:
        return None


def _first_date_in(text: str) -> Optional[str]:
    if not text:
        return None
    match = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
    if match:
        return _day(match.group(1))
    return _day(text)


def _commit_is_substantive(title: str) -> bool:
    """A commit that is more than a README/license touch or the empty initial commit."""
    text = re.sub(r"\s+", " ", (title or "").strip())
    if not text:
        return False
    if re.match(r"(?i)^initial commit$", text):
        return False
    if re.match(
        r"(?i)^(?:(?:update|edit|fix|tweak|add|docs?)\s+)*"
        r"(?:readme|license|citation|changelog)(?:\.md)?$",
        text,
    ):
        return False
    return True


def _commit_clusters(commits, gap_days: int = _COMMIT_GAP_DAYS):
    parsed = []
    for row in commits or []:
        if not isinstance(row, dict):
            continue
        day = _day(row.get("date"))
        if not day:
            continue
        parsed.append((day, row.get("title") or ""))
    parsed.sort()
    if not parsed:
        return []
    clusters = [[parsed[0]]]
    for prev, cur in zip(parsed, parsed[1:]):
        gap = (
            datetime.strptime(cur[0], "%Y-%m-%d").date()
            - datetime.strptime(prev[0], "%Y-%m-%d").date()
        ).days
        if gap > gap_days:
            clusters.append([cur])
        else:
            clusters[-1].append(cur)
    return clusters


def _signal_from_commits(commits):
    """(YYYY-MM-DD, signal) for the latest meaningful commit cluster.

    signal is first_public_release when the newest cluster contains a
    substantive commit (weights, release notes, code), last_substantive_commit
    when the newest cluster is README-only and an older cluster is real, or
    ('', 'readme_only') when nothing substantive was ever committed.
    """
    clusters = _commit_clusters(commits)
    if not clusters:
        return None, ""

    def _substantive(cluster) -> bool:
        return any(_commit_is_substantive(title) for _day, title in cluster)

    latest = clusters[-1]
    if _substantive(latest):
        return latest[0][0], "first_public_release"
    for cluster in reversed(clusters[:-1]):
        if _substantive(cluster):
            return cluster[0][0], "last_substantive_commit"
    return None, "readme_only"


def hf_model_release_date(payload, *, today: str = None, trending: bool = False,
                          commits=None):
    """Pick the release day for one HF repo.

    Returns (YYYY-MM-DD or None, signal, repo_created_at).

    Repo createdAt is the weakest clock: labs open the repo before the public
    launch (Kumo Tabular created Sep 1, released Sep 28; AstaBrief created
    Feb 9, weights Oct 2). Preference:

    1. Start of the latest substantive commit cluster (first public release /
       first weights commit), when commits are provided.
    2. lastModified, when the repo actually changed.
    3. The day we first observed it on HF trending, when no other clock exists.
    4. createdAt, only when nothing more relevant is present.

    A README-only follow-up does not count as a release: the date stays the
    repo birth (or the previous substantive cluster), so the stale filter
    still drops back-catalog doc bumps.
    """
    payload = payload or {}
    created = _day(payload.get("createdAt"))
    modified = _day(payload.get("lastModified"))
    if commits:
        commit_day, commit_signal = _signal_from_commits(commits)
        if commit_day and commit_signal in (
            "first_public_release", "last_substantive_commit",
        ):
            return commit_day, commit_signal, created
        if commit_signal == "readme_only":
            return created, "createdAt", created
    if modified:
        return modified, "lastModified", created
    if trending:
        day = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return day, "trending", created
    if created:
        return created, "createdAt", created
    return None, "unknown", created


def log_dropped_candidate(kind: str, name: str, reason: str) -> None:
    """One stderr line per candidate that did not make the digest.

    kind is 'filter' (stale, noise, no card, serving sku) or 'writer_exclusion'.
    """
    if kind not in ("filter", "writer_exclusion"):
        kind = "filter"
    name = " ".join(str(name or "unknown").split())
    reason = " ".join(str(reason or "").split())
    print(
        f"dropped candidate kind={kind} name={name} reason={reason}",
        file=sys.stderr,
    )


def stale_drop_reason(model, today: str = None, max_age_days: int = 14) -> Optional[str]:
    """Why this first-seen model is too old to publish, or None to keep it.

    A fresh lastModified / release commit / trending date keeps the model even
    when repo createdAt is outside the window. createdAt alone is a drop only
    when no stronger clock exists.
    """
    if model is None:
        return None
    release = getattr(model, "release_date", None)
    if not is_stale_release(release, today=today, max_age_days=max_age_days):
        return None
    signal = getattr(model, "recency_signal", None) or "createdAt"
    created = getattr(model, "repo_created_at", None) or ""
    date = str(release)[:10]
    if signal == "createdAt":
        return (
            f"stale signal=createdAt date={date} repo_created={created or date}; "
            f"no lastModified, first public release, weights commit, or trending "
            f"date inside {max_age_days}d"
        )
    return f"stale signal={signal} date={date} repo_created={created or 'unknown'}"


def drop_stale_models(models, seen, today: str = None):
    """Remove back-catalog rows. Log each reason. Mark dropped ids seen."""
    kept, dropped = [], []
    for model in models or []:
        reason = stale_drop_reason(model, today=today)
        if not reason:
            kept.append(model)
            continue
        log_dropped_candidate("filter", getattr(model, "name", None), reason)
        dropped.append(model)
        if seen is not None and getattr(model, "name", None):
            seen.add(model.name)
    if dropped:
        print(
            f"Dropping {len(dropped)} stale back-catalog model(s): "
            + ", ".join(m.name for m in dropped[:5])
            + ("…" if len(dropped) > 5 else ""),
            file=sys.stderr,
        )
    return kept


def _fetch_hf_commits(model_id: str):
    """Commit list for one HF model, or [] on any failure. Never raises."""
    if not model_id or "/" not in model_id:
        return []
    try:
        resp = _http_get(
            f"https://huggingface.co/api/models/{model_id}/commits/main",
            "HF commits",
            timeout=15,
        )
        rows = resp.json()
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if isinstance(row, dict) and row.get("date"):
            out.append({"date": row.get("date"), "title": row.get("title") or ""})
    return out


def refine_stale_repo_dates(models, today: str = None, budget: int = None):
    """For repos born before the window but modified inside it, prefer the
    commit cluster (first public release / first weights) over lastModified.

    A README-only bump falls back to createdAt so the stale filter can drop it.
    Bounded so a busy org list cannot turn into hundreds of commit requests.
    """
    if budget is None:
        budget = HF_RECENCY_COMMIT_BUDGET
    suspects = []
    for model in models or []:
        if not (getattr(model, "source", "") or "").startswith("huggingface"):
            continue
        if getattr(model, "recency_signal", None) != "lastModified":
            continue
        created = getattr(model, "repo_created_at", None)
        if not created or not is_stale_release(created, today=today):
            continue
        if is_stale_release(getattr(model, "release_date", None), today=today):
            continue
        suspects.append(model)
    suspects.sort(
        key=lambda m: ((m.likes or 0), (m.downloads or 0)),
        reverse=True,
    )
    for model in suspects[: max(0, budget)]:
        commits = _fetch_hf_commits(model.name)
        if not commits:
            continue
        date, signal, created = hf_model_release_date(
            {"createdAt": model.repo_created_at, "lastModified": model.release_date},
            today=today,
            commits=commits,
        )
        if date == model.release_date and signal == model.recency_signal:
            continue
        model.release_date = date
        model.recency_signal = signal
        if created:
            model.repo_created_at = created
    return list(models or [])


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def candidate_in_digest(model, summary: str) -> bool:
    """True when the published body still mentions this candidate."""
    summary = summary or ""
    if not summary.strip() or summary.strip() == NO_MODELS_SENTINEL:
        return False
    for url in (
        getattr(model, "url", None),
        getattr(model, "canonical_url", None),
    ):
        if url and url in summary:
            return True
    name = getattr(model, "name", "") or ""
    if name and name in summary:
        return True
    key = _alnum(name.split("/")[-1] if name else "")
    if len(key) >= 6 and key in _alnum(summary):
        return True
    return False


def log_writer_exclusions(models, summary: str) -> List[str]:
    """Log candidates handed to the writer that are absent from the digest."""
    omitted = []
    for model in models or []:
        if model is None or not getattr(model, "name", None):
            continue
        if candidate_in_digest(model, summary):
            continue
        log_dropped_candidate("writer_exclusion", model.name, "omitted by writer")
        omitted.append(model.name)
    return omitted


def is_significant_release(model_id: str, author: str, tags: list,
                           downloads: int = 0) -> bool:
    """Check if this is a significant release worth reporting."""
    model_lower = model_id.lower()

    significant_families = [
        "llama-", "llama2-", "llama3", "llama4", "llama-4",
        "mistral", "mixtral", "devstral", "leanstral", "voxtral",
        "qwen2", "qwen3", "qwen3.5", "qwen3.6",
        "gemma-", "gemma4", "gemma-4",
        "phi-", "phi4", "falcon-", "yi-", "deepseek",
        "command-r", "command-a", "codestral",
        "nvidia/llama", "nemotron", "granite",
        "olmo", "pythia", "glm-", "glm5", "glm-5", "glm-4.7",
        "grok", "grok-4",
        "claude", "gpt-4", "gpt-4o", "gpt-5", "gpt-5.5", "o1-", "o3-",
        "gemini-", "gemini2", "gemini3", "gemini-3",
        "arcee", "minimax", "voxcpm", "kimi",
        "deepseek-v3", "deepseek-v4", "deepseek-ocr",
        "minicpm", "supertonic", "supertone",
        "sulphur", "hidream", "zamba", "zaya",
        "ring-",
        "a.x-k",
        "internlm", "intern-s",
        "hunyuan", "fugu",
        "north-mini", "higgs-audio",
        "exaone", "k-exaone",
        "anima", "reka-edge", "lyria-",
        "openai/o1", "openai/o3", "anthropic/claude",
        "wan2", "wan3", "wan-3", "dramabox", "pixal3d", "agent",
        "longcat", "laguna-", "gigachat",
        "inkling", "sarvam", "muse-",
        "mellum",
    ]
    if any(f in model_lower for f in significant_families):
        return True

    # KNOWN_ORGS is the significance list. A parallel significant_orgs subset
    # lagged every supervisor org add (issue #15): Cohere / poolside /
    # sapientinc / inclusionAI day-one releases needed 100k downloads to rank.
    author_key = (author or "").lower().split("/")[0].lstrip("~")
    if author_key in KNOWN_ORGS:
        return True

    if downloads and downloads >= 100000:
        return True

    return False


# OpenRouter lists each model as a base id plus serving SKUs (`:batch` for
# async batch inference, `:free` for the $0 routed copy). Those are not
# releases — collapsing them onto the base id stops a leftover SKU from
# becoming the day's only digest item (2026-08-29: glm-5.3-flash:batch).
_OPENROUTER_SERVING_SUFFIXES = (":batch", ":free")


def _openrouter_base_id(model_id: str) -> str:
    """Strip trailing OpenRouter serving suffixes (`:batch`, `:free`)."""
    name = (model_id or "").strip()
    while True:
        lower = name.lower()
        stripped = False
        for suffix in _OPENROUTER_SERVING_SUFFIXES:
            if lower.endswith(suffix):
                name = name[: -len(suffix)]
                stripped = True
                break
        if not stripped:
            return name


def is_openrouter_serving_sku(model_id: str) -> bool:
    """True when `model_id` is an OpenRouter `:batch` / `:free` serving copy."""
    raw = (model_id or "").strip()
    return bool(raw) and _openrouter_base_id(raw) != raw


def fetch_openrouter_models() -> List[ModelRelease]:
    models = []
    try:
        resp = _http_get("https://openrouter.ai/api/v1/models", "OpenRouter", timeout=30)
        for m in resp.json().get("data", []):
            model_id = m.get("id", "")
            if not model_id:
                continue
            if is_openrouter_serving_sku(model_id):
                continue
            pricing = m.get("pricing", {})
            try:
                ip = float(pricing.get("prompt", 0)) * 1_000_000
                op = float(pricing.get("completion", 0)) * 1_000_000
            except (ValueError, TypeError):
                ip = op = None
            ctx = m.get("context_length")

            is_open = False
            open_kws = ["llama", "mistral", "qwen", "gemma", "mixtral",
                        "phi", "falcon", "yi", "deepseek", "nemotron", "olm", "c4ai",
                        "sulphur", "zamba", "arcee", "minicpm",
                        "devstral", "leanstral", "voxtral", "granite"]
            closed = ["openai", "anthropic", "google", "cohere", "ai21"]
            prov = (m.get("owned_by") or "").lower()
            if any(kw in model_id.lower() for kw in open_kws):
                is_open = True
            elif prov in closed:
                is_open = False
            elif "open" in prov or "open" in model_id.lower():
                is_open = True

            traits = []
            if ctx and ctx >= 128_000:
                traits.append("long_context")
            if "vision" in model_id.lower() or "vl" in model_id.lower():
                traits.append("multimodal")
            if any(x in model_id.lower() for x in ["reasoning", "r1", "o3", "o1"]):
                traits.append("reasoning")
            if any(x in model_id.lower() for x in ["code", "coder", "claude", "gpt-4"]):
                traits.append("coding")
            if ip is not None and ip < 0.5:
                traits.append("cheap")
            if "moe" in model_id.lower() or "mixtral" in model_id.lower():
                traits.append("MoE")

            created = m.get('created', 0)
            if created:
                rd = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d")
            else:
                rd = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            desc = _smart_truncate(m.get("description", ""), 200)
            models.append(ModelRelease(
                name=model_id,
                provider=_resolve_provider(m.get("owned_by", ""), model_id),
                source="openrouter",
                url=f"https://openrouter.ai/models/{model_id}",
                description=desc,
                context_window=ctx,
                pricing_input=ip,
                pricing_output=op,
                release_date=rd,
                is_open_source=is_open,
                unique_traits=traits,
            ))
    except Exception as e:
        reason = _remember_source_error("OpenRouter", e)
        print(f"OpenRouter error: {reason}", file=sys.stderr)
    return models


def fetch_ollama_models() -> List[ModelRelease]:
    """Ollama library — currently low signal, fetch lightly."""
    models = []
    try:
        resp = _http_get("https://ollama.com/library", "Ollama", timeout=30)
        seen = set()
        for m in re.findall(r'href="/library/([^"]+)"', resp.text):
            if m in seen or m.startswith("."):
                continue
            seen.add(m)
            models.append(ModelRelease(
                name=m, provider="Ollama", source="ollama",
                url=f"https://ollama.com/library/{m}",
                description="Local LLM available via Ollama",
                is_open_source=True,
                unique_traits=["local", "open_source"]
            ))
    except Exception as e:
        reason = _remember_source_error("Ollama", e)
        print(f"Ollama error: {reason}", file=sys.stderr)
    return models


# Major AI orgs to monitor directly on HuggingFace
MAJOR_HF_ORGS = [
    "deepseek-ai", "meta-llama", "mistralai", "Qwen", "google",
    "anthropic", "openai", "nvidia", "microsoft", "x-ai",
    "z-ai", "zai-org", "arcee-ai", "openbmb", "MiniMaxAI",
    "NousResearch", "tiiuae", "01-ai", "BAAI", "xiaomi",
    "moonshotai", "ByteDance-Seed", "bytedance-research", "inclusionAI", "ibm",
    "allenai", "amazon", "perplexity-ai", "stabilityai",
    "HiDream-ai", "SulphurAI", "Zyphra",
    "circlestone-labs", "Hcompany", "Supertone",
    "TencentARC", "tencent", "ResembleAI", "ADSKAILab", "open-thoughts",
    "CohereLabs", "LGAI-EXAONE",
    "bosonai", "sapientinc", "SakanaAI",
    "internlm", "meituan-longcat", "poolside", "ai-sage",
    "thinkingmachines", "black-forest-labs",
    "skt", "Wan-AI", "sarvamai", "meta-models",
    "JetBrains",
]


ENRICH_HF_CARDS = os.environ.get("MODELBYTES_ENRICH_HF_CARDS", "1") == "1"
# Parallel.ai web discovery — lets the inline path find genuinely-new releases
# the static fetchers miss (the dedup table drains to 0-new after a few days).
# Web research + cited sources, fed to the writer model. No Claude.
PARALLEL_API_KEY = os.environ.get("MODELBYTES_PARALLEL_API_KEY", "")
DISCOVERY_ENABLED = os.environ.get(
    "MODELBYTES_DISCOVERY", "1" if PARALLEL_API_KEY else "0") == "1"
PARALLEL_SEARCH_URL = "https://api.parallel.ai/v1/search"
# When the claude.ai curator is retired, the inline (deepseek/Ollama) path IS
# the everyday digest, not a degraded fallback — so don't alert "published via
# fallback / curator absent" every day. Real failures (QA block, send fail,
# verification-stripped no-post, crash) still alert. Quiet catalog-empty
# no-models days are recorded, not paged (2026-08-24).
# Default ON: the claude.ai curator is retired (supervisor paused 2026-08-22).
# Unset / missing must not page "curator absent" every day. Set to 0 to opt out.
INLINE_PRIMARY = os.environ.get("MODELBYTES_INLINE_PRIMARY", "1") == "1"


def _param_size_from_name(name: str) -> Optional[str]:
    """The size token a model advertises in its own name ('…-32B', '…-A4B',
    '…-70m'). The canonical headline size — more trustworthy than HF
    safetensors.total, which is often a partial/sharded/adapter upload. Returns
    the LARGEST token (total, not the MoE active count) or None.

    Case-insensitive on the unit (real HF IDs are almost always lowercase,
    e.g. 'tmax-27b', 'qwen35-9b'; 2026-06-22 incident: a case-sensitive match
    missed those, marked params 'unknown', and the LLM hallucinated specs).
    The unit must be at a boundary (hyphen, start, or end) so a 'b'/'m' inside
    a word ('lab', 'web', 'something') can't false-match."""
    seg = name.split("/")[-1]
    best = None
    # Match <digits>[.<digits>] (b|m) where the unit is at a token boundary:
    # preceded by start-of-string or a non-letter (hyphen/space), and followed
    # by end-of-string or a non-letter. Case-insensitive.
    for num, unit in re.findall(r"(?:(?<=[-\s_])|(?<=^))(\d+(?:\.\d+)?)\s*([bBmM])(?![a-zA-Z])", seg):
        val = float(num) * (1e9 if unit.upper() == "B" else 1e6)
        if best is None or val > best[0]:
            best = (val, f"{num}{unit.upper()}")
    return best[1] if best else None


def _format_param_count(n) -> Optional[str]:
    """Raw HF safetensors total → '12B' / '760M'. None for 0/missing/unparseable."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n >= 1_000_000_000:
        v = n / 1_000_000_000
        return (f"{v:.1f}".rstrip("0").rstrip(".")) + "B"
    if n >= 1_000_000:
        return f"{round(n / 1_000_000)}M"
    return f"{n}"


def _extract_card_benchmarks(card_data: dict) -> str:
    """Pull a short benchmark string from a model card's model-index results.
    Defensive against the format's many shapes; returns '' when nothing usable."""
    out = []
    try:
        for entry in card_data.get("model-index", []) or []:
            for result in entry.get("results", []) or []:
                name = (result.get("dataset", {}) or {}).get("name")
                metrics = result.get("metrics", []) or []
                val = next((m.get("value") for m in metrics
                            if isinstance(m.get("value"), (int, float))), None)
                if name and val is not None:
                    out.append(f"{name} {val}")
                if len(out) >= 5:
                    break
            if len(out) >= 5:
                break
    except Exception:
        return ""
    return ", ".join(out)


def fetch_hf_card(model_id: str) -> dict:
    """Fetch a HuggingFace model's card metadata (license, total params, context,
    benchmarks) so the inline LLM has real facts to write from. Returns only the
    keys actually found; {} on any failure — never raises (a missing card just
    yields a thinner entry, never a crash)."""
    card = {}
    try:
        resp = _http_get(f"https://huggingface.co/api/models/{model_id}",
                         f"HF card {model_id}", timeout=20)
        data = resp.json()
        card_data = data.get("cardData", {}) or {}
        lic = card_data.get("license") or card_data.get("license_name")
        # "other"/"unknown" are HF placeholders, not real licenses — don't
        # publish "other license".
        if isinstance(lic, str) and lic.lower() not in ("other", "unknown", "none", ""):
            card["license"] = lic
        # Trust the model's own name over safetensors.total (often partial — a
        # 32B model whose repo holds only a 676K adapter must not become "676K").
        params = _param_size_from_name(model_id) or _format_param_count(
            (data.get("safetensors", {}) or {}).get("total"))
        if params:
            card["total_parameters"] = params
        ctx = (data.get("config", {}) or {}).get("max_position_embeddings")
        if isinstance(ctx, int) and ctx > 0:
            card["context_window"] = ctx
        bench = _extract_card_benchmarks(card_data)
        if bench:
            card["benchmarks"] = bench
    except Exception as e:
        print(_redact_secrets(f"HF card {model_id} fetch failed: {e}"), file=sys.stderr)
    return card


def enrich_with_hf_cards(models: List[ModelRelease]) -> None:
    """Fill missing hard-facts on HF-sourced models from their cards, in place.
    Bounded by the caller (pass the digest set, ≤15). Never overwrites a value
    we already have; never raises."""
    for m in models:
        if not (m.source or "").startswith("huggingface") or "/" not in m.name:
            continue
        card = fetch_hf_card(m.name)
        if not card:
            continue
        m.license = m.license or card.get("license")
        m.total_parameters = m.total_parameters or card.get("total_parameters")
        m.context_window = m.context_window or card.get("context_window")
        if card.get("benchmarks") and not m.card_facts:
            m.card_facts = card["benchmarks"]


def fetch_org_models(author: str) -> List[ModelRelease]:
    """Fetch models from a specific HF org, sorted by recent."""
    models = []
    try:
        resp = _http_get(
            f"https://huggingface.co/api/models?author={author}&sort=lastModified&direction=-1&limit=10",
            f"HF org {author}",
            timeout=20)
        for m in resp.json():
            model_id = m.get("id", "")
            if not model_id:
                continue
            tags = m.get("tags", [])
            pipeline = m.get("pipeline_tag", "")
            downloads = m.get("downloads", 0) or 0
            likes = m.get("likes", 0) or 0

            if is_noise_model(model_id, author, tags, downloads, likes):
                continue

            rd, recency_signal, repo_created = hf_model_release_date(m)
            desc = _smart_truncate(
                f"{pipeline} model" if pipeline else "ML model", 200)

            models.append(ModelRelease(
                name=model_id,
                provider=_resolve_provider(author),
                source="huggingface-org",
                url=f"https://huggingface.co/{model_id}",
                description=desc,
                release_date=rd,
                recency_signal=recency_signal,
                repo_created_at=repo_created,
                architecture=tags[0] if tags else None,
                is_open_source=True,
                unique_traits=["hf_hub"] + tags[:3],
                downloads=downloads,
                likes=likes,
            ))
    except Exception as e:
        reason = _remember_source_error("HuggingFace-Orgs", e)
        print(f"HF org {author} error: {reason}", file=sys.stderr)
    return models


def fetch_major_orgs() -> List[ModelRelease]:
    """Poll major AI org HF repos for new releases."""
    models = []
    for org in MAJOR_HF_ORGS:
        org_models = fetch_org_models(org)
        models.extend(org_models)
    return models


def fetch_hf_text_generation() -> List[ModelRelease]:
    """Fetch top HF text-generation models by downloads/likes."""
    models = []
    try:
        resp = _http_get(
            "https://huggingface.co/api/models"
            "?pipeline_tag=text-generation&sort=downloads&direction=-1&limit=30",
            "HF top text-generation",
            timeout=20)
        for m in resp.json():
            model_id = m.get("id", "")
            author = m.get("author", "")
            tags = m.get("tags", [])
            downloads = m.get("downloads", 0) or 0
            likes = m.get("likes", 0) or 0

            if is_noise_model(model_id, author, tags, downloads, likes):
                continue

            rd, recency_signal, repo_created = hf_model_release_date(m)
            pipeline = m.get("pipeline_tag", "")
            desc = _smart_truncate(
                f"{pipeline} model" if pipeline else "LLM", 200)

            models.append(ModelRelease(
                name=model_id,
                provider=_resolve_provider(author),
                source="huggingface-top",
                url=f"https://huggingface.co/{model_id}",
                description=desc,
                release_date=rd,
                recency_signal=recency_signal,
                repo_created_at=repo_created,
                architecture=tags[0] if tags else None,
                is_open_source=True,
                unique_traits=["hf_hub"] + tags[:3],
                downloads=downloads,
                likes=likes,
            ))
    except Exception as e:
        reason = _remember_source_error("HuggingFace-Top-TextGen", e)
        print(f"HF top text-gen error: {reason}", file=sys.stderr)
    return models


def fetch_huggingface_trending() -> List[ModelRelease]:
    """Fetch from HF trending + recently modified. Apply strict filtering."""
    models = []
    try:
        # Try trending first
        resp = _http_get("https://huggingface.co/api/trending", "HF trending", timeout=30)
        for item in resp.json().get("recentlyTrending", []):
            if item.get("repoType") != "model":
                continue
            m = item.get("repoData", {})
            model_id = m.get("id", "")
            if not model_id:
                continue
            author = m.get("author", "")
            tags = m.get("tags", [])
            pipeline = m.get("pipeline_tag", "")
            downloads = m.get("downloads", 0) or 0
            likes = m.get("likes", 0) or 0

            if is_noise_model(model_id, author, tags, downloads, likes):
                continue
            # Trending page already vouches for relevance — lower the bar
            # If it survived noise filter, just needs modest engagement
            if not (is_significant_release(model_id, author, tags, downloads)
                    or downloads >= 5000 or likes >= 100
                    or downloads >= 1000 and likes >= 30):
                continue

            rd, recency_signal, repo_created = hf_model_release_date(m)
            desc = _smart_truncate(
                f"{pipeline} model" if pipeline else m.get("cardData", {}).get("model_summary", "ML model"),
                200)

            models.append(ModelRelease(
                name=model_id,
                provider=_resolve_provider(author),
                source="huggingface",
                url=f"https://huggingface.co/{model_id}",
                description=desc,
                release_date=rd,
                recency_signal=recency_signal,
                repo_created_at=repo_created,
                architecture=tags[0] if tags else None,
                is_open_source=True,
                unique_traits=["hf_hub"] + tags[:3],
                downloads=downloads,
                likes=likes,
            ))

        # Also fetch recently modified for completeness
        resp2 = _http_get(
            "https://huggingface.co/api/models",
            "HF recent models",
            params={"sort": "lastModified", "direction": -1, "limit": 50},
            timeout=30)
        for m in resp2.json():
            model_id = m.get("id", "")
            if not model_id:
                continue
            author = m.get("author", "")
            tags = m.get("tags", [])
            downloads = m.get("downloads", 0) or 0
            likes = m.get("likes", 0) or 0

            if is_noise_model(model_id, author, tags, downloads, likes):
                continue
            # Trending/recent models: lower bar than general discovery.
            # If it survived noise filter, just needs modest engagement or significance.
            if not (is_significant_release(model_id, author, tags, downloads)
                    or downloads >= 5000 or likes >= 100
                    or downloads >= 1000 and likes >= 30):
                continue

            rd, recency_signal, repo_created = hf_model_release_date(m)
            pipeline = m.get("pipeline_tag", "")
            desc = _smart_truncate(
                f"{pipeline} model" if pipeline else "ML model", 200)

            models.append(ModelRelease(
                name=model_id,
                provider=_resolve_provider(author),
                source="huggingface",
                url=f"https://huggingface.co/{model_id}",
                description=desc,
                release_date=rd,
                recency_signal=recency_signal,
                repo_created_at=repo_created,
                architecture=tags[0] if tags else None,
                is_open_source=True,
                unique_traits=["hf_hub"] + tags[:3],
                downloads=downloads,
                likes=likes,
            ))
    except Exception as e:
        reason = _remember_source_error("HuggingFace-Trending", e)
        print(f"HF error: {reason}", file=sys.stderr)
    try:
        extra = fetch_hf_new_models()
    except Exception as e:
        print(f"HF new pass error: {_short_source_error(e)}", file=sys.stderr)
        extra = []
    seen_ids = {m.name for m in models}
    for model in extra:
        if model.name not in seen_ids:
            models.append(model)
            seen_ids.add(model.name)
    return models


def _has_model_card(payload) -> bool:
    card = (payload or {}).get("cardData")
    return isinstance(card, dict) and bool(card)


def _hf_new_prefilter(row) -> Optional[str]:
    """None queues a card lookup. '' skips quietly. Any other string is logged."""
    model_id = row.get("id") or ""
    author = row.get("author") or (model_id.split("/")[0] if "/" in model_id else "")
    tags = list(row.get("tags") or [])
    if row.get("pipeline_tag"):
        tags.append(row.get("pipeline_tag"))
    try:
        likes = int(row.get("likes") or 0)
    except (TypeError, ValueError):
        likes = 0
    try:
        downloads = int(row.get("downloads") or 0)
    except (TypeError, ValueError):
        downloads = 0
    if likes < HF_NEW_MIN_LIKES and downloads < HF_NEW_MIN_DOWNLOADS:
        return ""
    if is_noise_model(
        model_id, author, tags, downloads, likes, engagement_floor=False,
    ):
        pipe = row.get("pipeline_tag") or "-"
        return f"noise filter (pipeline={pipe})"
    return None


def fetch_hf_new_models(today: str = None) -> List[ModelRelease]:
    """Models new on Hugging Face, whatever the org.

    trendingScore plus the trending endpoint, then a likes/downloads floor and
    a real model card. Not limited to KNOWN_ORGS — that list is why
    NaiveAI/Naive-N0.5-Flash never became a candidate. Repo createdAt is not
    the launch date; lastModified / the commit cluster is.
    """
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = []
    try:
        resp = _http_get(
            "https://huggingface.co/api/models"
            f"?sort=trendingScore&direction=-1&limit={HF_NEW_TRENDING_LIMIT}",
            "HF new",
            timeout=30,
        )
        payload = resp.json()
        if isinstance(payload, list):
            rows.extend(row for row in payload if isinstance(row, dict))
    except Exception as e:
        print(f"HF new trendingScore error: {_short_source_error(e)}", file=sys.stderr)
    try:
        resp = _http_get(
            "https://huggingface.co/api/trending", "HF new trending", timeout=30)
        body = resp.json() or {}
        for item in body.get("recentlyTrending", []) or []:
            if not isinstance(item, dict) or item.get("repoType") != "model":
                continue
            repo = dict(item.get("repoData") or {})
            if repo.get("id"):
                repo["_on_trending"] = True
                rows.append(repo)
    except Exception as e:
        print(f"HF new trending error: {_short_source_error(e)}", file=sys.stderr)

    deduped, seen = [], set()
    for row in rows:
        model_id = row.get("id") or ""
        if not model_id or model_id in seen or row.get("private"):
            continue
        seen.add(model_id)
        deduped.append(row)

    queued = []
    for row in deduped:
        reason = _hf_new_prefilter(row)
        if reason is None:
            queued.append(row)
        elif reason:
            log_dropped_candidate("filter", row.get("id") or "unknown", reason)

    def _rank(row):
        created = _day(row.get("createdAt"))
        fresh = 0
        if created and not is_stale_release(created, today=today, max_age_days=21):
            fresh = 1
        try:
            likes = int(row.get("likes") or 0)
        except (TypeError, ValueError):
            likes = 0
        return (fresh, likes)

    queued.sort(key=_rank, reverse=True)
    models = []
    for row in queued[:HF_NEW_DETAIL_CAP]:
        model_id = row["id"]
        detail = dict(row)
        if not _has_model_card(detail) or not _day(detail.get("lastModified")):
            try:
                resp = _http_get(
                    f"https://huggingface.co/api/models/{model_id}",
                    f"HF new card {model_id}",
                    timeout=20,
                )
                fetched = resp.json()
                if isinstance(fetched, dict):
                    detail.update(fetched)
            except Exception as e:
                log_dropped_candidate(
                    "filter", model_id,
                    f"model card lookup failed: {_short_source_error(e)}",
                )
                continue
        if not _has_model_card(detail):
            log_dropped_candidate("filter", model_id, "no model card")
            continue
        author = detail.get("author") or model_id.split("/")[0]
        rd, signal, created = hf_model_release_date(
            detail, today=today, trending=bool(row.get("_on_trending")))
        tags = list(detail.get("tags") or [])
        pipeline = detail.get("pipeline_tag") or ""
        try:
            downloads = int(detail.get("downloads") or 0)
        except (TypeError, ValueError):
            downloads = 0
        try:
            likes = int(detail.get("likes") or 0)
        except (TypeError, ValueError):
            likes = 0
        model = ModelRelease(
            name=model_id,
            provider=_resolve_provider(author),
            source="huggingface-new",
            url=f"https://huggingface.co/{model_id}",
            description=_smart_truncate(
                f"{pipeline} model" if pipeline else "ML model", 200),
            release_date=rd,
            recency_signal=signal,
            repo_created_at=created,
            architecture=tags[0] if tags else None,
            is_open_source=True,
            unique_traits=["hf_hub", "hf_new"] + tags[:3],
            downloads=downloads,
            likes=likes,
        )
        reason = stale_drop_reason(model, today=today)
        if reason:
            log_dropped_candidate("filter", model_id, reason)
            continue
        models.append(model)
    return models


def categorize_model(model: ModelRelease) -> str:
    name = model.name.lower()
    provider = (model.provider or "").lower()
    traits = [t.lower() for t in (model.unique_traits or [])]

    premier = ["llama-3.3", "llama-3.2", "mistral-large", "mixtral",
               "qwen2.5-72b", "qwen3", "qwen3.6", "deepseek-v3", "deepseek-v4",
               "gemma-2-27b", "gemma-4", "gemma-3", "diffusiongemma", "command-r-plus", "command-a", "nemotron",
               "sulphur", "minicpm", "zaya", "glm-5", "glm-4.7",
               "minimax", "grok-2", "grok-3",
               "inkling", "a.x-k"]
    # NOTE: 'kimi' must NOT be in this list — Moonshot's Kimi K2 models are
    # open-weight (issue #16); moonshotai routes via sig_org_map below.
    closed = ["gpt-4", "claude-3", "claude-4", "claude-opus-4", "o1-", "o3-", "gemini-1.5", "gemini-2", "gemini-3", "grok-4"]
    reasoning = ["reasoning", "r1", "o1", "o3"]
    coding = ["codestral", "coder", "code-", "claude-3.5", "devstral", "grok-build"]
    image_gen = ["dall-e", "flux", "stable-diffusion", "midjourney", "wan2", "pixal", "grok-imagine"]
    audio = ["lyria", "supertone", "supertonic", "dramabox", "higgs-audio"]

    if any(p in name for p in premier) or provider in ["meta", "mistral ai", "alibaba"]:
        if "closed" not in traits and model.is_open_source is not False:
            return "open_frontier"
    if any(c in name for c in closed) or provider in ["openai", "anthropic", "google"]:
        return "closed_frontier"
    # Domain keywords (reasoning / coding / image / audio) all land in the
    # single SPECIALIZED tier under format v3
    if any(r in name for r in reasoning):
        return "specialized"
    if any(c in name for c in coding):
        return "specialized"
    if any(i in name for i in image_gen):
        return "specialized"
    if any(a in name for a in audio):
        return "specialized"
    # Known significant orgs always get meaningful categorization.
    # Key by the HF/OpenRouter slug from model.name — production sets
    # provider to the PROVIDER_NAMES display string ("Tencent ARC"), so a
    # slug-keyed lookup on provider.lower() never fired (issue #22).
    name_org = ""
    if "/" in (model.name or ""):
        name_org = model.name.split("/", 1)[0].lower().lstrip("~")
    sig_org_map = {"tencentarc": "specialized", "resembleai": "specialized",
                   "adskailab": "other", "open-thoughts": "specialized",
                   "deepseek-ai": "open_frontier", "inclusionai": "open_frontier",
                   "moonshotai": "open_frontier"}
    if name_org in sig_org_map:
        return sig_org_map[name_org]
    if model.source == "ollama":
        return "local"
    # Give high-engagement unknown orgs a shot at being shown
    if getattr(model, "likes", 0) >= 500 or getattr(model, "downloads", 0) >= 50000:
        return "other"
    return "other"


def _availability_tag(m: ModelRelease) -> str:
    """Format v3 per-entry action tag: how a builder can use this model today,
    derived deterministically from where we observed it."""
    if m.source == "openrouter":
        return "⚡ API live · OpenRouter"
    if m.source == "ollama":
        return "📦 Ollama pull-ready"
    if m.source == "discovery":
        return "🔗 Cited source"
    return "📦 Open weights · HF"


# The bare "nothing to publish" body. Both the template and the LLM fallback
# return this when there is no publishable content. main() checks for it so the
# sentinel is treated as a no-post day and never dumped on the channel
# (2026-07-04 backfill incident).
NO_MODELS_SENTINEL = "No new models today."


_MON_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_HOST_LINK_LABELS = (
    ("huggingface.co", "HF"),
    ("hf.co", "HF"),
    ("openrouter.ai", "OpenRouter"),
    ("ollama.com", "Ollama"),
)
# Sentence-initial and ordinary words. A capitalized token outside this set
# must match a listed model, its provider, or its license — otherwise the
# Take is naming something that is not in the digest (Astra on Sep 30).
_TAKE_STOPWORDS = frozenset({
    "today", "todays", "open", "closed", "builder", "builders", "model", "models",
    "release", "releases", "weight", "weights", "week", "weeks", "day", "days",
    "frontier", "local", "pattern", "multimodal", "source", "sources", "nothing",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "apache", "free", "with",
    "from", "that", "this", "these", "those", "small", "large", "while", "after",
    "before", "about", "their", "there", "where", "which", "what", "when",
    "your", "into", "over", "under", "between", "across", "alongside", "managed",
    "cheaper", "options", "vision", "fallback", "same", "point", "upgrades",
    "tooling", "added", "math", "verifier", "eval", "checker", "window",
    "incremental", "shelved", "signals", "safety", "gates", "tightening",
    "flagship", "rollouts", "smaller", "utility", "concerns", "pulling",
    "major", "launch", "licensed", "opens", "without", "specialised",
    "specialized", "sovereign", "german", "english", "french", "chinese",
    "japanese", "korean", "based", "diarization", "speaker", "whole", "only",
    "else", "narrow", "practical", "move", "wiring", "cheap", "agent", "loop",
    "genuinely", "stood", "really", "still", "just", "more", "most", "some",
    "than", "then", "them", "they", "have", "been", "being", "were", "will",
    "would", "could", "should", "other", "another", "every", "each", "both",
    "high", "capacity", "labs", "shipped", "landed", "landing", "means",
    "builders", "pattern", "todays",
})
_PARAM_FRAGMENT = re.compile(
    r"(?i)^(?:about|around|~)?\s*\d+(?:\.\d+)?\s*[bmkt]"
    r"\s*(?:total|active)?\s*(?:params|parameters)?$")
_RELEASED_FRAGMENT = re.compile(
    r"(?i)^released:?\s+(?:\d{4}-\d{2}-\d{2}|[a-z]+\.?\s+\d{1,2})$")
_LICENSE_FRAGMENT = re.compile(
    r"(?i)^(?:license:?\s*)?(?:"
    r"apache[-\s]?2(?:\.0)?|mit|cc[-\s]?by(?:[-\s]?nc)?(?:[-\s]?4\.0)?|"
    r"closed/api|bsd(?:[-\s]?3)?"
    r")$")
_BENCH_FRAGMENT = re.compile(r"(?i)\b(?:bench|suite|edition)\b|\b\d+\.\d{3,}\b")
_GLUE_PHRASE = re.compile(
    r"(?i)^(?:under|with|and|or|at|for|a|an|the|of|in|on|to|license|"
    r"param|params|parameters)(?:\s+(?:a|an|the|of|to))*$")
_ENTRY_LINE_RE = re.compile(
    r"^(?P<prefix>\s*(?:•\s*)?)<b>(?P<name>[^<]+)</b>\s*[—–-]\s*(?P<rest>.*)$")
_GENERIC_LINK_LABEL = re.compile(r"^(?:→\s*)?(?:source|s|src|link)$", re.I)
_MENTION_RE = re.compile(r"\b[A-Z][A-Za-z0-9.+-]{3,}\b")
_LICENSE_TABLE = {
    "apache-2.0": "Apache-2.0",
    "apache-2": "Apache-2.0",
    "mit": "MIT",
    "cc-by-nc-4.0": "CC BY-NC 4.0",
    "cc-by-4.0": "CC BY 4.0",
    "cc-by-nc": "CC BY-NC",
    "cc-by": "CC BY",
    "closed/api": "",
    "other": "",
    "unknown": "",
    "none": "",
}


def _license_key(lic: str) -> str:
    if not lic:
        return ""
    key = re.sub(r"[\s_]+", "-", str(lic).strip().lower())
    return key.replace("apache2.0", "apache-2.0")


def _display_license(lic: Optional[str]) -> str:
    """Human license label. Placeholder values (Closed/API, other) render blank."""
    key = _license_key(lic or "")
    if not key:
        return ""
    if key in _LICENSE_TABLE:
        return _LICENSE_TABLE[key]
    if "closed" in key and "api" in key:
        return ""
    return str(lic).strip()


def _norm_magnitude_token(token: str) -> str:
    match = re.search(r"(\d+(?:\.\d+)?)\s*([KMBT])", token or "", re.I)
    if not match:
        return ""
    number = float(match.group(1))
    num = str(int(number)) if number == int(number) else f"{number:g}"
    return f"{num}{match.group(2).upper()}"


def _context_fragment(ctx) -> str:
    shown = _format_context(ctx)
    if not shown:
        return ""
    if shown[-1].isalpha():
        shown = shown[:-1] + shown[-1].upper()
    return f"{shown} context"


def _param_token(value: str) -> str:
    return re.sub(
        r"(?i)\s*(total|active|params|parameters)\b", "", (value or "")).strip()


def _params_fragment(model: ModelRelease) -> str:
    total = _param_token(getattr(model, "total_parameters", None) or "")
    active = _param_token(getattr(model, "active_parameters", None) or "")
    if (total and active
            and _norm_magnitude_token(total) != _norm_magnitude_token(active)):
        return f"{total} total / {active} active"
    if total:
        return f"{total} total"
    if active:
        return f"{active} active"
    return ""


def _released_fragment(release_date) -> str:
    day = str(release_date or "")[:10]
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return ""
    return f"Released {_MON_ABBR[parsed.month - 1]} {parsed.day}"


def _price_fragment(model: ModelRelease) -> str:
    pin = getattr(model, "pricing_input", None)
    if pin is None:
        return ""
    if pin == 0:
        return "FREE"
    pout = getattr(model, "pricing_output", None)
    if pout is None:
        return f"${pin:.2f} per 1M"
    return f"${pin:.2f}/${pout:.2f} per 1M"


def metadata_line(model: ModelRelease) -> str:
    """Fixed-order facts. Missing fields are omitted, never padded.

    Order: params, context, license, price, release date. Benchmarks are
    not included — card scores were being dumped verbatim (#224).
    """
    bits = [
        _params_fragment(model),
        _context_fragment(getattr(model, "context_window", None)),
        _display_license(getattr(model, "license", None)),
        _price_fragment(model),
        _released_fragment(getattr(model, "release_date", None)),
    ]
    return " · ".join(bit for bit in bits if bit)


def link_label(url: str) -> str:
    """Short label for a digest link, from the host. Never the word Source."""
    host = _url_host(url)
    if not host:
        return "Source"
    for domain, label in _HOST_LINK_LABELS:
        if host == domain or host.endswith("." + domain):
            return label
    return host


_NAME_ACRONYMS = {
    "gpt": "GPT", "llm": "LLM", "moe": "MoE", "vl": "VL", "ocr": "OCR",
    "asr": "ASR", "tts": "TTS", "hf": "HF", "api": "API",
}


def _pretty_name_token(token: str) -> str:
    if any(ch.isupper() for ch in token):
        return token
    acronym = _NAME_ACRONYMS.get(token.lower())
    if acronym:
        return acronym
    match = re.match(r"([a-z]+)(.*)$", token)
    if not match:
        return token
    word, rest = match.group(1), match.group(2)
    return word[:1].upper() + word[1:] + rest


def clean_display_name(name: str) -> str:
    seg = (name or "").strip().lstrip("~")
    if "/" in seg:
        seg = seg.split("/", 1)[1]
    seg = re.sub(r"(?i)(?::free|:latest|-latest)$", "", seg)
    seg = re.sub(r"(?i)-(?:it|instruct)$", "", seg)
    seg = seg.replace("_", " ")
    seg = re.sub(r"-+", " ", seg)
    seg = re.sub(r"\s+", " ", seg).strip()
    return " ".join(_pretty_name_token(part) for part in seg.split(" "))


def _entry_url(model: ModelRelease) -> str:
    for url in (getattr(model, "canonical_url", None), getattr(model, "url", None)):
        if url and not _host_is_aggregator(url):
            return _upgrade_http_url(url)
    for url in (getattr(model, "canonical_url", None), getattr(model, "url", None)):
        if url:
            return _upgrade_http_url(url)
    return ""


def _model_magnitudes(model: ModelRelease) -> set:
    keys = set()
    for value in (getattr(model, "total_parameters", None),
                  getattr(model, "active_parameters", None)):
        token = _norm_magnitude_token(value or "")
        if token:
            keys.add(token)
    ctx = _context_fragment(getattr(model, "context_window", None))
    token = _norm_magnitude_token(ctx)
    if token:
        keys.add(token)
    return keys


def _magnitude_tokens(text: str) -> List[str]:
    scrubbed = re.sub(r"(?i)\bper\s+1\s*[m]\b", " ", text or "")
    out = []
    for num, unit in re.findall(r"\b(\d+(?:\.\d+)?)\s*([KMBT])\b", scrubbed, re.I):
        token = _norm_magnitude_token(f"{num}{unit}")
        if token and token not in out:
            out.append(token)
    return out


def _price_values(model: ModelRelease) -> List[float]:
    vals = []
    for price in (getattr(model, "pricing_input", None),
                  getattr(model, "pricing_output", None)):
        if price is None:
            continue
        vals.append(float(price))
    return vals


def _number_conflict_reason(text: str, models: List[ModelRelease]) -> str:
    allowed = set()
    prices: List[float] = []
    for model in models or []:
        allowed |= _model_magnitudes(model)
        prices.extend(_price_values(model))
    bad = [token for token in _magnitude_tokens(text) if token not in allowed]
    if bad:
        return "number " + ", ".join(bad) + " is not in the structured fields"
    for amount in re.findall(r"\$(\d+(?:\.\d+)?)", text or ""):
        if not prices or not any(abs(float(amount) - price) < 0.001 for price in prices):
            return f"number ${amount} is not in the structured fields"
    return ""


def _license_spellings(lic: Optional[str]) -> List[str]:
    key = _license_key(lic or "")
    if not key or not _display_license(lic):
        return []
    shown = _display_license(lic)
    spells = []
    for candidate in (shown, (lic or "").strip(), shown.replace("-", " "),
                      shown.replace(" ", "-")):
        if candidate and candidate not in spells:
            spells.append(candidate)
    return spells


def _is_fact_fragment(part: str, model: ModelRelease) -> bool:
    text = part.strip(" .;:")
    if not text:
        return True
    if (_PARAM_FRAGMENT.match(text) or _RELEASED_FRAGMENT.match(text)
            or _LICENSE_FRAGMENT.match(text)):
        return True
    if _BENCH_FRAGMENT.search(text) and len(text) < 80:
        return True
    shown = _display_license(getattr(model, "license", None))
    raw = (getattr(model, "license", None) or "").strip()
    if shown and text.lower() == shown.lower():
        return True
    if raw and text.lower() == raw.lower():
        return True
    return False


def _strip_inline_facts(text: str, model: ModelRelease) -> str:
    for spelling in _license_spellings(getattr(model, "license", None)):
        text = re.sub(rf"(?i)\b{re.escape(spelling)}\b", "", text)
    text = re.sub(
        r"(?i)\b\d+(?:\.\d+)?\s*[bmkt]\s*(?:total|active)?\s*(?:params|parameters)\b",
        "", text)
    text = re.sub(
        r"(?i)\b\d+(?:\.\d+)?\s*[bmkt]\s+(?:total|active)\b", "", text)
    text = re.sub(
        r"(?i)\breleased:?\s+(?:\d{4}-\d{2}-\d{2}|[a-z]+\.?\s+\d{1,2})\b",
        "", text)
    text = re.sub(r"(?i)\blicense:?\s*", "", text)
    text = re.sub(r"(?i)\b[\w.-]*bench[\w.-]*\b", "", text)
    text = re.sub(r"\b\d+\.\d{3,}\b", "", text)
    text = re.sub(
        r"\$\d+(?:\.\d+)?(?:\s*/\s*\$\d+(?:\.\d+)?)?"
        r"(?:\s*per\s+1\s*[m]\s*(?:tokens?)?)?",
        "", text, flags=re.I)
    return text


def clean_item_prose(prose: str, model: ModelRelease) -> str:
    """One differentiator sentence. Restated specs are removed; a sentence
    whose numbers contradict the structured fields is dropped and logged."""
    text = html.unescape(re.sub(r"<[^>]+>", "", prose or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        sentence = sentence.strip()
        if not sentence:
            continue
        parts = [p.strip(" .;") for p in re.split(r"\s*(?:,|·)\s*", sentence)]
        kept_parts = []
        for part in parts:
            if not part:
                continue
            reason = _number_conflict_reason(part, [model])
            if reason:
                print(f"Dropped prose clause ({reason}): {part[:180]}",
                      file=sys.stderr)
                continue
            if _is_fact_fragment(part, model):
                continue
            part = _strip_inline_facts(part, model)
            part = re.sub(
                r"(?i)(?:\s+\b(?:at|with|for|under|of|per)\b)+\s*$", "", part)
            part = re.sub(r"\s+", " ", part).strip(" ,;·-")
            if not part or _GLUE_PHRASE.match(part):
                continue
            kept_parts.append(part)
        if not kept_parts:
            continue
        rebuilt = re.sub(r"\s+", " ", ", ".join(kept_parts)).strip(" ,;·")
        if not rebuilt:
            continue
        if rebuilt[-1] not in ".!?":
            rebuilt += "."
        kept.append(rebuilt)
    return " ".join(kept)


def render_model_entry(model: ModelRelease, prose: str = "",
                       display_name: str = None) -> str:
    """One digest entry. Prose is the writer's sentence; every fact after it
    comes from the model fields, in metadata_line order, with a host link label.
    """
    name = html.escape(display_name or clean_display_name(model.name), quote=False)
    cleaned = clean_item_prose(prose or "", model)
    meta = metadata_line(model)
    avail = _availability_tag(model)
    tail = ". ".join(bit for bit in (meta, avail) if bit)
    if cleaned:
        head = f"<b>{name}</b> — <i>{html.escape(cleaned, quote=False)}</i>"
    else:
        head = f"<b>{name}</b> —"
    line = f"{head} {tail}." if tail else head
    url = _entry_url(model)
    if url:
        label = html.escape(link_label(url), quote=False)
        href = html.escape(url, quote=True)
        line += f' <a href="{href}">→ {label}</a>'
    return line


def _allowed_name_blobs(models: List[ModelRelease]) -> List[str]:
    blobs = []
    for model in models or []:
        for raw in (model.name, clean_display_name(model.name),
                    model.provider or "",
                    _display_license(getattr(model, "license", None))):
            if raw:
                blobs.append(raw.lower())
        pretty = clean_display_name(model.name).lower()
        blobs.extend(re.findall(r"[a-z0-9][a-z0-9.+-]{2,}", pretty))
    return blobs


def _mention_is_allowed(mention: str, blobs: List[str]) -> bool:
    token = mention.lower().rstrip("'")
    if token.endswith("'s"):
        token = token[:-2]
    if token in _TAKE_STOPWORDS:
        return True
    compact = _compact_match_text(token)
    for blob in blobs:
        if token in blob or blob in token:
            return True
        other = _compact_match_text(blob)
        if (compact and other and min(len(compact), len(other)) >= 4
                and (compact in other or other in compact)):
            return True
    return False


def _unlisted_mention(text: str, models: List[ModelRelease]) -> str:
    blobs = _allowed_name_blobs(models)
    for match in _MENTION_RE.finditer(text or ""):
        token = match.group(0)
        if "-" in token and not re.search(r"\d", token):
            continue
        if _mention_is_allowed(token, blobs):
            continue
        return token
    return ""


def guard_take(take: str, models: List[ModelRelease],
               today: str = None) -> Tuple[str, Optional[str]]:
    """Return (take, None) or ("", reason).

    The Take may only name items in this digest, and every magnitude in it
    must match a structured field. On failure the caller drops the Take.
    """
    text = re.sub(r"(?i)</?i>", "", take or "")
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
    if not text or text.upper() == "NONE":
        return "", None
    mention = _unlisted_mention(text, models)
    if mention:
        return "", f"unlisted mention {mention}"
    reason = _number_conflict_reason(text, models)
    if reason:
        return "", reason
    dates = _loose_release_dates(text, today=today)
    stale = [d for d in dates if digest_release_is_stale(d, today=today)]
    if stale:
        return "", f"stale date {stale[0]}"
    return text, None


def format_take_html(text: str) -> str:
    """Take italics are applied here, not by the writer model."""
    raw = re.sub(r"(?i)</?i>", "", text or "")
    raw = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
    if not raw or raw.upper() == "NONE":
        return ""
    return f"<i>{html.escape(raw, quote=False)}</i>"


def _models_in_digest_window(models, today: str = None) -> List[ModelRelease]:
    """Drop dated releases outside the digest news window. Undated models stay."""
    kept = []
    for model in models or []:
        if model is None:
            continue
        if digest_release_is_stale(getattr(model, "release_date", None), today=today):
            log_dropped_candidate(
                "filter", getattr(model, "name", None),
                f"digest window >{DIGEST_FRESHNESS_DAYS}d "
                f"date={str(getattr(model, 'release_date', '') or '')[:10]}")
            continue
        kept.append(model)
    return kept


def _digest_dateline(today: str = None) -> str:
    if today:
        ref = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        ref = datetime.now(timezone.utc)
    return ref.strftime("%A, %B %d, %Y")


def _match_model(name: str, models: List[ModelRelease]):
    key = _compact_match_text(name)
    if len(key) < 3:
        return None
    best = None
    best_score = 0
    for model in models or []:
        candidates = [
            clean_display_name(model.name),
            (model.name or "").split("/")[-1],
            model.name or "",
        ]
        for cand in candidates:
            compact = _compact_match_text(cand)
            if not compact:
                continue
            if compact == key:
                return model
            if (len(key) >= 6 and len(compact) >= 6
                    and (key in compact or compact in key)):
                score = min(len(key), len(compact))
                if score > best_score:
                    best = model
                    best_score = score
    return best


def _prose_from_entry_rest(rest: str) -> str:
    rest = re.sub(r'(?i)<a\s+href="[^"]*"[^>]*>.*?</a>', "", rest or "")
    italics = re.findall(r"(?is)<i>(.*?)</i>", rest)
    if italics:
        text = " ".join(html.unescape(re.sub(r"<[^>]+>", "", part)) for part in italics)
    else:
        text = html.unescape(re.sub(r"<[^>]+>", "", rest))
    return re.sub(r"\s+", " ", text).strip()


def _relabel_generic_links(text: str) -> str:
    def repl(match):
        href, label = match.group(1), match.group(2).strip()
        if not _GENERIC_LINK_LABEL.match(label):
            return match.group(0)
        return f'<a href="{href}">→ {link_label(href)}</a>'

    return re.sub(r'(?i)<a href="([^"]*)">([^<]*)</a>', repl, text or "")


def _parse_writer_blocks(summary: str):
    """TAKE/ITEM/PROSE blocks, or None when the writer emitted HTML instead."""
    if not re.search(r"(?im)^(TAKE|ITEM):", summary or ""):
        return None
    take = ""
    items = []
    current = None
    for line in (summary or "").splitlines():
        if re.match(r"(?i)TAKE:\s*", line):
            take = re.sub(r"(?i)^TAKE:\s*", "", line).strip()
            current = None
        elif re.match(r"(?i)ITEM:\s*", line):
            current = {"name": re.sub(r"(?i)^ITEM:\s*", "", line).strip(),
                       "prose": ""}
            items.append(current)
        elif re.match(r"(?i)PROSE:\s*", line) and current is not None:
            current["prose"] = re.sub(r"(?i)^PROSE:\s*", "", line).strip()
    return take, items


def _render_structured(take: str, items: list, models: List[ModelRelease],
                       today: str = None) -> Tuple[str, List[str]]:
    fresh = [m for m in (models or [])
             if not digest_release_is_stale(getattr(m, "release_date", None), today=today)]
    lines = []
    stale_names = []
    kept, reason = guard_take(take or "", fresh, today=today)
    if reason:
        print(f"Dropped Take ({reason}): {(take or '')[:200]}", file=sys.stderr)
    elif kept:
        lines.extend([format_take_html(kept), ""])
    buckets = {key: [] for key in
               ("open_frontier", "closed_frontier", "specialized", "local", "other")}
    for item in items or []:
        model = _match_model(item.get("name") or "", models)
        if model is None:
            print(f"Dropped entry with no candidate: {item.get('name')}",
                  file=sys.stderr)
            continue
        if digest_release_is_stale(getattr(model, "release_date", None), today=today):
            label = f"{item.get('name')} ({str(model.release_date)[:10]})"
            stale_names.append(label)
            print(f"Dropped stale digest entry {label}.", file=sys.stderr)
            continue
        buckets[categorize_model(model)].append(render_model_entry(
            model, item.get("prose") or "", display_name=item.get("name") or None))
    headers = (
        ("open_frontier", "OPEN FRONTIER", "🔓"),
        ("closed_frontier", "CLOSED FRONTIER", "🔒"),
        ("specialized", "SPECIALIZED", "🎯"),
        ("local", "LOCAL", "🏠"),
    )
    for key, title, emoji in headers:
        if not buckets[key]:
            continue
        lines.extend(["", f"━━━ <b>{title}</b> {emoji}", ""])
        for entry in buckets[key]:
            lines.extend([entry, ""])
    if buckets["other"]:
        lines.extend(["", "━━━ <b>ALSO TRACKED</b>", ""])
        for entry in buckets["other"]:
            lines.extend([entry, ""])
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text, stale_names


def _rewrite_html_entries(summary: str, models: List[ModelRelease],
                          today: str = None) -> Tuple[str, List[str]]:
    """Replace writer-formatted entry lines with render_model_entry.

    A matched model outside the digest window is dropped. An unmatched line
    keeps its prose (web-only) but its generic Source label is rewritten.
    """
    stale_names = []
    out = []
    for line in (summary or "").split("\n"):
        match = _ENTRY_LINE_RE.match(line)
        if not match:
            out.append(line)
            continue
        name = match.group("name").strip()
        model = _match_model(name, models)
        if model is None:
            out.append(_relabel_generic_links(line))
            continue
        if digest_release_is_stale(getattr(model, "release_date", None), today=today):
            label = f"{name} ({str(getattr(model, 'release_date', '') or '')[:10]})"
            stale_names.append(label)
            print(f"Dropped stale digest entry {label}.", file=sys.stderr)
            continue
        prose = _prose_from_entry_rest(match.group("rest"))
        rendered = render_model_entry(model, prose, display_name=name)
        out.append(f"{match.group('prefix')}{rendered}")
    return _prune_orphaned_tiers(out), stale_names


def _guard_take_line(summary: str, models: List[ModelRelease],
                     today: str = None) -> str:
    lines = (summary or "").split("\n")
    idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if (stripped.startswith("━━━") or stripped.startswith("<b>")
                or stripped.startswith("•")):
            break
        idx = i
        break
    if idx is None:
        return summary
    raw = lines[idx].strip()
    text = re.sub(r"(?i)</?i>", "", raw)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
    if not text or re.match(
            r"(?i)^(monday|tuesday|wednesday|thursday|friday|saturday|sunday),",
            text):
        return summary
    fresh = [m for m in (models or [])
             if not digest_release_is_stale(getattr(m, "release_date", None), today=today)]
    kept, reason = guard_take(text, fresh, today=today)
    if reason:
        print(f"Dropped Take ({reason}): {text[:200]}", file=sys.stderr)
        del lines[idx]
    elif not kept:
        del lines[idx]
    else:
        lines[idx] = format_take_html(kept)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _apply_writer_format(summary: str, models: List[ModelRelease],
                         today: str = None) -> Tuple[str, List[str]]:
    """Render metadata, link labels, and Take italics from structured fields.

    HTML from the writer and TAKE/ITEM/PROSE blocks both come out in the
    same entry shape, so the fallback model cannot change the format.
    """
    parsed = _parse_writer_blocks(summary)
    if parsed is not None:
        take, items = parsed
        return _render_structured(take, items, models, today=today)
    rewritten, stale_names = _rewrite_html_entries(summary, models, today=today)
    rewritten = _relabel_generic_links(rewritten)
    rewritten = _guard_take_line(rewritten, models, today=today)
    return rewritten, stale_names


def build_digest_message(models: List[ModelRelease], today: str = None) -> str:
    """Build tiered digest message (HTML format)."""
    if not models:
        return NO_MODELS_SENTINEL
    # Serving SKUs are not publishable leftovers. If they are all that
    # remains after the writer produced 0 entries, stay quiet rather than
    # posting an ALSO TRACKED stub (2026-08-29).
    models = [m for m in models if not is_openrouter_serving_sku(m.name)]
    if not models:
        return NO_MODELS_SENTINEL
    models, _ = prepare_models_for_digest(models)
    models = _models_in_digest_window(models, today=today)
    if not models:
        return NO_MODELS_SENTINEL

    # Deduplicate by base name
    seen = set()
    deduped = []
    for m in models:
        base = m.name.split("/")[-1].lower().replace(":free", "").replace("-latest", "")
        if base not in seen:
            seen.add(base)
            deduped.append(m)
    models = deduped[:20]

    tiers = {"open_frontier": [], "closed_frontier": [], "specialized": [],
             "local": [], "other": []}
    for m in models:
        tiers[categorize_model(m)].append(m)

    lines = [
        f"🤖 <b>ModelBytes Digest</b>",
        f"<i>{_digest_dateline(today)}</i>",
        "",
    ]

    def _section(title: str, emoji: str, items: List[ModelRelease]):
        if not items:
            return
        lines.extend(["", f"━━━ <b>{title}</b> {emoji}", ""])
        for m in items:
            prose = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', m.description or "")
            lines.append(render_model_entry(
                m, prose, display_name=clean_display_name(m.name)))
            lines.append("")

    _section("OPEN FRONTIER", "🔓", tiers["open_frontier"])
    _section("CLOSED FRONTIER", "🔒", tiers["closed_frontier"])
    _section("SPECIALIZED", "🎯", tiers["specialized"])
    _section("LOCAL", "🏠", tiers["local"])

    if tiers["other"]:
        lines.extend(["", "━━━ <b>ALSO TRACKED</b>", ""])
        for m in tiers["other"][:10]:
            name = m.name.split('/')[-1]
            link = m.canonical_url or m.url
            if link:
                lines.append(f'  • <a href="{link}">{name}</a> ({m.source})')
            else:
                lines.append(f"  • {name} ({m.source})")
        if len(tiers["other"]) > 10:
            lines.append(f"  …and {len(tiers['other']) - 10} more")
        lines.append("")

    total = len(models)
    lines.extend(["", f"Total: {total} items tracked today"])
    return "\n".join(lines)


# The grammar every rendered digest entry follows: a bold display name then a
# dash separator ("<b>MiniMax M3</b> — ..."). Em-dash is what the prompt
# specifies; hyphen and en-dash (–, U+2013) are also accepted because the writer
# occasionally substitutes them — an en-dash entry read as "no entry" and
# silently zeroed a 0-fetched-model digest into a no-post (2026-07-12). The
# hyphen sits last in the class so it is a literal, not a range. This one
# definition is shared by the surfaced-count and the survive-check so they agree.
_ENTRY_GRAMMAR = r"<b>[^<]+</b>\s*[—–-]"


def _count_surfaced_models(summary: str) -> int:
    """Count model entries actually rendered in an LLM digest body.

    Entries are bold-name lines followed by an em-dash/en-dash/hyphen
    ("<b>Name</b> — ...") plus Local Ready bullets ("• ..."). Tier headers
    like "<b>🔓 Premier Open</b>" have no trailing dash and are not counted.
    """
    count = 0
    for raw in summary.splitlines():
        line = raw.strip()
        if re.match(_ENTRY_GRAMMAR, line):
            count += 1
        elif line.startswith("•"):
            count += 1
    return count


# How the last summarize_models() call produced its digest: 'llm' or
# 'template'. Lets the publisher record/alert which fallback tier ran.
LAST_SUMMARY_MODE = "template"
# The model that actually produced the last digest (None if template). Lets the
# publisher alert when the primary was unavailable and a fallback model was used.
LAST_LLM_MODEL = None
# Why the last failed LLM candidate produced nothing (HTTP error, empty
# content + finish_reason). Feeds the primary-missed ops alert so
# "unavailable" is not used for a 200 with thinking-only output (2026-08-17).
LAST_LLM_FAILURE = None
# How many entries the last summarize_models() call trimmed for carrying a stale
# release date. Lets main() send a non-blocking ops note that the writer leaked
# a stale date and an entry was dropped (2026-07-04 incident).
LAST_STALE_DROPPED = 0
# Display labels for those entries ("Shieldstral (2026-08-04)") so the ops
# note names what was trimmed — a count-only alert is a forensic session
# (2026-08-21: take still mentioned a 3B safety classifier; Railway logs
# only said "Dropped 1").
LAST_STALE_DROPPED_NAMES: List[str] = []
# Writer-entry counts from the last summarize_models() call. Lets main() tell
# "writer produced 0 on a quiet day" (do not 🚨) from "writer produced N and
# verification stripped them all" (do 🚨) — the 2026-08-24 alert blamed
# link/stale verification on a writer-0 day.
LAST_WRITER_N_WRITTEN = 0
LAST_LINK_DROPPED = 0
# Primary-source Parallel.ai hits promoted to ModelRelease objects so a
# catalog-quiet day can still post via the template when the writer emits
# nothing. Reset at the start of each discover_recent_releases() call.
LAST_DISCOVERY_MODELS: List[ModelRelease] = []


def _recent_digest_names(today: str = None, days: int = 10,
                         pending_dir: Path = None) -> List[str]:
    """Bold entry names from the last `days` published digests (today excluded),
    so the writer doesn't repeat a model we already covered.

    Reads posted_digests.body first (durable), then pending/*.txt for dates
    the DB has no body for.
    """
    seen, out = set(), []
    for _date, text in _digest_history(
            today=today, days=days, pending_dir=pending_dir):
        for name in _ENTRY_RE.findall(text):
            name = name.strip()
            if name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
    return out


_HF_REPO_RE = re.compile(
    r"https?://(?:www\.)?huggingface\.co/([^/]+)/([^/?#]+)", re.I)
_OR_MODEL_RE = re.compile(
    r"https?://(?:www\.)?openrouter\.ai/models/([^?#]+)", re.I)
_OLLAMA_LIB_RE = re.compile(
    r"https?://(?:www\.)?ollama\.com/library/([^/?#]+)", re.I)
_ROUNDUP_TITLE_RE = re.compile(
    r"(?i)\b("
    r"this week|round-?up|release tracker|weekly(?: ai| llm)?|"
    r"daily update|new models today|llm updates|"
    r"top \d+|best llms|best (?:ai |llm )models|every llm|"
    r"what.?s new in (?:ai|llms)|"
    r"latest (?:ai |llm )?models"
    r")\b")


def _url_host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _host_is_aggregator(url: str) -> bool:
    host = _url_host(url)
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in _AGGREGATOR_DOMAINS)


def _discovery_name(url: str, title: str) -> str:
    m = _HF_REPO_RE.search(url or "")
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    m = _OR_MODEL_RE.search(url or "")
    if m:
        return m.group(1).rstrip("/")
    m = _OLLAMA_LIB_RE.search(url or "")
    if m:
        return m.group(1)
    title = (title or "").strip()
    return title[:80] if title else (url or "discovery-hit")


def _discovery_hit_to_model(url: str, title: str, excerpt: str,
                            publish_date: str) -> Optional[ModelRelease]:
    """Turn one Parallel.ai hit into a ModelRelease, or None if it is not a
    candidate (aggregator, roundup, empty)."""
    if not url or not (title or excerpt):
        return None
    if _host_is_aggregator(url):
        return None
    if (_ROUNDUP_TITLE_RE.search(title or "")
            or _ROUNDUP_TITLE_RE.search(excerpt or "")):
        return None
    name = _discovery_name(url, title)
    pd = (publish_date or "").strip()[:10] or None
    return ModelRelease(
        name=name,
        provider=_resolve_provider(name.split("/")[0] if "/" in name else "",
                                   name),
        source="discovery",
        url=url,
        description=_smart_truncate(excerpt or title, 200),
        release_date=pd,
        canonical_url=url,
        confidence="low",
        unique_traits=["web_discovery"],
    )


def _discovery_already_covered(model: ModelRelease,
                               recent_names: List[str]) -> bool:
    blob = _compact_match_text(
        f"{model.name} {model.description or ''} {model.url or ''}")
    name_key = _compact_match_text((model.name or "").split("/")[-1])
    for r in recent_names or []:
        rk = _compact_match_text(r)
        if len(rk) < 6:
            continue
        if rk == name_key or (blob and rk in blob):
            return True
    return False


def _filter_discovery_models(models: List[ModelRelease],
                             recent_names: List[str] = None,
                             seen: Set[str] = None,
                             today: str = None,
                             max_age_days: int = 14) -> List[ModelRelease]:
    """Keep primary-source discovery hits that are fresh, unseen, and not
    already covered in recent digests. Aggregator/roundup pages are dropped
    so a catalog-quiet day does not re-post last week's tracker dump."""
    recent_names = recent_names or []
    seen = seen or set()
    out = []
    for m in models or []:
        if not m or not m.url:
            continue
        if _host_is_aggregator(m.url):
            continue
        if (_ROUNDUP_TITLE_RE.search(m.name or "")
                or _ROUNDUP_TITLE_RE.search(m.description or "")):
            continue
        if is_stale_release(m.release_date, today=today, max_age_days=max_age_days):
            continue
        if m.name in seen or m.url in seen:
            continue
        if _discovery_already_covered(m, recent_names):
            continue
        out.append(m)
    return out


def _discovery_search_queries(month: str) -> List[str]:
    """Web-search slots that used to be generic '{month} model release' queries.

    Those queries returned listicle and aggregator pages ('Best AI Models in
    October 2026', Manifold Markets, Use.ai). Lab RSS and news pages replaced
    them. The list stays empty so Parallel is not asked for a roundup.
    `month` is kept so a future primary-source query has one argument.
    """
    return []


# First-party lab blogs and news pages. Whichever had a working RSS or a
# stable dated page on 2026-10-04. Qwen's published RSS is the GitHub Pages
# feed (quiet since 2025-09; qwen.ai's blog is a JS shell with no feed).
LAB_NEWS_FEEDS = (
    {"lab": "OpenAI", "url": "https://openai.com/news/rss.xml", "kind": "rss"},
    {"lab": "Anthropic", "url": "https://www.anthropic.com/sitemap.xml",
     "kind": "sitemap", "path_contains": "/news/"},
    {"lab": "Google DeepMind", "url": "https://deepmind.google/blog/rss.xml",
     "kind": "rss"},
    {"lab": "Meta AI", "url": "https://ai.meta.com/blog/", "kind": "html_meta"},
    {"lab": "Mistral", "url": "https://mistral.ai/news/rss", "kind": "rss"},
    {"lab": "DeepSeek", "url": "https://api-docs.deepseek.com/sitemap.xml",
     "kind": "sitemap", "path_contains": "/news/news", "slug_date": True},
    {"lab": "Qwen", "url": "https://qwenlm.github.io/blog/index.xml", "kind": "rss"},
    {"lab": "Moonshot/Kimi", "url": "https://www.kimi.ai/blog/", "kind": "html_kimi"},
    {"lab": "Zhipu/GLM", "url": "https://www.zhipuai.cn/en/news", "kind": "html_zhipu"},
    {"lab": "xAI", "url": "https://x.ai/news", "kind": "html_xai"},
)


def _feed_bytes_to_text(raw: bytes) -> str:
    """Decode a feed body. Gunzip when the bytes are gzip (DeepMind has served
    a gzip body that a utf-8 decode rejects)."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def _response_feed_text(resp) -> str:
    raw = getattr(resp, "content", None)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if isinstance(raw, (bytes, bytearray)) and raw:
        return _feed_bytes_to_text(bytes(raw))
    return getattr(resp, "text", "") or ""


def _lab_feed_get(url: str, source_name: str):
    """One GET for a lab feed. No retry loop — a dead host must not stall the cron."""
    resp = requests.get(
        url,
        timeout=12,
        headers={
            "User-Agent": HTTP_USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/html;q=0.9, */*;q=0.8",
        },
    )
    resp.raise_for_status()
    return resp


def _clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(title or "")).strip()


def _title_from_url(url: str) -> str:
    path = unquote(urlparse(url).path or "").rstrip("/").split("/")[-1]
    path = re.sub(r"[-_]+", " ", path).strip()
    return path[:140] or url


def _lab_item(lab: str, title: str, url: str, publish_date: str, excerpt: str = ""):
    title = _clean_title(title) or _title_from_url(url)
    return {
        "lab": lab,
        "title": title,
        "url": url,
        "publish_date": publish_date or "",
        "excerpts": [excerpt] if excerpt else [],
        "require_date": True,
    }


def _xml_local(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _xml_text(el) -> str:
    return "".join(el.itertext()).strip()


def parse_rss_items(text: str, lab: str) -> list:
    """RSS or Atom items as discovery rows. [] on empty or unparseable input."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    items = []
    for el in root.iter():
        if _xml_local(el.tag) not in ("item", "entry"):
            continue
        title = ""
        excerpt = ""
        date = ""
        link = ""
        for child in list(el):
            name = _xml_local(child.tag)
            if name == "title" and not title:
                title = _xml_text(child)
            elif name in ("description", "summary") and not excerpt:
                excerpt = _xml_text(child)
            elif name in ("pubDate", "published", "updated", "date") and not date:
                date = _xml_text(child)
            elif name == "link" and not link:
                href = (child.get("href") or "").strip()
                text_link = _xml_text(child)
                rel = (child.get("rel") or "").lower()
                candidate = text_link or href
                if candidate.startswith("http") and rel in ("", "alternate"):
                    link = candidate
                elif candidate.startswith("http") and not link:
                    link = candidate
        if not link or not (title or excerpt):
            continue
        items.append(_lab_item(lab, title, link, _day(date) or "", excerpt))
    return items


def _deepseek_slug_date(url: str) -> Optional[str]:
    """news260910 → 2026-09-10. Four-digit legacy slugs are left to lastmod."""
    match = re.search(r"news(\d{2})(\d{2})(\d{2})(?:/|$)", url or "")
    if not match:
        return None
    try:
        return datetime(
            2000 + int(match.group(1)), int(match.group(2)), int(match.group(3)),
        ).date().isoformat()
    except ValueError:
        return None


def parse_sitemap_items(text: str, lab: str, path_contains: str = "",
                        slug_date: bool = False) -> list:
    text = (text or "").strip()
    if not text:
        return []
    items = []
    for block in re.findall(r"<url>(.*?)</url>", text, re.S):
        loc = re.search(r"<loc>\s*([^<\s]+)\s*</loc>", block)
        if not loc:
            continue
        url = loc.group(1).strip()
        if path_contains and path_contains not in url:
            continue
        lastmod = re.search(r"<lastmod>\s*([^<]+)\s*</lastmod>", block)
        date = _deepseek_slug_date(url) if slug_date else None
        if not date and lastmod:
            date = _day(lastmod.group(1))
        if not date:
            continue
        items.append(_lab_item(lab, _title_from_url(url), url, date, ""))
    return items


def _parse_html_xai(text: str, lab: str) -> list:
    items, seen = [], set()
    for match in re.finditer(r'<time\s+dateTime="(\d{4}-\d{2}-\d{2})"', text or ""):
        before = text[max(0, match.start() - 1200):match.start()]
        after = text[match.end():match.end() + 800]
        hrefs = re.findall(r'href="(/news/[^"]+)"', before)
        if not hrefs:
            continue
        path = hrefs[-1]
        if path.rstrip("/") in ("", "/news"):
            continue
        title_match = re.search(r"<h3[^>]*>([^<]+)", after)
        title = title_match.group(1).strip() if title_match else _title_from_url(path)
        url = "https://x.ai" + path
        if url in seen:
            continue
        seen.add(url)
        items.append(_lab_item(lab, title, url, match.group(1), ""))
    return items


def _parse_html_meta(text: str, lab: str) -> list:
    items, seen = [], set()
    pattern = re.compile(
        r'href="(https://ai\.meta\.com/blog/[^"]+)"[^>]*>([^<]{3,180})</a>',
        re.I,
    )
    for match in pattern.finditer(text or ""):
        url = match.group(1).split("?")[0]
        if url.rstrip("/").endswith("/blog"):
            continue
        if url in seen:
            continue
        window = (text or "")[match.end():match.end() + 500]
        date = _first_date_in(window)
        if not date:
            continue
        seen.add(url)
        items.append(_lab_item(lab, match.group(2), url, date, ""))
    return items


def _parse_html_kimi(text: str, lab: str) -> list:
    items, seen = [], set()
    pattern = re.compile(r'href="(/blog/[^"]+)"\s+aria-label="([^"]+)"')
    for match in pattern.finditer(text or ""):
        path = match.group(1).split("?")[0]
        if path.rstrip("/") in ("", "/blog"):
            continue
        url = "https://www.kimi.ai" + path
        if url in seen:
            continue
        window = (text or "")[match.start():match.start() + 1500]
        date = _first_date_in(window)
        if not date:
            continue
        seen.add(url)
        items.append(_lab_item(lab, match.group(2), url, date, ""))
    return items


def _parse_html_zhipu(text: str, lab: str) -> list:
    items, seen = [], set()
    for match in re.finditer(r'href="([^"]*?/en/news/\d+)"', text or ""):
        href = match.group(1)
        url = href if href.startswith("http") else "https://www.zhipuai.cn" + href
        if url in seen:
            continue
        window = (text or "")[max(0, match.start() - 400):match.end() + 600]
        date = _first_date_in(window)
        if not date:
            continue
        title_match = re.search(
            r'href="' + re.escape(href) + r'"[^>]*>([^<]{4,160})</a>',
            window,
        )
        title = title_match.group(1).strip() if title_match else _title_from_url(url)
        seen.add(url)
        items.append(_lab_item(lab, title, url, date, ""))
    return items


_HTML_NEWS_PARSERS = {
    "html_xai": _parse_html_xai,
    "html_meta": _parse_html_meta,
    "html_kimi": _parse_html_kimi,
    "html_zhipu": _parse_html_zhipu,
}


def parse_html_news_items(text: str, lab: str, kind: str) -> list:
    parser = _HTML_NEWS_PARSERS.get(kind)
    if parser is None:
        return []
    return parser(text or "", lab)


def _lab_item_is_fresh(item, ref, max_age_days: int) -> bool:
    day = _day(item.get("publish_date"))
    if not day:
        return False
    age = (ref - datetime.strptime(day, "%Y-%m-%d").date()).days
    return -2 <= age <= max_age_days


def _cap_lab_rows(rows, per_lab: int = 3, limit: int = 10) -> list:
    rows = sorted(rows, key=lambda row: row.get("publish_date") or "", reverse=True)
    counts, out = {}, []
    for row in rows:
        lab = row.get("lab") or ""
        if counts.get(lab, 0) >= per_lab:
            continue
        counts[lab] = counts.get(lab, 0) + 1
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _collect_lab_feed_rows(ref, max_age_days: int) -> list:
    """Fresh items from every configured lab feed. One failure does not blank the rest."""
    rows = []
    for feed in LAB_NEWS_FEEDS:
        lab = feed["lab"]
        try:
            resp = _lab_feed_get(feed["url"], lab)
            text = _response_feed_text(resp)
        except Exception as exc:
            _remember_source_error("Discovery", exc)
            print(f"lab feed {lab} failed: {_short_source_error(exc)}", file=sys.stderr)
            continue
        kind = feed.get("kind")
        try:
            if kind == "rss":
                items = parse_rss_items(text, lab)
            elif kind == "sitemap":
                items = parse_sitemap_items(
                    text, lab,
                    path_contains=feed.get("path_contains") or "",
                    slug_date=bool(feed.get("slug_date")),
                )
            else:
                items = parse_html_news_items(text, lab, kind)
        except Exception as exc:
            _remember_source_error("Discovery", exc)
            print(f"lab feed {lab} parse failed: {_short_source_error(exc)}",
                  file=sys.stderr)
            continue
        for item in items:
            if _lab_item_is_fresh(item, ref, max_age_days):
                rows.append(item)
    return _cap_lab_rows(rows)


def discover_recent_releases(today: str = None, max_age_days: int = 14,
                             timeout: int = 60) -> str:
    """Lab blogs/news first, then any remaining primary-source web search.

    The generic '{month} AI model release' Parallel slots returned listicle
    pages (2026-10-04 audit). Those slots are now the lab feeds in
    LAB_NEWS_FEEDS, which run even when Parallel is disabled. Also fills
    LAST_DISCOVERY_MODELS. Returns '' when nothing fresh survived. Never raises.
    """
    global LAST_DISCOVERY_MODELS, _DISCOVERY_DISABLED
    LAST_DISCOVERY_MODELS = []
    _DISCOVERY_DISABLED = not (DISCOVERY_ENABLED and PARALLEL_API_KEY)
    ref = (datetime.strptime(today, "%Y-%m-%d").date() if today
           else datetime.now(timezone.utc).date())
    month = ref.strftime("%B %Y")
    results = list(_collect_lab_feed_rows(ref, max_age_days))
    queries = _discovery_search_queries(month)
    if queries and not _DISCOVERY_DISABLED:
        body = {
            "objective": (
                f"Find AI models newly released or updated within {max_age_days} days "
                f"of {ref.isoformat()}: open-weight and API models across text, reasoning, "
                "coding, multimodal, and audio. Prefer primary sources (vendor blogs, "
                "model cards, release notes) stating the release date and specs."),
            "search_queries": queries,
        }
        try:
            resp = requests.post(PARALLEL_SEARCH_URL, json=body,
                                 headers={"x-api-key": PARALLEL_API_KEY,
                                          "Content-Type": "application/json"},
                                 timeout=timeout)
            resp.raise_for_status()
            results.extend(resp.json().get("results", []) or [])
        except Exception as e:
            reason = _remember_source_error("Discovery", e)
            print(f"Parallel discovery failed: {reason}", file=sys.stderr)
            if not results:
                return ""
    if not results:
        return ""

    kept = []
    models = []
    for r in results:
        pd = (r.get("publish_date") or "").strip()
        if r.get("require_date") and not pd:
            continue
        if pd:
            try:
                age = (ref - datetime.strptime(pd[:10], "%Y-%m-%d").date()).days
                if age > max_age_days or age < -2:
                    continue  # too old, or implausibly future
            except ValueError:
                pass  # unparseable → keep, let the writer judge
        url = _upgrade_http_url((r.get("url") or "").strip())
        title = (r.get("title") or "").strip()
        excerpt = " ".join((r.get("excerpts") or [])[:2]).strip()
        if not url or not (title or excerpt):
            continue
        if _host_is_aggregator(url):
            continue
        if (_ROUNDUP_TITLE_RE.search(title)
                or _ROUNDUP_TITLE_RE.search(excerpt)):
            continue
        kept.append(f"- {title} ({pd or 'undated'}) — {url}\n  "
                    f"{_smart_truncate(excerpt, 280)}")
        model = _discovery_hit_to_model(url, title, excerpt, pd)
        if model:
            models.append(model)
        if len(kept) >= 10:
            break
    LAST_DISCOVERY_MODELS = models
    if not kept:
        return ""
    print(f"Discovery: {len(kept)} recent source(s)"
          f" ({len(models)} candidate model(s)).", file=sys.stderr)
    return "\n".join(kept)


def _collect_provided_urls(models, web_context: str) -> set:
    """Every URL we actually handed the writer — candidate model URLs + the
    source URLs from the Parallel web research. A digest link outside this set
    was constructed by the model and must not be published."""
    urls = set()
    for m in models or []:
        for u in (getattr(m, "url", None), getattr(m, "canonical_url", None)):
            if u:
                urls.update(_url_scheme_variants(u))
    for u in re.findall(r"https?://[^\s)\]]+", web_context or ""):
        urls.update(_url_scheme_variants(u))
    return urls


def _strip_unverified_links(summary: str, allowed_urls: set) -> Tuple[str, int]:
    """Drop any one-line entry whose <a href> wasn't a URL we provided (the
    writer must cite a real source, never construct one — the curator verified
    URLs by fetching; this enforces it deterministically). Then drop tier
    headers left with no entries. Returns (cleaned, dropped_count)."""
    kept, dropped = [], 0
    for line in summary.split("\n"):
        hrefs = re.findall(r'<a href="([^"]+)"', line)
        if hrefs and not all(_href_is_provided(h, allowed_urls) for h in hrefs):
            dropped += 1
            continue
        kept.append(line)
    return _prune_orphaned_tiers(kept), dropped


def _prune_orphaned_tiers(lines: List[str]) -> str:
    """After entries have been dropped line-by-line, remove any tier header left
    with no entries under it, then collapse the blank lines. Shared by the
    unverified-link scrub and the stale-date scrub so both degrade a digest the
    same way."""
    out = []
    for i, line in enumerate(lines):
        if line.lstrip().startswith("━━━"):
            has_entry = False
            for nxt in lines[i + 1:]:
                ls = nxt.lstrip()
                if ls.startswith("━━━"):
                    break
                if ls.startswith("<b>") or ls.startswith("•"):
                    has_entry = True
                    break
            if not has_entry:
                continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _stale_drop_label(line: str, today: str = None,
                      max_age_days: int = None) -> str:
    """Human label for a stale-scrubbed entry: 'Name (YYYY-MM-DD)'."""
    if max_age_days is None:
        max_age_days = DIGEST_FRESHNESS_DAYS
    m = _ENTRY_RE.search(line)
    name = (m.group(1).strip() if m else "") or "unnamed entry"
    stale_dates = [d for d in _loose_release_dates(line, today=today)
                   if is_stale_release(d, today=today, max_age_days=max_age_days)]
    if stale_dates:
        return f"{name} ({stale_dates[0]})"
    return name


def _strip_stale_entries(summary: str, today: str = None,
                         max_age_days: int = None) -> Tuple[str, List[str]]:
    """Drop any single entry line carrying a release date too old to be "new
    today", then prune orphaned tier headers. This is the per-entry counterpart
    to the whole-body stale-release gate: it reuses the SAME parser
    (_loose_release_dates) and the SAME predicate (is_stale_release), pinned to
    one reference date, so a stale date the gate would block is removed at entry
    granularity first. One hallucinated date (from an undated web source or the
    writer's own knowledge) therefore trims a single entry instead of taking the
    whole digest dark (the 2026-07-04 incident). The gate stays as the backstop
    for a stale date that lands outside an entry line. Returns (cleaned, labels)
    where labels are 'Name (YYYY-MM-DD)' so the ops note can name the trim
    (2026-08-21: count-only alert left the operator guessing).
    """
    if max_age_days is None:
        max_age_days = DIGEST_FRESHNESS_DAYS
    kept, dropped = [], []
    for line in summary.split("\n"):
        if any(is_stale_release(d, today=today, max_age_days=max_age_days)
               for d in _loose_release_dates(line, today=today)):
            dropped.append(_stale_drop_label(
                line, today=today, max_age_days=max_age_days))
            continue
        kept.append(line)
    return _prune_orphaned_tiers(kept), dropped


LLM_MAX_TOKENS = 16000  # 8000 was eaten by deepseek-v4-pro thinking (2026-08-17)
LLM_MAX_TOKENS_RETRY = 24000


def _llm_message_text(msg: dict) -> str:
    """Visible assistant text. Never use reasoning/thinking fields as the digest."""
    content = (msg or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                parts.append(p.get("text") or "")
        return "\n".join(parts).strip()
    return ""


def _diagnose_empty_llm(model: str, data: dict, choice: dict) -> str:
    finish = (choice or {}).get("finish_reason") or "?"
    msg = (choice or {}).get("message") or {}
    usage = (data or {}).get("usage") or {}
    reasoning_len = 0
    for k in ("reasoning", "reasoning_content", "thinking"):
        v = msg.get(k)
        if isinstance(v, str):
            reasoning_len = max(reasoning_len, len(v))
    bits = [f"empty content, finish_reason={finish}"]
    ct, pt = usage.get("completion_tokens"), usage.get("prompt_tokens")
    if ct is not None or pt is not None:
        bits.append(f"tokens={ct}/{pt}")
    if reasoning_len:
        if finish == "length":
            bits.append(f"reasoning_chars={reasoning_len} (thinking ate the token budget)")
        else:
            bits.append(f"reasoning_chars={reasoning_len}")
    return f"LLM '{model}' " + ", ".join(bits)


def _call_llm(model: str, prompt: str, max_tokens: int = None) -> Optional[str]:
    """One chat-completion call against the configured OpenAI-compatible endpoint.
    Returns the stripped content, or None on any failure or empty body — the
    caller decides whether to try the next model or the template.

    On empty content with finish_reason=length (reasoning burned the budget),
    retries once at LLM_MAX_TOKENS_RETRY before giving up (2026-08-17:
    deepseek-v4-pro HTTP 200 / empty content looked like an outage).
    """
    global LAST_LLM_FAILURE
    if max_tokens is None:
        max_tokens = LLM_MAX_TOKENS
    try:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            # Headroom for reasoning models that spend tokens on hidden reasoning
            # before emitting the digest body (3000 produced empty bodies twice;
            # 8000 still wasn't enough on 2026-08-17 for deepseek-v4-pro).
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        headers = {"Authorization": f"Bearer {LLM_API_KEY}",
                   "Content-Type": "application/json"}
        resp = requests.post(f"{LLM_BASE_URL}/chat/completions",
                             json=payload, headers=headers, timeout=LLM_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        text = _llm_message_text(choice.get("message") or {})
        if text:
            if model == LLM_MODEL:
                LAST_LLM_FAILURE = None  # primary recovered (possibly after retry)
            return text
        LAST_LLM_FAILURE = _diagnose_empty_llm(model, data, choice)
        print(LAST_LLM_FAILURE, file=sys.stderr)
        if (choice.get("finish_reason") == "length"
                and max_tokens < LLM_MAX_TOKENS_RETRY):
            print(f"Retrying '{model}' with max_tokens={LLM_MAX_TOKENS_RETRY} "
                  "(reasoning likely ate the budget).", file=sys.stderr)
            return _call_llm(model, prompt, max_tokens=LLM_MAX_TOKENS_RETRY)
        return None
    except Exception as e:
        LAST_LLM_FAILURE = _redact_secrets(f"LLM '{model}' call failed: {e}")
        print(LAST_LLM_FAILURE, file=sys.stderr)
        return None


def _primary_missed_alert(today: str) -> str:
    """Ops copy when we published with the secondary model. Must say *why*
    the primary missed — 'unavailable' was a lie on 2026-08-17 (HTTP 200,
    empty content, thinking consumed max_tokens)."""
    why = LAST_LLM_FAILURE or "primary returned empty"
    return (f"Primary LLM '{LLM_MODEL}' missed for {today} — published with "
            f"'{LAST_LLM_MODEL}'. {why}. Channel still posted.")


# Variant-suffix patterns that signal a FINE-TUNE / SFT / data variant of the
# same base — these collapse. Empty list would mean "everything collapses."
# Updated whenever a new variant-tag pattern shows up in the wild.
_CAPABILITY_TIERS = {
    "instruct", "it", "chat", "base", "raw", "foundation",
    "sft", "rlhf", "dpo",  # post-training stages — real capability signal
}
_COLLAPSE_THRESHOLD = 3  # Decision 1 (Sov, 2026-06-22 spec): ≥3 to collapse


def _variant_suffix(model_name: str, size: Optional[str]) -> Optional[str]:
    """The trailing variant tag of a model name, IF it's a collapsible one.

    'allenai/qwen35-9b-termigen' → 'termigen'  (collapse)
    'x/Llama-4-8B-Math'          → 'Math'       (collapse)
    'x/Foo-8b'                   → None         (no suffix — its own family)
    'meta-llama/Llama-4-70B-Instruct' → None    (capability tier — protected)
    'allenai/tmax-27b'           → None         (no suffix)

    Returns None for capability-tier suffixes (instruct/base/it/chat/sft/…)
    so a real instruct-vs-base pair at the same size stays separate.
    """
    seg = model_name.split("/")[-1]
    low = seg.lower()
    # Find the size token's position; the variant suffix is whatever trails it.
    if not size:
        return None
    # match the size token case-insensitively (e.g. '9b', '70B', '8B')
    size_pat = re.compile(re.escape(size).replace("B", r"[bB]").replace("M", r"[mM]"))
    m = size_pat.search(low)
    if not m:
        return None
    tail = seg[m.end():].lstrip("-_")
    if not tail:
        return None
    # The whole trailing chunk after the size is the variant label. If its
    # first token is a capability tier, this is NOT a collapsible variant.
    first = tail.split("-", 1)[0].lower()
    if first in _CAPABILITY_TIERS:
        return None
    return tail


def collapse_variants(models: List["ModelRelease"]) -> List["ModelRelease"]:
    """Collapse N≥3 same-(org, base, size) variants into one family entry.

    Inline-path-only safety net (Decision 4). When an org drops a batch of
    variants at the same size (SFT variants, dataset-named fine-tunes), the
    dedup set treats each repo as distinct and the daily cap gets burned by one
    org's batch (2026-06-22: 6 qwen35-9b-* forks filled the digest). This groups
    by (org, family_core, size); a group of ≥3 collapses to one ModelRelease
    whose description names the family + count + the variant suffixes.

    Rules (Sov sign-off, 2026-06-22 Notion spec):
    - threshold ≥3 (Decision 1)
    - a variant that is_significant_release escapes to its own entry (3)
    - instruct/base/it/chat/sft/rlhf/dpo suffixes DON'T collapse (5)
    - the collapsed entry is a plain ModelRelease (no new type) so the LLM
      prompt and the template renderer consume it unchanged
    """
    if len(models) < _COLLAPSE_THRESHOLD:
        return list(models)

    def _family_key(m: "ModelRelease"):
        author = m.name.split("/")[0].lower() if "/" in m.name else ""
        seg = m.name.split("/")[-1]
        size = _param_size_from_name(m.name)
        suffix = _variant_suffix(m.name, size)
        # No suffix → singleton family key (won't collide with suffixed peers).
        # Strip the suffix off the segment to get the family core.
        core = seg
        if suffix and size:
            size_pat = re.compile(re.escape(size).replace("B", r"[bB]").replace("M", r"[mM]"))
            mm = size_pat.search(seg)
            if mm:
                core = seg[:mm.end()]  # e.g. 'qwen35-9b', 'Llama-4-8B'
        return (author, core.lower(), size or "?")

    # Pull ESCAPE variants out first — a variant with standout engagement
    # relative to its siblings surfaces on its own (Decision 3). is_significant_
    # release alone is too broad (it returns True for every known-family name
    # with no engagement floor, which would collapse nothing for qwen/llama).
    # The intent of 'significant variant escapes' is 'one that's actually taking
    # off' — so gate on being an engagement outlier within the batch, not on
    # family name. A high absolute floor also qualifies (a genuinely viral
    # release). Re-evaluated per-collapse, not globally.
    def _author(m):
        return m.name.split("/")[0].lower() if "/" in m.name else ""

    def _is_escape(m: "ModelRelease", peers: List["ModelRelease"]) -> bool:
        dl = m.downloads or 0
        # Absolute floor: a genuinely viral variant escapes on its own.
        if dl >= 100000 or (m.likes or 0) >= 1000:
            return True
        # Relative: an outlier vs its siblings (>=5× the peer median downloads).
        peer_dl = sorted(p.downloads or 0 for p in peers if p is not m)
        if peer_dl:
            median = peer_dl[len(peer_dl) // 2]
            return dl >= max(median * 5, median + 1000)
        return False

    # First pass: assign every model to a family group.
    groups: Dict[tuple, List["ModelRelease"]] = {}
    order: List[tuple] = []
    for m in models:
        k = _family_key(m)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(m)

    # Second pass: within each group, pull escape variants out to their own
    # entries; the rest collapse if ≥ threshold remain.
    escape_singletons: List["ModelRelease"] = []
    collapsible_groups: Dict[tuple, List["ModelRelease"]] = {}
    for k in order:
        group = groups[k]
        if len(group) < _COLLAPSE_THRESHOLD:
            continue  # too small to collapse anyway — handled below
        escapes = [m for m in group if _is_escape(m, group)]
        remainder = [m for m in group if m not in escapes]
        escape_singletons.extend(escapes)
        if len(remainder) >= _COLLAPSE_THRESHOLD:
            collapsible_groups[k] = remainder
        else:
            # Escapes dropped the group below threshold — keep the remainder
            # as singletons too (don't collapse a pair).
            escape_singletons.extend(remainder)

    out: List["ModelRelease"] = []
    for k in order:
        group = collapsible_groups.get(k)
        if group is None:
            continue
        rep = group[0]
        suffixes = [_variant_suffix(g.name, _param_size_from_name(g.name)) or g.name
                    for g in group]
        suffixes_str = ", ".join(suffixes[:8]) + ("…" if len(suffixes) > 8 else "")
        size = _param_size_from_name(rep.name) or ""
        author = rep.name.split("/")[0] if "/" in rep.name else ""
        seg = rep.name.split("/")[-1]
        family_core = seg
        if size:
            size_pat = re.compile(re.escape(size).replace("B", r"[bB]").replace("M", r"[mM]"))
            mm = size_pat.search(seg)
            if mm:
                family_core = seg[:mm.end()]
        family_name = f"{author}/{family_core}" if author else family_core
        summary = (f"Family release — {len(group)} specialized variants of the "
                   f"{family_core} family ({size or 'unknown size'}): {suffixes_str}. "
                   f"Released as a batch.")
        collapsed = ModelRelease(
            name=family_name,
            provider=rep.provider,
            source=rep.source,
            url=rep.url,
            description=summary,
            total_parameters=size or rep.total_parameters,
            is_open_source=rep.is_open_source,
            license=rep.license,
            release_date=rep.release_date,
        )
        out.append(collapsed)

    # Singletons + pairs (groups below threshold) pass through unchanged.
    for k in order:
        if k in collapsible_groups:
            continue
        if k in groups and len(groups[k]) < _COLLAPSE_THRESHOLD:
            out.extend(groups[k])

    out.extend(escape_singletons)
    return out


def summarize_models(models: List[ModelRelease], web_context: str = "",
                     recent_names: List[str] = None, today: str = None) -> str:
    """Use LLM for concise digest if key available.

    web_context: cited Parallel.ai web research on recent releases (the inline
    path's freshness engine). recent_names: models already covered in recent
    digests, so the writer doesn't repeat them.
    """
    global LAST_SUMMARY_MODE, LAST_LLM_MODEL, LAST_STALE_DROPPED, LAST_LLM_FAILURE
    global LAST_STALE_DROPPED_NAMES, LAST_WRITER_N_WRITTEN, LAST_LINK_DROPPED
    LAST_SUMMARY_MODE = "template"
    LAST_LLM_MODEL = None
    LAST_STALE_DROPPED = 0
    LAST_STALE_DROPPED_NAMES = []
    LAST_LLM_FAILURE = None
    LAST_WRITER_N_WRITTEN = 0
    LAST_LINK_DROPPED = 0
    recent_names = recent_names or []
    models = [m for m in (models or []) if not is_openrouter_serving_sku(m.name)]
    if not models and not web_context:
        return NO_MODELS_SENTINEL
    models, validation_notes = prepare_models_for_digest(models)
    for note in validation_notes:
        print(f"Digest QA: {note}", file=sys.stderr)
    # URLs from models we then drop for age still count as "provided", so a
    # writer line that cites one is trimmed as stale rather than as a guessed link.
    link_models = list(models)
    models = _models_in_digest_window(models, today=today)
    if not models and not web_context:
        return NO_MODELS_SENTINEL

    seen = set()
    deduped = []
    for m in models:
        base = m.name.split("/")[-1].lower().replace(":free", "").replace("-latest", "")
        if base not in seen:
            seen.add(base)
            deduped.append(m)
    models = deduped[:12]

    info = []
    for m in models:
        tier = categorize_model(m)
        s = f"Name: {m.name} [{tier}]"
        if m.source:
            s += f" ({m.source})"
        if m.release_date:
            s += f"\nReleased: {m.release_date}"
        if m.description:
            s += f"\nDesc: {_smart_truncate(m.description, 200)}"
        if m.context_window:
            s += f"\nContext: {_format_context(m.context_window)}"
        if m.license:
            s += f"\nLicense: {m.license}"
        if m.total_parameters:
            s += f"\nTotal params: {m.total_parameters}"
        if m.active_parameters:
            s += f"\nActive params: {m.active_parameters}"
        if m.card_facts:
            s += f"\nBenchmarks (from model card): {m.card_facts}"
        s += f"\nConfidence: {m.confidence}"
        if m.validation_notes:
            s += f"\nUnknowns: {', '.join(m.validation_notes)}"
        if m.pricing_input is not None:
            s += f"\nPricing: {'FREE' if m.pricing_input == 0 else f'${m.pricing_input:.2f}/${m.pricing_output:.2f} per 1M'}"
        if m.canonical_url:
            s += f"\nCanonical URL: {m.canonical_url}"
        if m.url:
            s += f"\nObserved URL: {m.url}"
        info.append(s)

    web_block = ""
    if web_context:
        web_block = (
            "\nFRESH WEB RESEARCH (cited, recent) — THIS is your primary freshness "
            "source. Identify EVERY distinct genuinely-new model named across these "
            "sources (aim for breadth — frontier, open-weight, coding, multimodal, "
            "audio, local — typically 4-8 if the sources support it), one entry each, "
            "only models clearly released/updated in the last 3 days.\n"
            "Prefer a primary source (vendor page, Hugging Face, or OpenRouter). "
            "Do not treat a comparison listicle as a source. Never state a spec "
            "that is not in the lines below. The publisher attaches the link; "
            "do not invent a URL.\n"
            f"{web_context}\n")
    avoid_block = ""
    if recent_names:
        avoid_block = ("\nALREADY COVERED in recent digests — do NOT repeat unless there "
                       "is a NEW development (then say what changed):\n"
                       + ", ".join(recent_names[:60]) + "\n")

    prompt = f"""You are ModelBytes, an AI model tracker. Write ONLY the prose for a short Telegram digest. The publisher renders the metadata line (params, context, license, price, release date), the availability tag, the link label, and Take italics. You do not write those.

OUTPUT (no HTML, no links, no metadata line):
TAKE: <one sentence, or NONE>
ITEM: <clean display name of one candidate>
PROSE: <one sentence: why a builder should care. No specs.>

TAKE RULES:
- The Take line may mention only models you also list as items in this digest. If a claim does not map to one of those items, omit it. Do not cite a shelved, rumored, or unlisted launch.
- Do not put parameter counts, context lengths, licenses, prices, benchmark scores, or release dates in the Take or the PROSE. Those are appended from structured fields.
- Do not infer or invent parameter counts, license terms, benchmark numbers, or release dates beyond what is provided below.
- No vendor or lab attribution unless that name is in the candidate data for that item.
- Prefer a primary source (the vendor page, Hugging Face, or OpenRouter). Do not make an entry whose only source is a comparison listicle or news roundup. Skip a bare codename (a single name with no version or size) that appears only in a listicle.
- Omit the Take line (TAKE: NONE) when nothing ties two or more listed items together.
- Never write a release date older than 3 days. If the only date you have is older, omit that model entirely — do not mention it in the Take line either.

ITEM RULES:
- One ITEM per model. Name it the way people say it ("MiniMax M3", "Gemma 4 12B"), not the raw repo id. Drop an "org/" prefix, a leading "~", and format suffixes like "-it"/"-Instruct". Keep the version and size.
- SKIP: fine-tunes, ONNX, LoRA, GGUF, embedders, experiments, distilled, personal merges.
- Treat each model's Confidence and Unknowns as pre-publish QA. If a model is low confidence, skip it unless it is the only item.
- No filler verbs: explores, reveals, highlights, offering, showcases, demonstrates, unpacks, breaks down, dives into, worth watching, notable, gaining traction
- Do NOT write a totals/count line, tier headers, or links.
- Technical and direct, no hype
- Prefer genuinely-NEW models released or updated in the last 3 days.
{avoid_block}{web_block}
Candidate models from our fetchers (may be sparse or already-covered — the web research above is primary for freshness):
{chr(10).join(info) if info else "(none from fetchers today — build the digest from the web research above)"}"""

    if not LLM_API_KEY:
        print("No LLM key — falling back to template digest")
        return build_digest_message(models, today=today)

    # Try the primary model, then the fallback — so one model vanishing from
    # Ollama Cloud degrades to another model, not to the bare template.
    candidates = [LLM_MODEL] + [m for m in (LLM_MODEL_FALLBACK,) if m and m != LLM_MODEL]
    summary = None
    for model in candidates:
        print(f"Calling LLM ({model})...")
        out = _call_llm(model, prompt)
        if out:
            summary = out
            LAST_LLM_MODEL = model
            break
        print(f"LLM '{model}' produced nothing — trying next candidate.", file=sys.stderr)
    if not summary:
        print("All LLM candidates failed — falling back to template")
        return build_digest_message(models, today=today)

    # The model is unreliable at filling the count (it echoes the literal
    # "X"); strip any footer it emitted and append a deterministic one.
    summary = re.sub(r"(?im)^\s*(?:total:\s*)?[\dx]+\s+(?:models|items) tracked today\s*$", "", summary).rstrip()
    summary = re.sub(r"(?im)^\s*📊?\s*surfaced\b.*\bscanned\b.*today\s*$", "", summary).rstrip()
    if not summary:
        print("LLM body was only a footer — falling back to template")
        return build_digest_message(models, today=today)
    # How many entries the writer actually produced, before any scrub — so a
    # zero-survivor fallback can report whether entries existed and were stripped
    # or the writer never wrote one (the 2026-07-12 diagnosis needed both cases).
    n_written = _count_surfaced_models(summary)
    n_items = len(re.findall(r"(?im)^ITEM:\s*\S", summary))
    n_written = max(n_written, n_items)
    LAST_WRITER_N_WRITTEN = n_written
    # Hard guarantee: every published link is a URL we actually provided. Drops
    # entries whose <a href> the writer constructed/guessed (e.g. a plausible
    # but unverified huggingface.co/... link) rather than copying a source URL.
    summary, dropped = _strip_unverified_links(
        summary, _collect_provided_urls(link_models, web_context))
    LAST_LINK_DROPPED = dropped
    if dropped:
        print(f"Dropped {dropped} entr(y/ies) with unverified/constructed links.",
              file=sys.stderr)
    # Stale-date scrub: the writer can emit a release date older than the
    # freshness window (from an undated web source or its own knowledge). Drop
    # just those entries — the per-entry counterpart to the link scrub — so one
    # stale line trims a single entry instead of tripping the whole-body gate
    # and taking the digest dark (the 2026-07-04 incident). The gate remains the
    # backstop for a stale date outside an entry line. The window is the digest
    # news window (DIGEST_FRESHNESS_DAYS), not the 14-day catalog clock.
    summary, stale_names = _strip_stale_entries(summary, today=today)
    summary, more_stale = _apply_writer_format(summary, link_models, today=today)
    if more_stale:
        stale_names = list(stale_names) + list(more_stale)
    LAST_STALE_DROPPED = len(stale_names)
    LAST_STALE_DROPPED_NAMES = stale_names
    if stale_names:
        print(f"Dropped {len(stale_names)} entr(y/ies) with a stale release date: "
              f"{', '.join(stale_names)}.", file=sys.stderr)
    if not summary.strip() or not re.search(_ENTRY_GRAMMAR, summary):
        # Distinguish "writer wrote entries, verification stripped them all" from
        # "writer wrote none" — otherwise a no-post day is a forensic session
        # (the 2026-07-12 investigation). The breakdown makes it a one-glance read.
        print(f"No entries survived verification (writer produced {n_written} "
              f"entr(y/ies); link-scrub dropped {dropped}, stale-scrub dropped "
              f"{len(stale_names)}) — falling back to template", file=sys.stderr)
        return build_digest_message(models, today=today)
    header = f"🤖 <b>ModelBytes Digest</b>\n<i>{_digest_dateline(today)}</i>"
    # Honest footer: how many we actually surfaced vs how many we scanned.
    footer = f"📊 Surfaced {_count_surfaced_models(summary)} · scanned {len(models)} today"
    LAST_SUMMARY_MODE = "llm"
    return f"{header}\n\n{summary}\n\n{footer}"


TELEGRAM_MAX_CHARS = 4096
DIGEST_LIMIT = 15  # max models included in one daily digest


def _truncate_for_telegram(message: str, limit: int = TELEGRAM_MAX_CHARS) -> str:
    """Truncate at the last newline before Telegram's 4096-char limit, with a
    truncation marker. Delegates to the shared publish core (identical logic);
    kept as a module function so existing callers and tests are unchanged."""
    return _ss_truncate_for_telegram(message, limit)


# The message_id of the last successful channel post (t.me/ModelBytes/<id>) —
# the one durable proof of publication Telegram gives; recorded in publish_runs.
LAST_TELEGRAM_MESSAGE_ID = None


def send_telegram_post(message: str) -> bool:
    """Send one message to the @ModelBytes channel. Returns True on success.

    Delegates the HTTP mechanics (truncate, retry 429/5xx honoring Retry-After,
    fail-soft) to the shared publish core. Preserves the module-level
    LAST_TELEGRAM_MESSAGE_ID side-effect that callers (publish_runs audit)
    read after a successful send.
    """
    global LAST_TELEGRAM_MESSAGE_ID
    result = _publisher.send_telegram(message)
    if result.ok:
        LAST_TELEGRAM_MESSAGE_ID = result.message_id
        print(f"Sent ({len(message)} chars).", file=sys.stderr)
    else:
        LAST_TELEGRAM_MESSAGE_ID = None
        # Redact via the publisher's known secret_values (which may differ from
        # the module globals in tests) so a token in an error URL never leaks.
        from ss_publish import redact_secrets
        print(redact_secrets(f"Telegram send error: {result.error}",
                             _publisher.secret_values), file=sys.stderr)
    return result.ok


def _telegram_html_to_slack_mrkdwn(value: str) -> str:
    # Delegate the HTML→mrkdwn parse to the shared core (identical token handling:
    # b/strong→*, i/em→_, code/pre→`, a href→<url|label>, br→\n), then apply
    # ModelBytes' post-processing (per-line rstrip, collapse 3+ newlines, strip)
    # that the golden corpus expects.
    text = _ss_telegram_html_to_mrkdwn(value)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def send_slack_post(message: str) -> bool:
    """Mirror a published digest to Slack via chat.postMessage.

    No-op (returns False) unless both SLACK_BOT_TOKEN and
    MODELBYTES_SLACK_CHANNEL_ID are configured, so Telegram-only deploys are
    unaffected. Failures are logged but never abort the publish (Telegram is
    the primary channel).
    """
    if not SLACK_BOT_TOKEN or not MODELBYTES_SLACK_CHANNEL_ID:
        print("Slack not configured — skipping Slack mirror", file=sys.stderr)
        return False
    text = _telegram_html_to_slack_mrkdwn(message)
    try:
        resp = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={
                "channel": MODELBYTES_SLACK_CHANNEL_ID,
                "text": text[:39000],
                "unfurl_links": False,
                "unfurl_media": False,
            },
            timeout=30,
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"Slack error: {data.get('error', resp.text[:300])}", file=sys.stderr)
            return False
        print("Mirrored digest to Slack.", file=sys.stderr)
        return True
    except Exception as e:
        print(f"Slack send error: {e}", file=sys.stderr)
        return False


PENDING_RAW_BASE = os.environ.get(
    "MODELBYTES_PENDING_RAW_BASE",
    "https://raw.githubusercontent.com/SovereignSignal/modelbytes/master/pending",
)


# Default 0: there is no curator to wait for. A positive value still polls
# GitHub for a late hand-written pending/<date>.txt, but INLINE_PRIMARY days
# skip the wait regardless (see _wait_for_pending).
PENDING_GRACE_SECONDS = int(os.environ.get("MODELBYTES_PENDING_GRACE_SECONDS", "0"))
PENDING_POLL_INTERVAL = int(os.environ.get("MODELBYTES_PENDING_POLL_SECONDS", "120"))


def _fetch_pending_from_github(today: str, attempts: int = 3) -> Optional[str]:
    """Fetch today's curated pending file straight from GitHub raw.

    The Railway image only contains the pending file if a deploy happened
    AFTER the curator's ~15:45 UTC push — a race the 2026-06-11 publish lost
    (stale 14:19 image → bare template went out despite a good curated digest
    sitting on master). Master is therefore the source of truth and this fetch
    runs FIRST; the baked-in local copy is only a fallback for GitHub outages.
    Retries transient failures and cache-busts (raw.githubusercontent caches
    both content and 404s for ~5 minutes). Returns None when absent/unreachable.
    """
    url = f"{PENDING_RAW_BASE}/{today}.txt"
    for attempt in range(1, attempts + 1):
        try:
            # The unique query param is the cache-buster (raw.githubusercontent
            # keys its CDN cache on the URL and caches 404s ~5 min).
            resp = requests.get(url, timeout=20,
                                params={"nocache": str(int(time.time()))},
                                headers={"User-Agent": HTTP_USER_AGENT})
            if resp.status_code == 200 and resp.text.strip():
                print(f"Fetched curated digest from GitHub raw ({url}).")
                return resp.text
            if resp.status_code == 404:
                return None
            print(f"GitHub raw pending fetch: HTTP {resp.status_code} "
                  f"(attempt {attempt}/{attempts})", file=sys.stderr)
        except Exception as e:
            print(_redact_secrets(f"GitHub raw pending fetch failed "
                                  f"(attempt {attempt}/{attempts}): {e}"), file=sys.stderr)
        if attempt < attempts:
            time.sleep(2 * attempt)
    return None


def _wait_for_pending(today: str) -> Optional[str]:
    """Optional wait for a late hand-written pending/<date>.txt.

    The claude.ai curator this existed for is retired (supervisor paused
    2026-08-22). INLINE_PRIMARY days skip the wait: a missing pending file is
    the normal path, not a 10-minute hang plus ops alert. A positive
    MODELBYTES_PENDING_GRACE_SECONDS still polls when INLINE_PRIMARY is off.
    """
    if INLINE_PRIMARY or PENDING_GRACE_SECONDS <= 0:
        return None
    # Tell the operator at the START of the wait, not after it — a late curator
    # is itself a signal worth seeing in real time.
    send_ops_alert(f"Curated digest for {today} not on master at publish time — "
                   f"entering {PENDING_GRACE_SECONDS}s grace window before "
                   "falling back.")
    deadline = time.monotonic() + PENDING_GRACE_SECONDS
    while time.monotonic() < deadline:
        wait = min(PENDING_POLL_INTERVAL, max(1, deadline - time.monotonic()))
        print(f"No curated digest yet — waiting {int(wait)}s for the curator "
              f"(grace window).")
        time.sleep(wait)
        # Single attempt per poll: the outer loop IS the retry; nesting the
        # 3-attempt inner loop here would burn the window on a GitHub outage.
        body = _fetch_pending_from_github(today, attempts=1)
        if body:
            return body
    return None


_DATELINE_RE = re.compile(r"<i>[A-Z][a-z]+day, [A-Z][a-z]+ \d{1,2}, \d{4}</i>")


def _fix_dateline(body: str, today: str = None) -> str:
    """Rewrite the digest's dateline to the actual UTC date. The curator wrote
    the wrong weekday 2 of its first 3 v3 days ('Wednesday, June 11' for a
    Thursday); the publisher knows the real date deterministically. If the
    dateline format ever drifts so this can't match, the linter emits a
    'no parseable dateline' warning rather than failing silently."""
    ref = (datetime.strptime(today, "%Y-%m-%d") if today
           else datetime.now(timezone.utc))
    correct = f"<i>{ref.strftime('%A, %B')} {ref.day:02d}, {ref.year}</i>"
    fixed, n = _DATELINE_RE.subn(correct, body, count=1)
    if fixed != body:
        print("Corrected digest dateline to actual UTC date.")
    return fixed


def _forward_published_releases(models, message: str, today: str,
                                qa_errors=None) -> None:
    """Hand a published digest to the release-event seam.

    Called only after validate_digest_for_publish returned no errors and
    send_telegram_post succeeded. Preview never reaches this. Failures are
    logged and swallowed so a receiver outage cannot dark the channel.
    The curated fast-path has no ModelRelease rows; it calls this with an
    empty list so the seam exists on that branch without inventing events.
    """
    try:
        release_forwarding.forward_release_events(
            models, message, preview=False, today=today, qa_errors=qa_errors,
        )
    except Exception as exc:
        print(f"[WARN] release forwarding failed: {type(exc).__name__}",
              file=sys.stderr)


def try_post_pending_curated() -> bool:
    """Fast-path: post a pre-curated digest written by the curator routine.

    The modelbytes-curator-routine writes pending/<TODAY>.txt to master
    ~30 minutes before this cron fires. If today's post is already recorded,
    return True. If we find a pending file, post it verbatim, record the date,
    and return True. Otherwise return False so main() falls through to the
    existing deterministic pipeline.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    init_posted_digest_store()
    if has_posted_digest(today):
        print(f"Digest for {today} already marked posted -- skipping.")
        return True

    pending_path = Path("pending") / f"{today}.txt"

    # Master is the source of truth: GitHub raw FIRST (the curator pushes
    # there and Railway images are stale by construction — auto-deploy does
    # not fire on curator pushes). The baked-in local copy is the fallback
    # for GitHub outages, and a grace window covers a late-running curator.
    body = _fetch_pending_from_github(today)
    if body is None and pending_path.exists():
        local = pending_path.read_text().strip()
        if local:
            print(f"GitHub raw unavailable — using baked-in {pending_path}.")
            body = local
    if body is None:
        body = _wait_for_pending(today)
    if body is None:
        return False
    body = body.strip()  # both sources pre-check non-empty; strip is hygiene

    body = _fix_dateline(body, today)
    body, qa_warnings, qa_errors = validate_digest_for_publish(body)
    for warning in qa_warnings:
        print(f"Digest QA warning ({pending_path}): {warning}", file=sys.stderr)
    # Alert only on warning classes that mean content damage (fact drift,
    # floods, leaks, post-expiry claims) — format-drift warnings alone would
    # ping the operator daily and train them to ignore the channel.
    alert_worthy = [w for w in qa_warnings
                    if any(k in w for k in ("fact drift", "flood", "quant",
                                            "stale release", "expiry"))]
    if alert_worthy:
        send_ops_alert("Curated digest QA (published anyway): "
                       + "; ".join(alert_worthy[:6]))
    if qa_errors:
        print(
            f"Pending curated digest failed pre-publish QA: {'; '.join(qa_errors)}",
            file=sys.stderr,
        )
        send_ops_alert(f"Curated digest for {today} BLOCKED by QA "
                       f"({'; '.join(qa_errors[:4])}) — falling back to pipeline.")
        record_publish_run(today, "curated", "blocked",
                           message_chars=len(body), error="; ".join(qa_errors))
        return False

    print(f"Pending curated digest found for {today} ({len(body)} chars). Posting.")
    if not send_telegram_post(body):
        print("Telegram send of curated digest failed — falling back to pipeline.",
              file=sys.stderr)
        send_ops_alert(f"Telegram send of curated digest for {today} FAILED — "
                       "trying fallback pipeline.")
        record_publish_run(today, "curated", "send-failed", message_chars=len(body),
                           error="telegram send failed")
        return False

    # No structured candidates on this branch — do not invent events from HTML.
    _forward_published_releases([], body, today, qa_errors)
    mark_posted_digest(today, "curated", str(pending_path), body)
    # Keep the local file in sync with what was actually published (the body
    # usually came from GitHub raw, fresher than the baked-in copy) so anything
    # reading pending/<today>.txt — including tomorrow's fact-consistency
    # check — sees what readers saw.
    try:
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(body, encoding="utf-8")
    except OSError as exc:
        print(f"Could not record published digest to {pending_path}: {exc}",
              file=sys.stderr)
    slack_ok = send_slack_post(body)  # mirror to Slack (no-op unless configured)
    if not slack_ok and SLACK_BOT_TOKEN and MODELBYTES_SLACK_CHANNEL_ID:
        send_ops_alert(f"Slack mirror failed for {today} (Telegram posted fine).")
    record_publish_run(today, "curated", "posted", message_chars=len(body),
                       telegram_message_id=LAST_TELEGRAM_MESSAGE_ID,
                       slack_ok=slack_ok,
                       error=("warnings: " + "; ".join(qa_warnings[:8]))
                             if qa_warnings else None)
    ping_heartbeat(True, f"curated posted for {today}")
    print(f"Posted curated digest for {today}.")
    return True


def _record_no_publishable(today: str, mode: str, models_found: int,
                           message: str = "") -> None:
    """Record a no-post day. Alert only when the writer produced entries that
    verification then stripped — a catalog-quiet / writer-0 day is expected
    (2026-08-24) and must not 🚨."""
    n_written = LAST_WRITER_N_WRITTEN
    n_link = LAST_LINK_DROPPED
    n_stale = LAST_STALE_DROPPED
    stripped = n_written > 0 and (n_link > 0 or n_stale > 0)
    err = None
    if n_written or n_link or n_stale:
        err = (f"writer produced {n_written}; link-scrub dropped {n_link}, "
               f"stale-scrub dropped {n_stale}")
    record_publish_run(today, mode, "no-models",
                       models_found=models_found, models_emitted=0,
                       message_chars=len(message or ""),
                       error=err)
    ping_heartbeat(True, "no publishable entries" if stripped else "no models")
    if stripped:
        send_ops_alert(
            f"No post today ({today}): writer produced {n_written} "
            f"entr(y/ies); link-scrub dropped {n_link}, stale-scrub dropped "
            f"{n_stale}. Nothing survived verification. Nothing posted.")


def main():
    preview_mode = "--preview" in sys.argv
    if preview_mode:
        sys.argv.remove("--preview")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    live_mode = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID) and not preview_mode

    # Per-run diagnostics. summarize_models overwrites these; reset here so a
    # no-summarize quiet day cannot inherit counts from a previous call in
    # the same process (the test suite).
    global LAST_WRITER_N_WRITTEN, LAST_LINK_DROPPED, LAST_STALE_DROPPED
    global LAST_DISCOVERY_MODELS, LAST_LLM_MODEL, LAST_SUMMARY_MODE
    global _DISCOVERY_DISABLED
    LAST_WRITER_N_WRITTEN = 0
    LAST_LINK_DROPPED = 0
    LAST_STALE_DROPPED = 0
    LAST_DISCOVERY_MODELS = []
    LAST_LLM_MODEL = None
    LAST_SUMMARY_MODE = "template"
    _DISCOVERY_DISABLED = False
    _SOURCE_ERRORS.clear()

    # Retry queued release events before every early return (already-posted
    # curated day, seed, quiet day, no-models). Preview and a disabled flag
    # do neither HTTP nor outbox writes. This does not scan historical
    # digests — only rows an earlier armed run left pending.
    if not preview_mode:
        try:
            if release_forwarding.forwarding_enabled():
                release_forwarding.retry_pending_release_events()
            else:
                print("release forwarding: disabled", file=sys.stderr)
        except Exception as exc:
            print(f"[WARN] release outbox retry failed: {type(exc).__name__}",
                  file=sys.stderr)

    if live_mode and not DATABASE_URL:
        # Degraded but not fatal yet: the curated fast-path doesn't need the DB
        # (ledger writes are best-effort no-ops). Alert and still try it —
        # blocking a good curated digest over a lost env var would be worse.
        send_ops_alert("DATABASE_URL missing in live mode — idempotency ledger "
                       "and dedupe are OFF. Curated publish will still be "
                       "attempted; the fallback pipeline cannot run safely.")

    # Fast-path: post a pre-curated digest from the curator routine if one exists.
    # Falls through to the deterministic pipeline if no pending file or send fails.
    if not preview_mode and try_post_pending_curated():
        return 0

    # A lost DATABASE_URL must be loud, not a silent skipped day: without it,
    # load_seen_models() returns empty → every fallback day re-detects "first
    # run" and posts nothing, forever, with exit 0 (design-pass finding). The
    # fallback pipeline genuinely needs the DB, so only it is gated.
    if live_mode and not DATABASE_URL:
        print("FATAL: DATABASE_URL is not set — the fallback pipeline cannot "
              "dedupe and would silently skip every day.", file=sys.stderr)
        ping_heartbeat(False, "DATABASE_URL missing, no curated digest")
        return 1

    init_database()
    seen_models = load_seen_models()
    is_first_run = len(seen_models) == 0

    print(f"Checking {today}... Tracking {len(seen_models)} models")

    all_new = []
    queued_names = set()
    fetched_sources = []
    for source_name, fetcher in [
        ("OpenRouter", fetch_openrouter_models),
        ("Ollama", fetch_ollama_models),
        ("HuggingFace-Trending", fetch_huggingface_trending),
        ("HuggingFace-Orgs", fetch_major_orgs),
        ("HuggingFace-Top-TextGen", fetch_hf_text_generation),
    ]:
        print(f"Fetching {source_name}...")
        batch = list(fetcher() or [])
        fetched_sources.append(
            (source_name, batch, _consume_source_error(source_name)))
        for model in batch:
            if model.name in seen_models or model.name in queued_names:
                continue
            all_new.append(model)
            queued_names.add(model.name)
            # Don't add to seen_models yet — noise models should be
            # re-evaluated next run with updated engagement data.
            # Only posted/significant models get added later.
    flush_source_health(
        [{"source": name, "items": len(batch), "error": err}
         for name, batch, err in fetched_sources],
        preview=preview_mode,
    )

    # Commit cluster before the stale drop: a repo created before launch
    # (Kumo, AstaBrief) keeps lastModified here, then the cluster date when
    # the commits say the public release is inside the window.
    all_new = refine_stale_repo_dates(all_new, today)
    all_new = drop_stale_models(all_new, seen_models, today)

    serving = [m for m in all_new if is_openrouter_serving_sku(m.name)]
    if serving:
        print(f"Dropping {len(serving)} OpenRouter serving SKU(s): "
              + ", ".join(m.name for m in serving[:5])
              + ("…" if len(serving) > 5 else ""))
        # Same as stale: mark seen so a fetcher leak cannot retry daily.
        for m in serving:
            log_dropped_candidate("filter", m.name, "openrouter serving sku")
            seen_models.add(m.name)
        all_new = [m for m in all_new if not is_openrouter_serving_sku(m.name)]

    print(f"Found {len(all_new)} new model(s)")

    if is_first_run and not preview_mode:
        # An empty models table means a true first run OR wiped/migrated state.
        # Seeding silently on wiped state would skip the day (and every future
        # fallback day) with exit 0 — require an explicit opt-in.
        if not ALLOW_SEED:
            print("State looks reset (0 seen models) — refusing to silently "
                  "seed. Set MODELBYTES_ALLOW_SEED=1 for a genuine first run.",
                  file=sys.stderr)
            send_ops_alert("Models table is EMPTY — looks like wiped/migrated "
                           "state, not a quiet day. Refusing to seed silently; "
                           "set MODELBYTES_ALLOW_SEED=1 if this is intentional.")
            record_publish_run(today, "fallback", "blocked",
                               models_found=len(all_new),
                               error="empty models table without MODELBYTES_ALLOW_SEED")
            # Deterministic block: a retry will hit the identical missing-env
            # state and re-alert. Exit 0 so Railway doesn't mark the job
            # Crashed and re-run it 3× (re-firing this alert each time). The
            # blocked publish_run row + the ops alert are the complete record;
            # ping_heartbeat(/fail) still flags it for attention.
            ping_heartbeat(False, "empty state, seed not allowed")
            emit_writer_health(0, 0)
            return 0
        print("First run — seeding, no digest sent")
        # Seed all current models so they won't be reported as "new" next time
        for m in all_new:
            seen_models.add(m.name)
        save_seen_models(seen_models)
        record_publish_run(today, "fallback", "seeded", models_found=len(all_new))
        ping_heartbeat(True, "seeded")
        emit_writer_health(0, 0)
        return 0

    # Models passed the fetcher-level is_noise_model checks already; the prior
    # second pass here passed `m.provider` (display name like "Alibaba") as the
    # author arg, which never matches `KNOWN_ORGS` slugs like "qwen". That made
    # orgs with diverging display names (tencentarc/"Tencent ARC", allenai/"AI2")
    # fall into the unknown-org engagement gate and get filtered as noise.
    # Removing the broken pass; the fetcher-level filter is sufficient. (audit A11)
    #
    # Web discovery (Parallel.ai) is the inline freshness engine: it runs even
    # when the fetchers find 0 new models (the dedup-drained dark-channel case),
    # so the channel stays fresh without the claude.ai curator. Hits that name
    # a primary URL are promoted to ModelRelease objects and merged into the
    # candidate set so a writer-0 day can still post via the template
    # (2026-08-24: 9 web sources, 0 catalog models, writer produced 0 entries).
    web_context = discover_recent_releases(
        today, max_age_days=DIGEST_FRESHNESS_DAYS)
    # Lab feeds run even when Parallel is off. "disabled" is only the empty
    # no-key case; a feed that actually returned items is a live source.
    discovery_has_items = bool((web_context or "").strip() or LAST_DISCOVERY_MODELS)
    if _DISCOVERY_DISABLED and not discovery_has_items:
        discovery_error = _consume_source_error("Discovery")
        if discovery_error:
            flush_source_health(
                [{"source": "Discovery", "items": 0, "error": discovery_error}],
                preview=preview_mode,
            )
        else:
            flush_source_health(
                [{"source": "Discovery", "items": 0, "error": "disabled", "track": False}],
                preview=preview_mode,
            )
    else:
        discovery_error = _consume_source_error("Discovery")
        discovery_items = len(LAST_DISCOVERY_MODELS)
        if discovery_items == 0 and (web_context or "").strip() and not discovery_error:
            discovery_items = 1
        flush_source_health(
            [{"source": "Discovery", "items": discovery_items, "error": discovery_error}],
            preview=preview_mode,
        )
    recent_names = _recent_digest_names(today)
    n_disc = 0
    existing = {m.name for m in all_new}
    for m in _filter_discovery_models(
            LAST_DISCOVERY_MODELS, recent_names=recent_names,
            seen=seen_models, today=today,
            max_age_days=DIGEST_FRESHNESS_DAYS):
        if m.name in existing or m.name in seen_models:
            continue
        all_new.append(m)
        existing.add(m.name)
        n_disc += 1
    if n_disc:
        print(f"Added {n_disc} web-discovery candidate(s); "
              f"{len(all_new)} new model(s) total")

    if all_new or web_context:
        def _author(m):
            return m.name.split("/")[0].lower() if "/" in m.name else ""

        def _significant(m):
            return is_significant_release(
                m.name, _author(m), m.unique_traits or [], m.downloads or 0
            )

        # Rank by significance, then engagement, so the daily cap keeps the
        # most important releases rather than whichever source was fetched first.
        ranked = sorted(
            all_new,
            key=lambda m: (1 if _significant(m) else 0, m.downloads or 0, m.likes or 0),
            reverse=True,
        )
        ranked = _models_in_digest_window(ranked, today=today)
        digest_models = ranked[:DIGEST_LIMIT]
        held = ranked[DIGEST_LIMIT:]
        # Mark the pre-collapse set seen FIRST: collapsed-away variants must be
        # recorded as seen or they re-surface next run (the family entry's name
        # is the base, not the individual variants). Done before collapse so
        # the original variant names are captured.
        pre_collapse = list(digest_models)
        # Collapse (inline path only, 2026-06-22 spec): group same-(org, base,
        # size) variants and collapse N≥3 into one family entry so one org's
        # batch doesn't burn the daily cap or spam the channel. Sits after
        # ranking (keeps the top variants) and before summarize (the collapsed
        # entry is a plain ModelRelease the renderer consumes).
        digest_models = collapse_variants(digest_models)
        print(
            f"Posting top {len(digest_models)} of {len(all_new)} new model(s)"
            + (f"; {len(held)} held for a later run" if held else "")
        )

        # Defer seen-marking until a successful post. Marking before summarize
        # burned unposted models on no-post days (they never resurfaced).
        posted_names = {m.name for m in pre_collapse} | {m.name for m in digest_models}
        noise_overflow = {m.name for m in held if not _significant(m)}

        # Enrich the top candidates with real HF-card facts (params, license,
        # context, benchmarks) so the inline model writes from specs, not just
        # its training knowledge — closes the research gap vs the old curator.
        if ENRICH_HF_CARDS:
            enrich_with_hf_cards(digest_models)

        message = summarize_models(digest_models, web_context, recent_names)
        log_writer_exclusions(digest_models, message or "")
        included = 0
        if message and message.strip() != NO_MODELS_SENTINEL:
            included = _count_surfaced_models(message)
        emit_writer_health(len(digest_models), included)
        fallback_mode = f"fallback-{LAST_SUMMARY_MODE}"

        # The pipeline degrades to the bare NO_MODELS_SENTINEL when there is
        # nothing to render (0 candidates, or the writer emitted no entries
        # and the template also had nothing). That sentinel is not a digest —
        # posting it dumps "No new models today." on the public channel
        # (2026-07-04 backfill incident). Never post it.
        if message.strip() == NO_MODELS_SENTINEL:
            print("Pipeline produced no publishable entries — treating as no-post.")
            if preview_mode:
                print("Preview mode — not sending (no publishable entries)")
                return 0
            _record_no_publishable(today, fallback_mode, len(all_new), message)
            save_seen_models(seen_models)
            return 0

        message, qa_warnings, qa_errors = validate_digest_for_publish(
            message, mode="fallback")
        for warning in qa_warnings:
            print(f"Digest QA warning (fallback): {warning}", file=sys.stderr)

        # Preview must be fully side-effect-free: no alerts, no DB writes, no
        # heartbeat, no send. This MUST come before the qa-error/alert path —
        # otherwise a preview run DMs the operator a false "NO POST" alert
        # (exactly what happened 2026-06-13 while validating a model).
        if preview_mode:
            print("=== PREVIEW ===")
            print(message)
            print(f"=== END ({len(message)} chars) ===")
            if qa_errors:
                print(f"[preview] would BLOCK on QA errors: {'; '.join(qa_errors)}")
            print("Preview mode — not sending (no alerts, no DB writes)")
            return 0

        if qa_errors:
            print(
                f"Fallback digest failed pre-publish QA: {'; '.join(qa_errors)}",
                file=sys.stderr,
            )
            send_ops_alert(f"NO POST today ({today}): fallback digest blocked "
                           f"by QA — {'; '.join(qa_errors[:4])}")
            record_publish_run(today, fallback_mode, "blocked",
                               models_found=len(all_new),
                               models_emitted=len(digest_models),
                               message_chars=len(message),
                               error="; ".join(qa_errors))
            # A QA block is a correct, FINAL decision (e.g. refusing to post a
            # 17-day-old model as 'new today'). The content won't change on
            # retry — the fetcher will surface the same model, the gate will
            # trip the same way — so exit 0, not 1. Exit 1 made Railway mark
            # the job Crashed and re-run it 3×, re-firing this exact alert each
            # time (the incident on 2026-06-19). The blocked publish_run row +
            # the ops alert are the complete record; ping_heartbeat(/fail) is
            # the correct 'needs attention' signal and does not trigger a
            # Railway crash-restart.
            ping_heartbeat(False, "fallback blocked by QA")
            return 0

        if not send_telegram_post(message):
            send_ops_alert(f"NO POST today ({today}): Telegram send failed on "
                           "the fallback path. Check token (died twice before) "
                           "and Railway logs.")
            record_publish_run(today, fallback_mode, "send-failed",
                               models_found=len(all_new),
                               models_emitted=len(digest_models),
                               message_chars=len(message),
                               error="telegram send failed")
            ping_heartbeat(False, "fallback telegram send failed")
            return 1
        # After QA passed and Telegram accepted the post. Surfaced-name
        # filtering happens inside the seam; this call does not run on the
        # preview, QA-block, or send-failure returns above.
        _forward_published_releases(digest_models, message, today, qa_errors)
        # Record what we published so the Slack review report (which reads
        # pending/<today>.txt) reflects the latest digest instead of a stale file.
        # Safe re-post-wise: the posted_digests ledger short-circuits any rerun.
        pending_path = Path("pending") / f"{today}.txt"
        try:
            pending_path.write_text(message, encoding="utf-8")
        except OSError as exc:
            print(f"Could not record published digest to {pending_path}: {exc}", file=sys.stderr)
        mark_posted_digest(today, "fallback", str(pending_path), message)
        slack_ok = send_slack_post(message)  # mirror to Slack (no-op unless configured)
        for n in posted_names | noise_overflow:
            seen_models.add(n)
        record_publish_run(today, fallback_mode, "posted",
                           models_found=len(all_new),
                           models_emitted=len(digest_models),
                           message_chars=len(message),
                           telegram_message_id=LAST_TELEGRAM_MESSAGE_ID,
                           slack_ok=slack_ok)
        # Model-availability signal (fires regardless of INLINE_PRIMARY): if the
        # primary LLM was unavailable and we published with the fallback model,
        # the operator should know so they can update MODELBYTES_LLM_MODEL.
        if LAST_LLM_MODEL and LAST_LLM_MODEL != LLM_MODEL:
            send_ops_alert(_primary_missed_alert(today))
        # Content-drift signal (non-blocking): the writer leaked a stale release
        # date and the per-entry scrub trimmed it, so the digest shipped without
        # tripping the whole-body gate (the 2026-07-04 dark-channel incident).
        # Worth knowing — a recurring trim points at a bad web source or model
        # drift — but never a reason to block the post.
        if LAST_STALE_DROPPED:
            who = (": " + ", ".join(LAST_STALE_DROPPED_NAMES)
                   if LAST_STALE_DROPPED_NAMES else "")
            send_ops_alert(f"Trimmed {LAST_STALE_DROPPED} stale-dated "
                           f"entr{'y' if LAST_STALE_DROPPED == 1 else 'ies'} from "
                           f"today's digest ({today}){who} before publishing — the "
                           "writer emitted a release date outside the freshness "
                           "window. Published the rest.")
        if not INLINE_PRIMARY:
            # Curator still expected → a fallback day is an exception worth flagging.
            streak = fallback_streak()
            send_ops_alert(f"Published via FALLBACK ({LAST_SUMMARY_MODE}) for {today} "
                           f"— curated pending file was absent. "
                           f"Fallback streak: {streak} day(s). "
                           "Check the curator routine if this persists.")
        ping_heartbeat(True, f"{'inline' if INLINE_PRIMARY else 'fallback'} "
                             f"({LAST_SUMMARY_MODE}) posted for {today}")
        print("Digest sent")

    else:
        emit_writer_health(0, 0)
        print("No new models")
        if not preview_mode:
            _record_no_publishable(today, "fallback", 0)

    save_seen_models(seen_models)
    return 0


def _crash_site(exc: BaseException) -> str:
    """Last monitor.py frame, else the last frame of any file. No source text
    (could contain secrets)."""
    tb = getattr(exc, "__traceback__", None)
    if tb is None:
        return ""
    frames = traceback.extract_tb(tb)
    if not frames:
        return ""
    chosen = frames[-1]
    for fr in reversed(frames):
        if Path(fr.filename).name == "monitor.py":
            chosen = fr
            break
    return f"{Path(chosen.filename).name}:{chosen.lineno} in {chosen.name}"


def _crash_where() -> str:
    """Compact 'where is this process' line. Replica id is set on a live
    Railway container and typically absent for `railway run` / laptops."""
    parts = [f"host={socket.gethostname()}"]
    env = (os.environ.get("RAILWAY_ENVIRONMENT_NAME")
           or os.environ.get("RAILWAY_ENVIRONMENT") or "")
    svc = os.environ.get("RAILWAY_SERVICE_NAME") or ""
    loc = "/".join(p for p in (env, svc) if p)
    if loc:
        parts.append(f"railway={loc}")
    dep = os.environ.get("RAILWAY_DEPLOYMENT_ID") or ""
    if dep:
        parts.append(f"deploy={dep[:8]}")
    replica = os.environ.get("RAILWAY_REPLICA_ID") or ""
    if replica:
        parts.append("via=replica")
    elif env:
        parts.append("via=railway-run-or-local")
    return " ".join(parts)


def _crash_hint(exc: BaseException) -> str:
    if _is_private_host_unreachable(exc):
        return ("*.railway.internal DNS failed — this process is off Railway's "
                "private network (laptop / Cloud VM `railway run`), not the "
                "16:00 cron. DATABASE_PUBLIC_URL is the fallback.")
    return ""


def _crash_ledger_note(today: str) -> str:
    """Best-effort: did today already ship? Must not raise (DB may be why we crashed)."""
    try:
        if has_posted_digest(today):
            return f"ledger: {today} already posted (channel shipped)"
        return f"ledger: {today} NOT posted — channel is dark"
    except Exception:
        return "ledger: could not check posted_digests"


def _argv_summary(argv) -> str:
    parts = []
    for a in argv or []:
        parts.append(a if a.startswith("-") else Path(a).name)
    return " ".join(parts) or "monitor.py"


def _format_crash_alert(exc: BaseException, argv=None, now=None) -> str:
    """Operator-facing crash body: error + site + where + hint + ledger.

    2026-08-13: repr(exc) alone made an off-network DNS failure look like the
    cron dying. Keep this short enough for a Telegram DM; no source lines,
    no env dumps (secrets).
    """
    now = now or datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    err = _redact_secrets(f"{type(exc).__name__}: {exc}").strip()
    if len(err) > 400:
        err = err[:400] + "…"
    lines = [
        f"Publisher CRASHED — {now.strftime('%Y-%m-%d %H:%M')} UTC",
        err,
    ]
    site = _crash_site(exc)
    if site:
        lines.append(f"at: {site}")
    lines.append(f"run: {_argv_summary(argv)}")
    lines.append(f"where: {_crash_where()}")
    hint = _crash_hint(exc)
    if hint:
        lines.append(f"hint: {hint}")
    lines.append(_crash_ledger_note(today))
    return "\n".join(lines)


def _handle_crash(exc: BaseException, preview: bool, argv=None) -> None:
    """Ops-alert + heartbeat on a live crash. Preview is side-effect-free.

    2026-08-13: a `railway run … --preview` off the private network crashed
    in init_database() (postgres.railway.internal DNS). The __main__ handler
    still DMed the operator — same class as the 2026-06-13 false NO POST
    alert. Capture `--preview` *before* main() strips it from argv.
    """
    body = _format_crash_alert(exc, argv=argv)
    if preview:
        print(f"Preview crashed:\n{body}", file=sys.stderr)
        return
    send_ops_alert(body)
    ping_heartbeat(False, f"crash: {_redact_secrets(repr(exc))}")
    # Audit row is best-effort — the DB may be why we crashed.
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        record_publish_run(today, "crash", "crashed",
                           error=_redact_secrets(f"{type(exc).__name__}: {exc}")[:500])
    except Exception:
        pass


if __name__ == "__main__":
    _preview = "--preview" in sys.argv
    _argv = list(sys.argv)
    try:
        _rc = main()
    except Exception as _e:
        # A crash anywhere must still reach the operator and the dead-man's
        # switch — Railway only records the exit code. Preview is the
        # exception: it must never page (see _handle_crash).
        _handle_crash(_e, preview=_preview, argv=_argv)
        raise
    sys.exit(_rc)

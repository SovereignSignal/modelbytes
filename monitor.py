#!/usr/bin/env python3
"""Monitor AI model releases from OpenRouter, Ollama, and Hugging Face.

Posts new model releases to Telegram @modelbytes channel with tiered, LLM-summarized digest.
"""

import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Optional, Set, Tuple

from ss_publish import (
    Publisher,
    TelegramResult,
    telegram_html_to_mrkdwn as _ss_telegram_html_to_mrkdwn,
    truncate_for_telegram as _ss_truncate_for_telegram,
)

import psycopg2
import requests

# Database — Postgres is the only state backend.
# When DATABASE_URL is unset (local dev, --preview mode), state functions
# degrade gracefully: load returns empty set, save is a no-op.
DATABASE_URL = os.environ.get("DATABASE_URL", "")

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


def init_database():
    """Create the models table if it doesn't exist. No-op without DATABASE_URL."""
    if not DATABASE_URL:
        return
    conn = psycopg2.connect(DATABASE_URL)
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
            posted_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)


def init_posted_digest_store() -> bool:
    """Create the post-idempotency table. Failure should not block posting."""
    if not DATABASE_URL:
        return False
    try:
        conn = psycopg2.connect(DATABASE_URL)
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
        conn = psycopg2.connect(DATABASE_URL)
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
        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO posted_digests
                        (post_date, source, digest_path, message_hash)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (post_date) DO NOTHING
                    """,
                    (date_str, source, digest_path, message_hash),
                )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        print(f"Could not mark digest posted for {date_str}: {e}", file=sys.stderr)
        return False


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
    secret_values=tuple(s for s in (TELEGRAM_BOT_TOKEN, SLACK_BOT_TOKEN, DATABASE_URL) if s),
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
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
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
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
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
    conn = psycopg2.connect(DATABASE_URL)
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
    conn = psycopg2.connect(DATABASE_URL)
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
)
_QUANT_NAME_RE = re.compile(r"(?i)\b(gguf|awq|gptq|onnx|imatrix|exl2)\b|-bnb-")
# An entry is a line-leading bold name — with a dash tail (curated/LLM grammar)
# or bare (deterministic template puts specs on following lines). Tier headers
# ("━━━ <b>…") and the 🤖 header line don't start with <b> so they don't match.
_ENTRY_RE = re.compile(r"^(?:• )?<b>([^<]+)</b>\s*(?:[—-]|$)", re.MULTILINE)
_HREF_RE = re.compile(r'<a href="([^"]*)"')
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
            # Telegram renders http:// fine — not channel-harm, so it only
            # blocks machine-assembled content; curated gets a warning.
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
        if is_stale_release(date_str):
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
    we published (last 14 files, today's own file excluded) without an explicit
    correction marker. Would have caught MiniMax M3 going from 229.9B/9.8B
    (Jun 9, 11) to ~428B/23B (Jun 12) silently.

    Limitation (accepted): history comes from the image's pending/ dir, which
    can lag master by up to a day; the supervisor's daily auto-commits keep
    deploys frequent enough in practice."""
    pending_dir = pending_dir or Path("pending")
    if not pending_dir.is_dir():
        return []
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_claims, today_blocks = _extract_fact_claims(body)
    if not today_claims:
        return []
    warnings = []
    history = [p for p in sorted(pending_dir.glob("*.txt"), reverse=True)
               if p.stem != today][:14]
    resolved = set()  # (name, kind) pairs already judged against the most recent prior
    for path in history:
        try:
            prior, _ = _extract_fact_claims(path.read_text())
        except OSError:
            continue
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
                            f"vs {prior_v} in {path.stem} — mark corrections explicitly")
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


def is_noise_model(model_id: str, author: str, tags: list,
                   downloads: int = 0, likes: int = 0) -> bool:
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
    
    # GGUF is a quant repackage, never a primary "new model" for this digest —
    # always noise, even from known orgs (unsloth/bartowski's whole output is
    # GGUF repackages of other people's models). Previously allowed for known
    # orgs, which leaked e.g. unsloth/diffusiongemma-…-GGUF into the candidate
    # set, where the publish-QA quant gate then blocked the entire digest.
    if "-gguf" in model_lower or "_gguf" in model_lower:
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

    # Non-LLM task types
    non_llm_tasks = [
        "image-classification", "object-detection", "image-segmentation",
        "audio-classification", "automatic-speech-recognition", "table-to-text",
        "fill-mask", "feature-extraction", "sentence-similarity",
        "zero-shot-classification", "token-classification",
        "translation", "summarization", "text-to-image", "image-to-text",
        "depth-estimation", "video-classification", "text-to-video",
        "text-to-speech", "music-generation", "voice",
    ]
    if any(t in tags_lower for t in non_llm_tasks):
        return True

    # Non-LLM architectures
    non_llm_arch = ["resnet", "vit", "efficientnet", "bert", "roberta",
                     "distilbert", "albert", "wav2vec", "whisper",
                     "soundstream", "encodec", "vits", "mobilenet",
                     "yolo", "detr", "sam", "clip"]
    if any(a in model_lower for a in non_llm_arch):
        return True

    # Unknown orgs: strict engagement gate
    if author_prefix not in KNOWN_ORGS:
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


def is_stale_release(release_date, today: str = None, max_age_days: int = 14) -> bool:
    """True when a model's release date is too old to count as news.

    Guards against new-org backfill: when the supervisor adds an org to the
    fetch lists, that org's entire back-catalog is unseen by the dedup DB and
    would flood the digest as "new" (2026-06-11: Kimi-VL from 2025-04 appeared
    in a "new today" digest). Unknown or unparseable dates are kept — absence
    of a date is not evidence of staleness.
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


def is_significant_release(model_id: str, author: str, tags: list,
                           downloads: int = 0) -> bool:
    """Check if this is a significant release worth reporting."""
    model_lower = model_id.lower()
    author_lower = (author or "").lower()

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
        "north-mini", "higgs-audio",
        "exaone", "k-exaone",
        "anima", "reka-edge", "lyria-",
        "openai/o1", "openai/o3", "anthropic/claude",
        "wan2", "dramabox", "pixal3d", "agent",
    ]
    if any(f in model_lower for f in significant_families):
        return True

    significant_orgs = ["meta-llama", "mistralai", "alibaba", "qwen", "google",
                        "deepseek-ai", "deepseek", "anthropic", "openai", "x-ai",
                        "z-ai", "zai-org", "arcee-ai", "nvidia", "microsoft",
                        "minimaxai", "openbmb", "netflix", "k2-fsa",
                        "xiaomi", "rekaai", "baai", "tencentarc",
                        "resembleai", "adskailab", "open-thoughts"]
    if author_lower in significant_orgs:
        return True

    if downloads and downloads >= 100000:
        return True

    return False


def fetch_openrouter_models() -> List[ModelRelease]:
    models = []
    try:
        resp = _http_get("https://openrouter.ai/api/v1/models", "OpenRouter", timeout=30)
        for m in resp.json().get("data", []):
            model_id = m.get("id", "")
            if not model_id:
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
        print(f"OpenRouter error: {e}", file=sys.stderr)
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
        print(f"Ollama error: {e}", file=sys.stderr)
    return models


# Major AI orgs to monitor directly on HuggingFace
MAJOR_HF_ORGS = [
    "deepseek-ai", "meta-llama", "mistralai", "Qwen", "google",
    "anthropic", "openai", "nvidia", "microsoft", "x-ai",
    "z-ai", "zai-org", "arcee-ai", "openbmb", "minimaxai",
    "NousResearch", "tiiuae", "01-ai", "baai", "xiaomi",
    "moonshotai", "bytedance-seed", "bytedance-research", "inclusionai", "ibm",
    "MiniMaxAI",
    "allenai", "amazon", "perplexity-ai", "stabilityai",
    "HiDream-ai", "SulphurAI", "Zyphra",
    "circlestone-labs", "Hcompany", "Supertone",
    "TencentARC", "ResembleAI", "ADSKAILab", "open-thoughts",
    "CohereLabs", "LGAI-EXAONE",
    "bosonai", "sapientinc",
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
# no-models, crash) still alert.
INLINE_PRIMARY = os.environ.get("MODELBYTES_INLINE_PRIMARY") == "1"


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

            rd = m.get("createdAt", "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            desc = _smart_truncate(
                f"{pipeline} model" if pipeline else "ML model", 200)

            models.append(ModelRelease(
                name=model_id,
                provider=_resolve_provider(author),
                source="huggingface-org",
                url=f"https://huggingface.co/{model_id}",
                description=desc,
                release_date=rd,
                architecture=tags[0] if tags else None,
                is_open_source=True,
                unique_traits=["hf_hub"] + tags[:3],
                downloads=downloads,
                likes=likes,
            ))
    except Exception as e:
        print(f"HF org {author} error: {e}", file=sys.stderr)
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

            rd = m.get("createdAt", "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
                architecture=tags[0] if tags else None,
                is_open_source=True,
                unique_traits=["hf_hub"] + tags[:3],
                downloads=downloads,
                likes=likes,
            ))
    except Exception as e:
        print(f"HF top text-gen error: {e}", file=sys.stderr)
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

            rd = m.get("createdAt", "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
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

            rd = m.get("createdAt", "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
                architecture=tags[0] if tags else None,
                is_open_source=True,
                unique_traits=["hf_hub"] + tags[:3],
                downloads=downloads,
                likes=likes,
            ))
    except Exception as e:
        print(f"HF error: {e}", file=sys.stderr)
    return models


def categorize_model(model: ModelRelease) -> str:
    name = model.name.lower()
    provider = (model.provider or "").lower()
    traits = [t.lower() for t in (model.unique_traits or [])]

    premier = ["llama-3.3", "llama-3.2", "mistral-large", "mixtral",
               "qwen2.5-72b", "qwen3", "qwen3.6", "deepseek-v3", "deepseek-v4",
               "gemma-2-27b", "gemma-4", "gemma-3", "diffusiongemma", "command-r-plus", "command-a", "nemotron",
               "sulphur", "minicpm", "zaya", "glm-5", "glm-4.7",
               "minimax", "grok-2", "grok-3"]
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
    # Known significant orgs always get meaningful categorization
    sig_org_map = {"tencentarc": "specialized", "resembleai": "specialized",
                   "adskailab": "other", "open-thoughts": "specialized",
                   "deepseek-ai": "open_frontier", "inclusionai": "open_frontier",
                   "moonshotai": "open_frontier"}
    if provider in sig_org_map:
        cat = sig_org_map[provider]
        return cat
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
    return "📦 Open weights · HF"


def build_digest_message(models: List[ModelRelease]) -> str:
    """Build tiered digest message (HTML format)."""
    if not models:
        return "No new models today."
    models, _ = prepare_models_for_digest(models)

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
        f"<i>{datetime.now(timezone.utc).strftime('%A, %B %d, %Y')}</i>",
        "",
    ]

    def _section(title: str, emoji: str, items: List[ModelRelease]):
        if not items:
            return
        lines.extend(["", f"━━━ <b>{title}</b> {emoji}", ""])
        for m in items:
            # Strip :free and :latest suffixes for cleaner display
            display_name = m.name.split("/")[-1].replace(":free", "").replace(":latest", "").replace("-latest", "")
            lines.append(f"<b>{display_name}</b>")
            if m.description:
                # Strip OpenRouter markdown links that don't render in Telegram HTML
                clean_desc = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', m.description)
                lines.append(f"  {_smart_truncate(clean_desc, 150)}...")
            specs = []
            if m.release_date and m.release_date != datetime.now(timezone.utc).strftime("%Y-%m-%d"):
                specs.append(f"Released: {m.release_date}")
            if m.context_window:
                specs.append(f"Context: {_format_context(m.context_window)}")
            if m.license:
                specs.append(f"License: {m.license}")
            if m.total_parameters:
                if m.active_parameters:
                    specs.append(f"Params: {m.total_parameters} total / {m.active_parameters} active")
                else:
                    specs.append(f"Params: {m.total_parameters}")
            if m.pricing_input is not None and m.pricing_input > 0:
                specs.append(f"${m.pricing_input:.2f}/${m.pricing_output:.2f} per 1M")
            elif m.pricing_input == 0:
                specs.append("FREE")
            if specs:
                lines.append(f"  {' | '.join(specs)}")
            lines.append(f"  {_availability_tag(m)}")
            link = m.canonical_url or m.url
            if link:
                lines.append(f'  <a href="{link}">→ Source</a>')
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


def _count_surfaced_models(summary: str) -> int:
    """Count model entries actually rendered in an LLM digest body.

    Entries are bold-name lines followed by an em-dash/hyphen
    ("<b>Name</b> — ...") plus Local Ready bullets ("• ..."). Tier headers
    like "<b>🔓 Premier Open</b>" have no trailing dash and are not counted.
    """
    count = 0
    for raw in summary.splitlines():
        line = raw.strip()
        if re.match(r"^<b>[^<]+</b>\s*[—-]", line):
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
# How many entries the last summarize_models() call trimmed for carrying a stale
# release date. Lets main() send a non-blocking ops note that the writer leaked
# a stale date and an entry was dropped (2026-07-04 incident).
LAST_STALE_DROPPED = 0


def _recent_digest_names(today: str = None, days: int = 10,
                         pending_dir: Path = None) -> List[str]:
    """Bold entry names from the last `days` pending digests (today excluded),
    so the writer doesn't repeat a model we already covered — the curator's
    dedup mechanism, replicated for the inline path."""
    pending_dir = pending_dir or Path("pending")
    if not pending_dir.is_dir():
        return []
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    files = [p for p in sorted(pending_dir.glob("*.txt"), reverse=True)
             if p.stem != today][:days]
    seen, out = set(), []
    for p in files:
        try:
            text = p.read_text()
        except OSError:
            continue
        for name in _ENTRY_RE.findall(text):
            name = name.strip()
            if name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
    return out


def discover_recent_releases(today: str = None, max_age_days: int = 14,
                             timeout: int = 60) -> str:
    """Query Parallel.ai for genuinely-new model releases → a compact, cited
    web-research block for the writer model. Returns '' when disabled or on any
    failure (the pipeline falls back to fetcher-only). Never raises."""
    if not (DISCOVERY_ENABLED and PARALLEL_API_KEY):
        return ""
    ref = (datetime.strptime(today, "%Y-%m-%d").date() if today
           else datetime.now(timezone.utc).date())
    month = ref.strftime("%B %Y")
    body = {
        "objective": (
            f"Find AI models newly released or updated within {max_age_days} days "
            f"of {ref.isoformat()}: open-weight and API models across text, reasoning, "
            "coding, multimodal, and audio. Prefer primary sources (vendor blogs, "
            "model cards, release notes) stating the release date and specs."),
        # Diverse angles → broader recall across tiers (frontier/open/coding/
        # multimodal/audio/local), not just one obvious release.
        "search_queries": [
            f"new AI model release {month}",
            f"new open-weight LLM released {month}",
            f"new multimodal model released {month}",
            f"new coding model release {month}",
            f"new audio or speech model released {month}",
            "AI model launch announcement this week",
            "Hugging Face newly released model this week",
        ],
    }
    try:
        resp = requests.post(PARALLEL_SEARCH_URL, json=body,
                             headers={"x-api-key": PARALLEL_API_KEY,
                                      "Content-Type": "application/json"},
                             timeout=timeout)
        resp.raise_for_status()
        results = resp.json().get("results", []) or []
    except Exception as e:
        print(_redact_secrets(f"Parallel discovery failed: {e}"), file=sys.stderr)
        return ""

    kept = []
    for r in results:
        pd = (r.get("publish_date") or "").strip()
        if pd:
            try:
                age = (ref - datetime.strptime(pd[:10], "%Y-%m-%d").date()).days
                if age > max_age_days or age < -2:
                    continue  # too old, or implausibly future
            except ValueError:
                pass  # unparseable → keep, let the writer judge
        url = (r.get("url") or "").strip()
        title = (r.get("title") or "").strip()
        excerpt = " ".join((r.get("excerpts") or [])[:2]).strip()
        if not url or not (title or excerpt):
            continue
        kept.append(f"- {title} ({pd or 'undated'}) — {url}\n  "
                    f"{_smart_truncate(excerpt, 280)}")
        if len(kept) >= 10:
            break
    if not kept:
        return ""
    print(f"Parallel discovery: {len(kept)} recent web source(s).", file=sys.stderr)
    return "\n".join(kept)


def _collect_provided_urls(models, web_context: str) -> set:
    """Every URL we actually handed the writer — candidate model URLs + the
    source URLs from the Parallel web research. A digest link outside this set
    was constructed by the model and must not be published."""
    urls = set()
    for m in models or []:
        for u in (getattr(m, "url", None), getattr(m, "canonical_url", None)):
            if u:
                urls.add(u.rstrip("/"))
    for u in re.findall(r"https?://[^\s)\]]+", web_context or ""):
        urls.add(u.rstrip("/").rstrip(".,"))
    return urls


def _strip_unverified_links(summary: str, allowed_urls: set) -> Tuple[str, int]:
    """Drop any one-line entry whose <a href> wasn't a URL we provided (the
    writer must cite a real source, never construct one — the curator verified
    URLs by fetching; this enforces it deterministically). Then drop tier
    headers left with no entries. Returns (cleaned, dropped_count)."""
    allowed = {u.rstrip("/") for u in allowed_urls}
    kept, dropped = [], 0
    for line in summary.split("\n"):
        hrefs = re.findall(r'<a href="([^"]+)"', line)
        if hrefs and not all(h.rstrip("/") in allowed for h in hrefs):
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


def _strip_stale_entries(summary: str, today: str = None) -> Tuple[str, int]:
    """Drop any single entry line carrying a release date too old to be "new
    today", then prune orphaned tier headers. This is the per-entry counterpart
    to the whole-body stale-release gate: it reuses the SAME parser
    (_loose_release_dates) and the SAME predicate (is_stale_release), pinned to
    one reference date, so a stale date the gate would block is removed at entry
    granularity first. One hallucinated date (from an undated web source or the
    writer's own knowledge) therefore trims a single entry instead of taking the
    whole digest dark (the 2026-07-04 incident). The gate stays as the backstop
    for a stale date that lands outside an entry line. Returns (cleaned, count).
    """
    kept, dropped = [], 0
    for line in summary.split("\n"):
        if any(is_stale_release(d, today=today)
               for d in _loose_release_dates(line, today=today)):
            dropped += 1
            continue
        kept.append(line)
    return _prune_orphaned_tiers(kept), dropped


def _call_llm(model: str, prompt: str) -> Optional[str]:
    """One chat-completion call against the configured OpenAI-compatible endpoint.
    Returns the stripped content, or None on any failure or empty body — the
    caller decides whether to try the next model or the template."""
    try:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            # Headroom for reasoning models that spend tokens on hidden reasoning
            # before emitting the digest body (3000 produced empty bodies twice).
            "max_tokens": 8000,
            "temperature": 0.3,
        }
        headers = {"Authorization": f"Bearer {LLM_API_KEY}",
                   "Content-Type": "application/json"}
        resp = requests.post(f"{LLM_BASE_URL}/chat/completions",
                             json=payload, headers=headers, timeout=LLM_TIMEOUT)
        resp.raise_for_status()
        return (resp.json()["choices"][0]["message"]["content"] or "").strip() or None
    except Exception as e:
        print(_redact_secrets(f"LLM '{model}' call failed: {e}"), file=sys.stderr)
        return None


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
                     recent_names: List[str] = None) -> str:
    """Use LLM for concise digest if key available.

    web_context: cited Parallel.ai web research on recent releases (the inline
    path's freshness engine). recent_names: models already covered in recent
    digests, so the writer doesn't repeat them.
    """
    global LAST_SUMMARY_MODE, LAST_LLM_MODEL, LAST_STALE_DROPPED
    LAST_SUMMARY_MODE = "template"
    LAST_LLM_MODEL = None
    LAST_STALE_DROPPED = 0
    recent_names = recent_names or []
    if not models and not web_context:
        return "No new models today."
    models, validation_notes = prepare_models_for_digest(models)
    for note in validation_notes:
        print(f"Digest QA: {note}", file=sys.stderr)

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
            "only models clearly released/updated in the last ~10 days.\n"
            "LINKS: each entry's <a href> MUST be a source URL copied CHARACTER-FOR-"
            "CHARACTER from the lines below. NEVER construct, complete, or guess a URL "
            "(e.g. do not invent a huggingface.co/<org>/<name> link) — if you have no "
            "provided URL for a model, omit that model. Never state a spec not in these "
            "sources.\n"
            f"{web_context}\n")
    avoid_block = ""
    if recent_names:
        avoid_block = ("\nALREADY COVERED in recent digests — do NOT repeat unless there "
                       "is a NEW development (then say what changed):\n"
                       + ", ".join(recent_names[:60]) + "\n")

    prompt = f"""You are ModelBytes, an AI model tracker. Write a SHORT Telegram digest.

FORMAT (begin with the Take line, then the tiers in this order, hide empty ones):
<i>one opinionated sentence on what today's releases mean for a builder — lead with the pattern across the day's models, not a single project name; write the sentence only, with no label in front of it; omit this whole line if nothing ties the day together</i>

━━━ <b>OPEN FRONTIER</b> 🔓
<b>Clean Model Name</b> — <i>One sentence: the differentiator / value prop — why a builder should care.</i> Hard facts (params, context, license, pricing — only if provided). ⚡ or 📦 availability. <a href="URL">→ Source</a>

━━━ <b>CLOSED FRONTIER</b> 🔒
(same entry format)

━━━ <b>SPECIALIZED</b> 🎯
(same entry format — domain models: coding, audio, image, video)

━━━ <b>LOCAL</b> 🏠
(same entry format — models whose headline is running on your own hardware)

ENTRY GRAMMAR (every entry, no exceptions):
1. <b>Clean display name</b> — drop the "org/" prefix and any leading "~", and write it the way people say it, not the raw repo id. E.g. "MiniMaxAI/MiniMax-M3" → "MiniMax M3"; "google/gemma-4-12B-it" → "Gemma 4 12B"; "~anthropic/claude-fable-latest" → "Claude Fable"; "open-thoughts/OpenThinkerAgent-32B" → "OpenThinkerAgent 32B". Keep the version/size; drop format suffixes like "-it"/"-Instruct". Then an <i>italic differentiator sentence</i>: what makes this model different / why it exists. Not a spec recitation.
2. Hard facts from the data below.
3. Availability tag: "⚡ API live · OpenRouter" (openrouter source), "📦 Open weights · HF" (huggingface), "📦 Ollama pull-ready" (ollama).
4. <a href="URL">→ Source</a> using the canonical URL when given.

RULES:
- ONLY HTML tags: <b>, <i>, <a href>
- Release date as "Released Apr 7" (no year)
- SKIP: fine-tunes, ONNX, LoRA, GGUF, embedders, experiments, distilled, personal merges
- Treat each model's Confidence and Unknowns as pre-publish QA.
- Only mention release date, license, total params, or active params if explicitly provided below.
- DO weave in the concrete specs provided (params, context, license, and any "Benchmarks (from model card)" line) — they make an entry land. Prefer one hard number over a vague adjective.
- Do not infer or invent parameter counts, license terms, benchmark numbers, or release dates beyond what is provided below.
- If a model is low confidence, skip it unless it is the only item in its section.
- No filler verbs: explores, reveals, highlights, offering, showcases, demonstrates, unpacks, breaks down, dives into, worth watching, notable, gaining traction
- HIDE empty sections
- Deduplicate across platforms
- MAX 2800 chars
- Do NOT write a totals/count line; it is appended automatically.
- Technical and direct, no hype
- Prefer genuinely-NEW models (released/updated in the last ~10 days). The web research below is your freshness source; the fetched catalog may be mostly already-seen.
{avoid_block}{web_block}
Candidate models from our fetchers (may be sparse or already-covered — the web research above is primary for freshness):
{chr(10).join(info) if info else "(none from fetchers today — build the digest from the web research above)"}"""

    if not LLM_API_KEY:
        print("No LLM key — falling back to template digest")
        return build_digest_message(models)

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
        return build_digest_message(models)

    # The model is unreliable at filling the count (it echoes the literal
    # "X"); strip any footer it emitted and append a deterministic one.
    summary = re.sub(r"(?im)^\s*(?:total:\s*)?[\dx]+\s+(?:models|items) tracked today\s*$", "", summary).rstrip()
    summary = re.sub(r"(?im)^\s*📊?\s*surfaced\b.*\bscanned\b.*today\s*$", "", summary).rstrip()
    if not summary:
        print("LLM body was only a footer — falling back to template")
        return build_digest_message(models)
    # Hard guarantee: every published link is a URL we actually provided. Drops
    # entries whose <a href> the writer constructed/guessed (e.g. a plausible
    # but unverified huggingface.co/... link) rather than copying a source URL.
    summary, dropped = _strip_unverified_links(
        summary, _collect_provided_urls(models, web_context))
    if dropped:
        print(f"Dropped {dropped} entr(y/ies) with unverified/constructed links.",
              file=sys.stderr)
    # Stale-date scrub: the writer can emit a release date older than the
    # freshness window (from an undated web source or its own knowledge). Drop
    # just those entries — the per-entry counterpart to the link scrub — so one
    # stale line trims a single entry instead of tripping the whole-body gate
    # and taking the digest dark (the 2026-07-04 incident). The gate remains the
    # backstop for a stale date outside an entry line.
    summary, stale_dropped = _strip_stale_entries(summary)
    LAST_STALE_DROPPED = stale_dropped
    if stale_dropped:
        print(f"Dropped {stale_dropped} entr(y/ies) with a stale release date.",
              file=sys.stderr)
    if not summary.strip() or not re.search(r"<b>[^<]+</b>\s*[—-]", summary):
        print("No entries survived link/staleness verification — falling back to template")
        return build_digest_message(models)
    header = f"🤖 <b>ModelBytes Digest</b>\n<i>{datetime.now(timezone.utc).strftime('%A, %B %d, %Y')}</i>"
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


# 10 minutes covers curator jitter (it lands 15:42-15:45 against a 16:00 cron)
# without holding the container for half an hour on days the curator is dead.
PENDING_GRACE_SECONDS = int(os.environ.get("MODELBYTES_PENDING_GRACE_SECONDS", "600"))
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
    """Grace window: the curator usually lands ~15:42-15:45 but has slipped past
    16:00 before (2026-06-08: 18:50). The container is already running, so
    polling GitHub for a few minutes costs nothing and beats permanently losing
    a late curated digest to the fallback (the ledger makes that irreversible).
    Disable with MODELBYTES_PENDING_GRACE_SECONDS=0."""
    if PENDING_GRACE_SECONDS <= 0:
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


def main():
    preview_mode = "--preview" in sys.argv
    if preview_mode:
        sys.argv.remove("--preview")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    live_mode = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID) and not preview_mode

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
    for source_name, fetcher in [
        ("OpenRouter", fetch_openrouter_models),
        ("Ollama", fetch_ollama_models),
        ("HuggingFace-Trending", fetch_huggingface_trending),
        ("HuggingFace-Orgs", fetch_major_orgs),
        ("HuggingFace-Top-TextGen", fetch_hf_text_generation),
    ]:
        print(f"Fetching {source_name}...")
        for model in fetcher():
            if model.name not in seen_models:
                all_new.append(model)
                # Don't add to seen_models yet — noise models should be
                # re-evaluated next run with updated engagement data.
                # Only posted/significant models get added later.

    stale = [m for m in all_new if is_stale_release(m.release_date)]
    if stale:
        print(f"Dropping {len(stale)} stale back-catalog model(s): "
              + ", ".join(m.name for m in stale[:5])
              + ("…" if len(stale) > 5 else ""))
        # Mark them seen so they stop resurfacing on every future run.
        for m in stale:
            seen_models.add(m.name)
        all_new = [m for m in all_new if not is_stale_release(m.release_date)]

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
            return 0
        print("First run — seeding, no digest sent")
        # Seed all current models so they won't be reported as "new" next time
        for m in all_new:
            seen_models.add(m.name)
        save_seen_models(seen_models)
        record_publish_run(today, "fallback", "seeded", models_found=len(all_new))
        ping_heartbeat(True, "seeded")
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
    # so the channel stays fresh without the claude.ai curator.
    web_context = discover_recent_releases(today)
    recent_names = _recent_digest_names(today)

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

        # Mark posted models seen so they don't re-appear. Use the pre-collapse
        # names (the individual variants) PLUS the collapsed family entry names
        # so neither the variants nor the family-base re-surfaces.
        for m in pre_collapse:
            seen_models.add(m.name)
        for m in digest_models:
            seen_models.add(m.name)
        # For the overflow, mark ONLY confirmed-insignificant models as seen so
        # we don't re-scan noise every run -- but keep significant-but-unposted
        # models UNSEEN so a busy-day overflow (or a model gaining traction)
        # surfaces on a later run instead of being silently dropped.
        for m in held:
            if not _significant(m):
                seen_models.add(m.name)

        # Enrich the top candidates with real HF-card facts (params, license,
        # context, benchmarks) so the inline model writes from specs, not just
        # its training knowledge — closes the research gap vs the old curator.
        if ENRICH_HF_CARDS:
            enrich_with_hf_cards(digest_models)

        message = summarize_models(digest_models, web_context, recent_names)
        fallback_mode = f"fallback-{LAST_SUMMARY_MODE}"
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
            send_ops_alert(f"Primary LLM '{LLM_MODEL}' unavailable for {today} — "
                           f"published with fallback model '{LAST_LLM_MODEL}'. "
                           "Check Ollama Cloud availability / update the primary.")
        # Content-drift signal (non-blocking): the writer leaked a stale release
        # date and the per-entry scrub trimmed it, so the digest shipped without
        # tripping the whole-body gate (the 2026-07-04 dark-channel incident).
        # Worth knowing — a recurring trim points at a bad web source or model
        # drift — but never a reason to block the post.
        if LAST_STALE_DROPPED:
            send_ops_alert(f"Trimmed {LAST_STALE_DROPPED} stale-dated "
                           f"entr{'y' if LAST_STALE_DROPPED == 1 else 'ies'} from "
                           f"today's digest ({today}) before publishing — the "
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
        print("No new models")
        if not preview_mode:
            send_ops_alert(f"No post today ({today}): no curated digest and no new "
                           "models surfaced by the fallback. If sources look quiet "
                           "several days running, check fetchers.")
            record_publish_run(today, "fallback", "no-models", models_found=0)
            ping_heartbeat(True, "no models")

    save_seen_models(seen_models)
    return 0


if __name__ == "__main__":
    try:
        _rc = main()
    except Exception as _e:
        # A crash anywhere must still reach the operator and the dead-man's
        # switch — Railway only records the exit code.
        send_ops_alert(f"Publisher CRASHED: {_e!r}")
        ping_heartbeat(False, f"crash: {_e!r}")
        raise
    sys.exit(_rc)

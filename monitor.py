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
LLM_BASE_URL = os.environ.get("MODELBYTES_LLM_URL", "https://api.openai.com/v1")

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
    "circlestone-labs", "moonshotai", "bytedance-seed", "bytedance-research",
    "amazon", "perplexity-ai", "inclusionai",
    "tencentarc", "resembleai", "adskailab", "open-thoughts",
    "lgai-exaone",
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
    if _fact_matches_text(ZAYA_FACT, message):
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


def validate_digest_for_publish(message: str) -> Tuple[str, List[str], List[str]]:
    """Return normalized message plus warnings/errors for pre-publish QA."""
    warnings = []
    errors = []
    normalized = (message or "").strip()
    normalized, corrections = _normalize_known_fact_claims(normalized)
    warnings.extend(corrections)

    if not normalized:
        errors.append("digest body is empty")
    if "ModelBytes Digest" not in normalized:
        warnings.append("digest header is missing")
    _lower = normalized.lower()
    if ("items tracked today" not in _lower
            and "models tracked today" not in _lower
            and "scanned" not in _lower):
        warnings.append("tracked-model footer is missing")

    for fact in KNOWN_MODEL_FACTS:
        if not _fact_matches_text(fact, normalized):
            continue
        if fact.canonical_url not in normalized:
            warnings.append(f"{fact.canonical_name}: canonical source URL missing")
        if fact.license and fact.license.lower() not in normalized.lower():
            warnings.append(f"{fact.canonical_name}: license not stated")
        if fact.total_parameters and fact.total_parameters.lower() not in normalized.lower():
            warnings.append(f"{fact.canonical_name}: total parameter count not stated")
        if fact.active_parameters and fact.active_parameters.lower() not in normalized.lower():
            warnings.append(f"{fact.canonical_name}: active parameter count not stated")

    if _fact_matches_text(ZAYA_FACT, normalized) and re.search(
        r"\b8(?:\.0)?B\s+active\b",
        normalized,
        flags=re.IGNORECASE,
    ):
        errors.append("ZAYA1-8B still has an incorrect 8B-active-parameter claim")

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
            "-eagle", "_eagle", "-mtp", "-draft-head",
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
    
    # GGUF: noise for unknown orgs, allow for known orgs (unsloth, bartowski, etc.)
    if ("-gguf" in model_lower or "_gguf" in model_lower):
        if author_prefix not in KNOWN_ORGS:
            return True
        # Known org GGUF = legitimate distribution, continue checking other junk
    
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
        "arcee", "minimax", "voxcpm",
        "deepseek-v3", "deepseek-v4", "deepseek-ocr",
        "minicpm", "supertonic", "supertone",
        "sulphur", "hidream", "zamba", "zaya",
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
    "allenai", "amazon", "perplexity-ai", "stabilityai",
    "HiDream-ai", "SulphurAI", "Zyphra",
    "circlestone-labs", "Supertone",
    "TencentARC", "ResembleAI", "ADSKAILab", "open-thoughts",
    "CohereLabs", "LGAI-EXAONE",
]


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
               "gemma-2-27b", "gemma-4", "gemma-3", "command-r-plus", "command-a", "nemotron",
               "sulphur", "minicpm", "zaya", "glm-5", "glm-4.7",
               "minimax", "grok-2", "grok-3"]
    # NOTE: 'kimi' must NOT be in this list — Moonshot's Kimi K2 models are
    # open-weight (issue #16); moonshotai routes via sig_org_map below.
    closed = ["gpt-4", "claude-3", "claude-4", "claude-opus-4", "o1-", "o3-", "gemini-1.5", "gemini-2", "gemini-3", "grok-4"]
    reasoning = ["reasoning", "r1", "o1", "o3"]
    coding = ["codestral", "coder", "code-", "claude-3.5", "devstral"]
    image_gen = ["dall-e", "flux", "stable-diffusion", "midjourney", "wan2", "pixal"]
    audio = ["lyria", "supertone", "supertonic", "dramabox"]

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


def summarize_models(models: List[ModelRelease]) -> str:
    """Use LLM for concise digest if key available."""
    if not models:
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

    prompt = f"""You are ModelBytes, an AI model tracker. Write a SHORT Telegram digest.

FORMAT (tiers in this order, hide empty ones):
━━━ <b>OPEN FRONTIER</b> 🔓
<b>Model Name</b> — <i>One sentence: the differentiator / value prop — why a builder should care.</i> Hard facts (params, context, license, pricing — only if provided). ⚡ or 📦 availability. <a href="URL">→ Source</a>

━━━ <b>CLOSED FRONTIER</b> 🔒
(same entry format)

━━━ <b>SPECIALIZED</b> 🎯
(same entry format — domain models: coding, audio, image, video)

━━━ <b>LOCAL</b> 🏠
(same entry format — models whose headline is running on your own hardware)

ENTRY GRAMMAR (every entry, no exceptions):
1. <b>Name</b> — then an <i>italic differentiator sentence</i>: what makes this model different / why it exists. Not a spec recitation.
2. Hard facts from the data below.
3. Availability tag: "⚡ API live · OpenRouter" (openrouter source), "📦 Open weights · HF" (huggingface), "📦 Ollama pull-ready" (ollama).
4. <a href="URL">→ Source</a> using the canonical URL when given.

RULES:
- ONLY HTML tags: <b>, <i>, <a href>
- Release date as "Released Apr 7" (no year)
- SKIP: fine-tunes, ONNX, LoRA, GGUF, embedders, experiments, distilled, personal merges
- Treat each model's Confidence and Unknowns as pre-publish QA.
- Only mention release date, license, total params, or active params if explicitly provided below.
- Do not infer or invent parameter counts, license terms, benchmark numbers, or release dates.
- If a model is low confidence, skip it unless it is the only item in its section.
- No filler verbs: explores, reveals, highlights, offering, showcases, demonstrates, unpacks, breaks down, dives into, worth watching, notable, gaining traction
- HIDE empty sections
- Deduplicate across platforms
- MAX 2800 chars
- Do NOT write a totals/count line; it is appended automatically.
- Technical and direct, no hype

Models:
{chr(10).join(info)}"""

    if not LLM_API_KEY:
        print("No LLM key — falling back to template digest")
        return build_digest_message(models)

    try:
        payload = {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            # Headroom for reasoning models (e.g. GLM-5.1) that spend tokens on
            # hidden reasoning before emitting the digest body. 3000 produced
            # empty bodies twice (2026-06-08, 2026-06-11) — reasoning consumed
            # the whole budget before any content tokens.
            "max_tokens": 8000,
            "temperature": 0.3,
        }
        headers = {
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        }
        print(f"Calling LLM ({LLM_MODEL})...")
        resp = requests.post(f"{LLM_BASE_URL}/chat/completions",
                             json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        summary = resp.json()["choices"][0]["message"]["content"].strip()
        # The model is unreliable at filling the count (it echoes the literal
        # "X"); strip any footer it emitted and append a deterministic one.
        summary = re.sub(r"(?im)^\s*(?:total:\s*)?[\dx]+\s+(?:models|items) tracked today\s*$", "", summary).rstrip()
        summary = re.sub(r"(?im)^\s*📊?\s*surfaced\b.*\bscanned\b.*today\s*$", "", summary).rstrip()
        # Reasoning models (e.g. GLM) can spend their whole budget on hidden
        # reasoning and return an empty content field — don't ship a headerless
        # blank; fall back to the deterministic template instead.
        if not summary:
            print("LLM returned an empty digest body — falling back to template")
            return build_digest_message(models)
        header = f"🤖 <b>ModelBytes Digest</b>\n<i>{datetime.now(timezone.utc).strftime('%A, %B %d, %Y')}</i>"
        # Honest footer: how many we actually surfaced vs how many we scanned.
        footer = f"📊 Surfaced {_count_surfaced_models(summary)} · scanned {len(models)} today"
        return f"{header}\n\n{summary}\n\n{footer}"
    except Exception as e:
        print(f"LLM failed: {e} — falling back to template")
        return build_digest_message(models)


TELEGRAM_MAX_CHARS = 4096
DIGEST_LIMIT = 15  # max models included in one daily digest


def _truncate_for_telegram(message: str, limit: int = TELEGRAM_MAX_CHARS) -> str:
    """Telegram rejects sendMessage over 4096 chars (UTF-16 code units, but
    char-count is a safe lower bound). If over limit, truncate at the last
    newline before the limit and append a truncation marker. Without this,
    oversized messages 400-fail and never reach the channel."""
    if len(message) <= limit:
        return message
    marker = "\n\n…[truncated]"
    headroom = limit - len(marker)
    cut = message.rfind("\n", 0, headroom)
    if cut < headroom * 0.7:  # no good newline boundary; fall back to char cut
        cut = headroom
    return message[:cut].rstrip() + marker


def send_telegram_post(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        print("Telegram not configured", file=sys.stderr)
        return False
    if len(message) > TELEGRAM_MAX_CHARS:
        original_len = len(message)
        message = _truncate_for_telegram(message)
        print(f"Telegram message was {original_len} chars (over {TELEGRAM_MAX_CHARS}); "
              f"truncated to {len(message)} chars.", file=sys.stderr)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        print(f"Sending {len(message)} chars...", file=sys.stderr)
        resp = requests.post(url, json=payload, timeout=30)
        if not resp.ok:
            print(f"Telegram error: {resp.text[:500]}", file=sys.stderr)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"Telegram send error: {e}", file=sys.stderr)
        return False


class _SlackMrkdwnConverter(HTMLParser):
    """Convert the small Telegram-HTML subset we emit (<b>, <i>, <a href>,
    <code>) into Slack mrkdwn: *bold*, _italic_, <url|label>, preserving line
    breaks. Mirrors the content-engine converter so Slack rendering is identical.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._in_link = False
        self._href = ""
        self._link_text: List[str] = []

    @staticmethod
    def _esc(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def handle_starttag(self, tag, attrs):
        if tag in ("b", "strong"):
            self.parts.append("*")
        elif tag in ("i", "em"):
            self.parts.append("_")
        elif tag in ("code", "pre"):
            self.parts.append("`")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "a":
            self._in_link = True
            self._href = dict(attrs).get("href", "") or ""
            self._link_text = []

    def handle_endtag(self, tag):
        if tag in ("b", "strong"):
            self.parts.append("*")
        elif tag in ("i", "em"):
            self.parts.append("_")
        elif tag in ("code", "pre"):
            self.parts.append("`")
        elif tag == "a":
            label = "".join(self._link_text).strip()
            href = self._href.strip()
            if href and label:
                self.parts.append(f"<{href}|{self._esc(label).replace('|', '/')}>")
            elif href:
                self.parts.append(f"<{href}>")
            else:
                self.parts.append(self._esc(label))
            self._in_link = False
            self._href = ""
            self._link_text = []

    def handle_data(self, data):
        if self._in_link:
            self._link_text.append(data)
        else:
            self.parts.append(self._esc(data))

    def result(self) -> str:
        return "".join(self.parts)


def _telegram_html_to_slack_mrkdwn(value: str) -> str:
    parser = _SlackMrkdwnConverter()
    parser.feed(value)
    text = parser.result()
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


def _fetch_pending_from_github(today: str) -> Optional[str]:
    """Fetch today's curated pending file straight from GitHub raw.

    The Railway image only contains the pending file if a deploy happened
    AFTER the curator's ~15:45 UTC push — a race the 2026-06-11 publish lost
    (stale 14:19 image → bare template went out despite a good curated digest
    sitting on master). Fetching from the repo at publish time removes the
    deploy-timing dependency entirely. Returns None when absent/unreachable.
    """
    url = f"{PENDING_RAW_BASE}/{today}.txt"
    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code == 200 and resp.text.strip():
            print(f"Fetched curated digest from GitHub raw ({url}).")
            return resp.text
        if resp.status_code != 404:
            print(f"GitHub raw pending fetch: HTTP {resp.status_code}", file=sys.stderr)
    except Exception as e:
        print(f"GitHub raw pending fetch failed: {e}", file=sys.stderr)
    return None


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

    if pending_path.exists():
        body = pending_path.read_text().strip()
        if not body:
            print(f"Pending file {pending_path} is empty — falling back to pipeline.")
            return False
    else:
        # Stale image (deploy race): the curator may have pushed after this
        # image was built. Ask GitHub directly before giving up.
        remote = _fetch_pending_from_github(today)
        if remote is None:
            return False
        body = remote.strip()
        if not body:
            return False
    body, qa_warnings, qa_errors = validate_digest_for_publish(body)
    for warning in qa_warnings:
        print(f"Digest QA warning ({pending_path}): {warning}", file=sys.stderr)
    if qa_errors:
        print(
            f"Pending curated digest failed pre-publish QA: {'; '.join(qa_errors)}",
            file=sys.stderr,
        )
        return False

    print(f"Pending curated digest found at {pending_path} ({len(body)} chars). Posting.")
    if not send_telegram_post(body):
        print("Telegram send of curated digest failed — falling back to pipeline.",
              file=sys.stderr)
        return False

    mark_posted_digest(today, "curated", str(pending_path), body)
    send_slack_post(body)  # mirror to Slack (no-op unless configured)
    print(f"Posted curated digest for {today}.")
    return True


def main():
    preview_mode = "--preview" in sys.argv
    if preview_mode:
        sys.argv.remove("--preview")

    # Fast-path: post a pre-curated digest from the curator routine if one exists.
    # Falls through to the deterministic pipeline if no pending file or send fails.
    if not preview_mode and try_post_pending_curated():
        return 0

    init_database()
    seen_models = load_seen_models()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
        print("First run — seeding, no digest sent")
        # Seed all current models so they won't be reported as "new" next time
        for m in all_new:
            seen_models.add(m.name)
        save_seen_models(seen_models)
        return 0

    # Models passed the fetcher-level is_noise_model checks already; the prior
    # second pass here passed `m.provider` (display name like "Alibaba") as the
    # author arg, which never matches `KNOWN_ORGS` slugs like "qwen". That made
    # orgs with diverging display names (tencentarc/"Tencent ARC", allenai/"AI2")
    # fall into the unknown-org engagement gate and get filtered as noise.
    # Removing the broken pass; the fetcher-level filter is sufficient. (audit A11)
    if all_new:
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
        print(
            f"Posting top {len(digest_models)} of {len(all_new)} new model(s)"
            + (f"; {len(held)} held for a later run" if held else "")
        )

        # Mark posted models as seen so they don't re-appear.
        for m in digest_models:
            seen_models.add(m.name)
        # For the overflow, mark ONLY confirmed-insignificant models as seen so
        # we don't re-scan noise every run -- but keep significant-but-unposted
        # models UNSEEN so a busy-day overflow (or a model gaining traction)
        # surfaces on a later run instead of being silently dropped.
        for m in held:
            if not _significant(m):
                seen_models.add(m.name)

        message = summarize_models(digest_models)
        message, qa_warnings, qa_errors = validate_digest_for_publish(message)
        for warning in qa_warnings:
            print(f"Digest QA warning (fallback): {warning}", file=sys.stderr)
        if qa_errors:
            print(
                f"Fallback digest failed pre-publish QA: {'; '.join(qa_errors)}",
                file=sys.stderr,
            )
            return 1

        if preview_mode:
            print("=== PREVIEW ===")
            print(message)
            print(f"=== END ({len(message)} chars) ===")
            print("Preview mode — not sending")
            return 0

        if not send_telegram_post(message):
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
        send_slack_post(message)  # mirror to Slack (no-op unless configured)
        print("Digest sent")

    else:
        print("No new models")

    save_seen_models(seen_models)
    return 0


if __name__ == "__main__":
    sys.exit(main())

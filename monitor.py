#!/usr/bin/env python3
"""Monitor AI model releases from OpenRouter, Ollama, and Hugging Face.

Posts new model releases to Telegram channel with summaries.
"""

import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set

import requests

# PostgreSQL support (optional - falls back to JSON if not available)
try:
    import psycopg2
    from psycopg2.extras import execute_values
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False

# Config
SCRIPT_DIR = Path(__file__).parent.resolve()
STATE_FILE = SCRIPT_DIR / "state" / "model_releases_state.json"

# Database
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = POSTGRES_AVAILABLE and DATABASE_URL

# Debug: Print database connection status
print(f"DEBUG: DATABASE_URL set: {bool(DATABASE_URL)}")
print(f"DEBUG: POSTGRES_AVAILABLE: {POSTGRES_AVAILABLE}")
print(f"DEBUG: USE_POSTGRES: {USE_POSTGRES}")
if DATABASE_URL:
    # Mask password in URL for logging
    masked = DATABASE_URL.split('@')[0].rsplit(':', 1)[0] + '@***' if '@' in DATABASE_URL else 'set'
    print(f"DEBUG: DATABASE_URL preview: {masked}")

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")

# Fallback to ModelBytes bot/channel if not set (local dev)
if not TELEGRAM_BOT_TOKEN:
    TELEGRAM_BOT_TOKEN = "***REMOVED-DEAD-TOKEN***"
if not TELEGRAM_CHANNEL_ID:
    TELEGRAM_CHANNEL_ID = "-100XXXXXXXXXX"

def init_database():
    """Initialize PostgreSQL tables if using Postgres."""
    if not USE_POSTGRES:
        return
    
    print("DEBUG: Initializing PostgreSQL database...")
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        
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
        
        conn.commit()
        cur.close()
        conn.close()
        print("DEBUG: Database tables created successfully")
    except Exception as e:
        print(f"DEBUG: Database initialization error: {e}", file=sys.stderr)

def load_seen_models() -> Set[str]:
    """Load seen model IDs from Postgres or JSON."""
    if USE_POSTGRES:
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute("SELECT model_id FROM models")
            seen = {row[0] for row in cur.fetchall()}
            print(f"DEBUG: Loaded {len(seen)} models from Postgres")
            cur.close()
            conn.close()
            return seen
        except Exception as e:
            print(f"Postgres error, falling back to JSON: {e}", file=sys.stderr)
    
    # Fallback to JSON
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return set(data.get("seen_models", []))
        except Exception:
            pass
    return set()

def save_seen_models(models: Set[str]):
    """Save all seen models to Postgres or JSON."""
    print(f"DEBUG: Saving {len(models)} models to database...")
    if USE_POSTGRES:
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            # Delete all existing and insert fresh
            cur.execute("DELETE FROM models")
            for model_id in models:
                cur.execute(
                    "INSERT INTO models (model_id, name) VALUES (%s, %s) ON CONFLICT (model_id) DO NOTHING",
                    (model_id, model_id)
                )
            conn.commit()
            print(f"DEBUG: Saved {len(models)} models to Postgres")
            cur.close()
            conn.close()
            return
        except Exception as e:
            print(f"Postgres save error: {e}", file=sys.stderr)
    
    # Fallback to JSON
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"seen_models": sorted(list(models))[-5000:], "last_run": datetime.now().strftime("%Y-%m-%d")}
    STATE_FILE.write_text(json.dumps(data, indent=2))

def save_model(model_id: str, model_data: dict = None):
    """Save a model to Postgres or JSON."""
    if USE_POSTGRES:
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO models (model_id, name, provider, source, url, description,
                                   context_window, pricing_input, pricing_output,
                                   architecture, release_date, is_open_source, unique_traits)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (model_id) DO UPDATE SET
                    last_updated = NOW(),
                    name = EXCLUDED.name,
                    provider = EXCLUDED.provider
            """, (
                model_id,
                model_data.get("name", "") if model_data else "",
                model_data.get("provider", "") if model_data else "",
                model_data.get("source", "") if model_data else "",
                model_data.get("url", "") if model_data else "",
                model_data.get("description", "") if model_data else "",
                model_data.get("context_window") if model_data else None,
                model_data.get("pricing_input") if model_data else None,
                model_data.get("pricing_output") if model_data else None,
                model_data.get("architecture") if model_data else None,
                model_data.get("release_date") if model_data else None,
                model_data.get("is_open_source") if model_data else None,
                model_data.get("unique_traits", []) if model_data else []
            ))
            conn.commit()
            cur.close()
            conn.close()
            return
        except Exception as e:
            print(f"Postgres save error: {e}", file=sys.stderr)
    
    # Fallback to JSON
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"seen_models": list(load_seen_models()) + [model_id]}
    STATE_FILE.write_text(json.dumps(data, indent=2))

def log_post(model_id: str, message_id: int):
    """Log a successful post to database."""
    if USE_POSTGRES:
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO posts (model_id, message_id, channel_id) VALUES (%s, %s, %s)",
                (model_id, message_id, TELEGRAM_CHANNEL_ID)
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print(f"Failed to log post: {e}", file=sys.stderr)

@dataclass
class ModelRelease:
    name: str
    provider: str
    source: str  # "openrouter", "ollama", "huggingface"
    url: str
    description: str
    context_window: Optional[int] = None
    pricing_input: Optional[float] = None
    pricing_output: Optional[float] = None
    architecture: Optional[str] = None
    release_date: Optional[str] = None
    is_open_source: Optional[bool] = None  # True = open weights, False = proprietary
    performance_scores: dict = None  # e.g., {"mmlu": 85.2, "hellaswag": 92.1}
    unique_traits: List[str] = None  # e.g., ["multimodal", "long_context", "reasoning"]
    
    def __post_init__(self):
        if self.performance_scores is None:
            self.performance_scores = {}
        if self.unique_traits is None:
            self.unique_traits = []


def load_state() -> dict:
    """Load seen model IDs."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen_models": [], "last_run": ""}


def save_state(state: dict):
    """Save seen model IDs."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fetch_openrouter_models() -> List[ModelRelease]:
    """Fetch models from OpenRouter API."""
    models = []
    try:
        resp = requests.get("https://openrouter.ai/api/v1/models", timeout=30)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        
        for m in data:
            model_id = m.get("id", "")
            if not model_id:
                continue
                
            # Parse pricing
            pricing = m.get("pricing", {})
            input_price = None
            output_price = None
            try:
                input_price = float(pricing.get("prompt", 0)) * 1_000_000  # per million tokens
                output_price = float(pricing.get("completion", 0)) * 1_000_000
            except (ValueError, TypeError):
                pass
            
            # Context length
            context = m.get("context_length")
            
            # Determine if open source based on provider and model name
            is_open = False
            open_keywords = ["llama", "mistral", "qwen", "gemma", "mixtral", "phi", "falcon", "yi", "deepseek", "nemotron", "olm", "c4ai"]
            closed_providers = ["openai", "anthropic", "google", "cohere", "ai21"]
            provider_lower = (m.get("owned_by") or "").lower()
            model_lower = model_id.lower()
            
            if any(kw in model_lower for kw in open_keywords):
                is_open = True
            elif provider_lower in closed_providers:
                is_open = False
            elif "open" in provider_lower or "open" in model_lower:
                is_open = True
            
            # Detect unique traits
            traits = []
            if context and context >= 128_000:
                traits.append("long_context")
            if "vision" in model_lower or "vl" in model_lower:
                traits.append("multimodal")
            if any(x in model_lower for x in ["reasoning", "r1", "o3", "o1"]):
                traits.append("reasoning")
            if any(x in model_lower for x in ["code", "coder", "claude", "gpt-4"]):
                traits.append("coding")
            if input_price is not None and input_price < 0.5:
                traits.append("cheap")
            if "moe" in model_lower or "mixtral" in model_lower:
                traits.append("MoE")
            
            models.append(ModelRelease(
                name=model_id,
                provider=m.get("owned_by", "unknown"),
                source="openrouter",
                url=f"https://openrouter.ai/models/{model_id}",
                description=m.get("description", "")[:200],
                context_window=context,
                pricing_input=input_price,
                pricing_output=output_price,
                release_date=datetime.now().strftime("%Y-%m-%d"),
                is_open_source=is_open,
                unique_traits=traits,
                performance_scores={}  # OpenRouter doesn't expose benchmarks directly
            ))
    except Exception as e:
        print(f"OpenRouter fetch error: {e}", file=sys.stderr)
    
    return models


def fetch_ollama_models() -> List[ModelRelease]:
    """Fetch models from Ollama library (scrape the library page)."""
    models = []
    try:
        # Ollama doesn't have a public API for library, but we can fetch the library page
        resp = requests.get("https://ollama.com/library", timeout=30)
        resp.raise_for_status()
        html = resp.text
        
        # Extract model names from the page
        # Pattern: data-testid="repo-item" or similar links to /library/<name>
        pattern = r'href="/library/([^"]+)"'
        matches = re.findall(pattern, html)
        
        seen = set()
        for model_name in matches[:50]:  # Limit to avoid rate limits
            if model_name in seen or model_name.startswith("."):
                continue
            seen.add(model_name)
            
            # Ollama models are by definition open source (can run locally)
            models.append(ModelRelease(
                name=model_name,
                provider="ollama",
                source="ollama",
                url=f"https://ollama.com/library/{model_name}",
                description="Local LLM model available via Ollama",
                release_date=datetime.now().strftime("%Y-%m-%d"),
                is_open_source=True,
                unique_traits=["local", "open_source"]
            ))
    except Exception as e:
        print(f"Ollama fetch error: {e}", file=sys.stderr)
    
    return models



def fetch_leaderboard_benchmarks() -> dict:
    """Fetch latest benchmarks from LMSYS Chatbot Arena leaderboard."""
    # LMSYS API endpoint for leaderboard data
    benchmarks = {}
    try:
        # Try to fetch from HuggingFace spaces leaderboard API
        resp = requests.get("https://huggingface.co/spaces/lmsys/chatbot-arena-leaderboard/raw/main/leaderboard.csv", timeout=30)
        if resp.status_code == 200:
            # Parse CSV data - top models by ELO
            lines = resp.text.strip().split('\n')[:20]  # Top 20
            for line in lines[1:]:  # Skip header
                parts = line.split(',')
                if len(parts) >= 3:
                    model_name = parts[0].strip('"')
                    elo = parts[1].strip('"')
                    try:
                        benchmarks[model_name] = {"elo": float(elo)}
                    except:
                        pass
    except Exception as e:
        print(f"Leaderboard fetch error: {e}", file=sys.stderr)
    
    return benchmarks


def is_high_performance_model(model: ModelRelease) -> bool:
    """Check if model is flagged as high performance based on various signals."""
    name = model.name.lower()
    provider = (model.provider or "").lower()
    
    # Known high-performing model families
    high_perf_indicators = [
        "gpt-4", "claude-3", "gemini-1.5", "gemini-2", "o1", "o3",
        "llama-3.3", "llama-3.1-405b", "mistral-large", "mixtral",
        "qwen2.5-72b", "qwen3", "deepseek-v3", "deepseek-r1",
        "gemma-2-27b", "command-r-plus", "yi-large"
    ]
    
    # Long context models
    long_context_models = model.context_window and model.context_window >= 128_000
    
    # Check name patterns
    is_known_high_perf = any(ind in name for ind in high_perf_indicators)
    
    # Check for reasoning models
    is_reasoning = any(x in name for x in ["reasoning", "r1", "o1", "o3"])
    
    # Check performance scores if available
    has_high_scores = False
    if model.performance_scores:
        mmlu = model.performance_scores.get("mmlu", 0)
        if mmlu >= 80:
            has_high_scores = True
    
    return is_known_high_perf or is_reasoning or has_high_scores or long_context_models


def is_noise_model(model_id: str, author: str, tags: list) -> bool:
    """Filter out noise models we don't want to report."""
    model_lower = model_id.lower()
    author_lower = (author or "").lower()
    tags_lower = [t.lower() for t in tags]
    
    # Skip test/random models
    noise_patterns = [
        "tiny-random", "test", "dummy", "example", "demo", "sample",
        "random", "placeholder", "minimal", "toy-",
    ]
    if any(p in model_lower for p in noise_patterns):
        return True
    
    # Skip models moved/deprecated
    if any(p in model_lower for p in ["moved", "deprecated", "archived", "old", "backup"]):
        return True
    
    # Skip random user fine-tunes (personal username patterns)
    # Keep models from known orgs
    known_orgs = [
        "meta-llama", "mistralai", "qwen", "google", "anthropic",
        "openai", "deepseek", "alibaba", "microsoft", "facebook",
        "stabilityai", "huggingface", "sentence-transformers", "bartowski",
        "nousresearch", "tiiuae", "01-ai", "philschmid", "cognitivecomputations",
        "thebloke", "ollama", "unsloth", "maziyarpanahi", "mradermacher",
        "nvidia", "ibm", "allenai", "bigscience", "eleutherai",
    ]
    is_known_org = any(org in author_lower for org in known_orgs)
    
    # If unknown author and model name looks like a fine-tune hash or experiment
    if not is_known_org:
        # Skip models with hash-like suffixes (experiment naming)
        import re
        if re.search(r"[-_]\d{6,}$", model_lower):  # ends in _123456 or -123456
            return True
        if re.search(r"_seed\d+_", model_lower):  # experiment naming like _seed1_
            return True
        if re.search(r"_bs\d+_", model_lower):  # batch size experiment
            return True
        if re.search(r"_aug\d+", model_lower):  # augmentation experiment
            return True
        if re.search(r"_b\d+-ep_", model_lower):  # batch/epoch experiment
            return True
        if re.search(r"_\d+shot_", model_lower):  # few-shot experiment
            return True
        if re.search(r"_v\d+_", model_lower):  # version experiment
            return True
    
    return False


def is_significant_release(model_id: str, author: str, tags: list, downloads: int = 0) -> bool:
    """Check if this is a significant release worth reporting."""
    model_lower = model_id.lower()
    author_lower = (author or "").lower()
    
    # Known significant model families
    significant_families = [
        "llama-", "llama2-", "llama3", "mistral", "mixtral", "qwen2", "qwen3",
        "gemma-", "phi-", "falcon-", "yi-", "deepseek", "command-r",
        "codestral", "nvidia/llama", "nemotron", "olmo", "pythia",
    ]
    
    if any(f in model_lower for f in significant_families):
        return True
    
    # Known significant orgs releasing flagship models
    if author_lower in ["meta-llama", "mistralai", "alibaba", "qwen", "google",
                        "deepseek-ai", "anthropic", "openai"]:
        return True
    
    # High download count indicates significance
    if downloads and downloads >= 10000:
        return True
    
    return False


def fetch_huggingface_trending() -> List[ModelRelease]:
    """Fetch trending models from Hugging Face - filtered for quality."""
    models = []
    try:
        # HF API - fetch recently created models
        resp = requests.get(
            "https://huggingface.co/api/models",
            params={"sort": "lastModified", "direction": -1, "limit": 100},
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        
        for m in data:
            model_id = m.get("id", "")
            if not model_id:
                continue
            
            author = m.get("author", "")
            tags = m.get("tags", [])
            pipeline = m.get("pipeline_tag", "")
            downloads = m.get("downloads", 0)
            
            # Skip noise models
            if is_noise_model(model_id, author, tags):
                continue
            
            # Only include if significant OR high downloads
            if not (is_significant_release(model_id, author, tags, downloads) or downloads >= 5000):
                continue
            
            models.append(ModelRelease(
                name=model_id,
                provider=author or "unknown",
                source="huggingface",
                url=f"https://huggingface.co/{model_id}",
                description=f"{pipeline} model" if pipeline else "ML model",
                release_date=m.get("created_at", datetime.now().strftime("%Y-%m-%d"))[:10],
                architecture=tags[0] if tags else None,
                is_open_source=True,
                unique_traits=["hf_hub"] + tags[:3]
            ))
    except Exception as e:
        print(f"HuggingFace fetch error: {e}", file=sys.stderr)
    
    return models


def escape_markdown(text: str) -> str:
    """Escape Telegram Markdown special characters."""
    if not text:
        return text
    # Escape characters that have special meaning in Markdown
    return text.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("]", "\\]")


def format_model_post(model: ModelRelease) -> str:
    """Format a model release for Telegram post with performance traits."""
    lines = []
    
    # Header with model name and open/closed indicator
    open_indicator = "🔓" if model.is_open_source else "🔒"
    # Escape model name to prevent unintended markdown
    safe_name = escape_markdown(model.name)
    lines.append(f"{open_indicator} *{safe_name}*")
    
    # Provider line
    provider = model.provider or "unknown"
    lines.append(f"🏢 {provider}")
    
    # Source badge
    lines.append(f"📍 Source: {model.source}")
    
    # Architecture (if known)
    if model.architecture:
        lines.append(f"🏗️ {model.architecture}")
    
    # Context window
    if model.context_window:
        ctx = model.context_window
        if ctx >= 1_000_000:
            lines.append(f"📏 Context: {ctx/1_000_000:.1f}M tokens")
        elif ctx >= 1000:
            lines.append(f"📏 Context: {ctx/1000:.0f}k tokens")
        else:
            lines.append(f"📏 Context: {ctx:,} tokens")
    
    # Pricing
    if model.pricing_input is not None and model.pricing_output is not None:
        lines.append(f"💰 ${model.pricing_input:.2f} in / ${model.pricing_output:.2f} out per 1M")
    
    # Performance scores
    if model.performance_scores:
        perf_parts = []
        if "mmlu" in model.performance_scores:
            perf_parts.append(f"MMLU: {model.performance_scores['mmlu']:.1f}")
        if "hellaswag" in model.performance_scores:
            perf_parts.append(f"HellaSwag: {model.performance_scores['hellaswag']:.1f}")
        if "mmlu_pro" in model.performance_scores:
            perf_parts.append(f"MMLU-Pro: {model.performance_scores['mmlu_pro']:.1f}")
        if perf_parts:
            lines.append(f"📊 {', '.join(perf_parts)}")
    
    # Unique traits
    if model.unique_traits:
        trait_emojis = {
            "multimodal": "🖼️",
            "vision": "👁️",
            "long_context": "📜",
            "reasoning": "🧠",
            "coding": "💻",
            "agent": "🤖",
            "fast": "⚡",
            "cheap": "💸",
            "moE": "🔀",
            "local": "🏠",
            "open_source": "🔓",
            "hf_hub": "🤗",
        }
        trait_strs = []
        for trait in model.unique_traits:
            emoji = trait_emojis.get(trait.lower(), "✨")
            trait_strs.append(f"{emoji} {trait}")
        if trait_strs:
            lines.append(f"✨ {', '.join(trait_strs)}")
    
    # Description (shortened, escaped)
    if model.description and len(model.description) > 10:
        desc = escape_markdown(model.description[:100].strip())
        if desc:
            lines.append(f"\n📝 {desc}")
    
    # Link
    lines.append(f"\n🔗 [View]({model.url})")
    
    return "\n".join(lines)


def send_telegram_post(message: str) -> bool:
    """Send a message to Telegram channel."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        print("Telegram credentials not configured", file=sys.stderr)
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    
    try:
        resp = requests.post(url, json=payload, timeout=30)
        if not resp.ok:
            print(f"Telegram error: {resp.status_code} - {resp.text[:500]}", file=sys.stderr)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"Telegram send error: {e}", file=sys.stderr)
        return False


def categorize_model(model: ModelRelease) -> str:
    """Categorize model into tier based on significance."""
    name = model.name.lower()
    provider = (model.provider or "").lower()
    
    # Tier 1: Premier Open Weights - flagship releases
    premier_open = [
        "llama-3.3", "llama-3.2", "llama3.1-405b", "llama3.1-70b",
        "mistral-large", "mixtral-8x22b", "qwen2.5-72b", "qwen3",
        "deepseek-v3", "deepseek-r1", "gemma-2-27b", "gemma-2-9b",
        "phi-3", "phi-4", "command-r-plus", "yi-large",
    ]
    if any(p in name for p in premier_open):
        return "premier_open"
    
    # Tier 2: Closed Giants - proprietary SOTA
    closed_giants = ["gpt-4", "claude-3", "claude-3.5", "gemini-2", "gemini-1.5", "o1", "o3"]
    if any(p in name for p in closed_giants):
        return "closed_giants"
    
    # Tier 3: Reasoning/Specialized
    reasoning = ["reasoning", "r1", "o1", "o3", "qwq", "marco-o1"]
    if any(p in name for p in reasoning):
        return "reasoning"
    
    coding_models = ["coder", "codestral", "codeqwen", "deepseek-coder"]
    if any(p in name for p in coding_models):
        return "coding"
    
    # Tier 4: Niche/Specialized
    if model.source == "ollama":
        return "local_ready"
    
    # Default: mention only if significant
    return "other"


def get_why_care(model: ModelRelease) -> str:
    """Generate 'why you should care' for a model."""
    name = model.name.lower()
    provider = model.provider or ""
    
    reasons = {
        "llama-3.3": "Meta's latest flagship open model",
        "llama-3.2": "Multimodal-capable Llama variant",
        "mistral-large": "Mistral's strongest model",
        "deepseek-v3": "Top-tier Chinese open model",
        "deepseek-r1": "Reasoning-focused, beats o1 on benchmarks",
        "qwen2.5-72b": "Alibaba's flagship open model",
        "gemma-2-27b": "Google's best open model",
        "phi-3": "Microsoft's efficient small model",
        "claude-3.5": "Anthropic's latest, best for coding",
        "gpt-4": "OpenAI's flagship",
        "gemini-2": "Google DeepMind's latest",
    }
    
    for key, reason in reasons.items():
        if key in name:
            return reason
    
    # Generic by category
    if "coder" in name or "codestral" in name:
        return "Strong code generation"
    if "reasoning" in name or "r1" in name:
        return "Chain-of-thought reasoning"
    if model.context_window and model.context_window >= 128_000:
        return "Long context capability"
    
    return "Worth tracking"


def send_digest(models: List[ModelRelease]) -> bool:
    """Send a tiered digest of new models."""
    if not models:
        return True
    
    # Categorize models
    tiers = {
        "premier_open": [],
        "closed_giants": [],
        "reasoning": [],
        "coding": [],
        "local_ready": [],
        "other": []
    }
    
    for model in models[:20]:  # Cap at 20 total
        tier = categorize_model(model)
        tiers[tier].append(model)
    
    # Build digest
    lines = []
    lines.append(f"🤖 *ModelBytes Digest*")
    lines.append(f"_{datetime.now().strftime('%A, %B %d, %Y')}_")
    lines.append("")
    
    # Tier 1: Premier Open Weights
    if tiers["premier_open"]:
        lines.append("")
        lines.append("━━━ *PREMIER OPEN WEIGHTS* 🔓")
        lines.append("_(Flagship releases you should know about)_")
        lines.append("")
        for model in tiers["premier_open"][:3]:
            lines.append(f"• {escape_markdown(model.name.split('/')[-1])}")
            lines.append(f"  Why care: {get_why_care(model)}")
            if model.unique_traits:
                traits = ', '.join(model.unique_traits[:3])
                lines.append(f"  Traits: {traits}")
            lines.append("")
    
    # Tier 2: Closed Giants
    if tiers["closed_giants"]:
        lines.append("")
        lines.append("━━━ *CLOSED GIANTS* 🔒")
        lines.append("_(Proprietary models worth tracking)_")
        lines.append("")
        for model in tiers["closed_giants"][:2]:
            lines.append(f"• {escape_markdown(model.name.split('/')[-1])}")
            lines.append(f"  Why care: {get_why_care(model)}")
            lines.append("")
    
    # Tier 3: Reasoning/Coding
    if tiers["reasoning"] or tiers["coding"]:
        lines.append("")
        lines.append("━━━ *SPECIALIZED* 🎯")
        lines.append("_(Niche but mighty)_")
        lines.append("")
        for model in (tiers["reasoning"] + tiers["coding"])[:3]:
            lines.append(f"• {escape_markdown(model.name.split('/')[-1])}")
            lines.append(f"  Why care: {get_why_care(model)}")
            lines.append("")
    
    # Tier 4: Local Ready (Ollama)
    if tiers["local_ready"]:
        lines.append("")
        lines.append("━━━ *LOCAL READY* 🏠")
        lines.append("_(Run it yourself)_")
        lines.append("")
        names = [m.name for m in tiers["local_ready"][:5]]
        lines.append(', '.join(names))
        lines.append("")
    
    # Summary line
    total = sum(len(v) for v in tiers.values())
    lines.append("")
    lines.append(f"_Total: {total} models tracked today_")
    
    message = '\n'.join(lines)
    
    # Send (split if needed)
    if len(message) > 4000:
        parts = message.split('\n━━━')
        for i, part in enumerate(parts):
            if i > 0:
                part = '━━━' + part
            if not send_telegram_post(part):
                return False
    else:
        if not send_telegram_post(message):
            return False
    
    return True


def main():
    # Initialize PostgreSQL database if available
    init_database()
    
    # Load seen models from Postgres (if available) or JSON
    seen_models: Set[str] = load_seen_models()
    today = datetime.now().strftime("%Y-%m-%d")
    is_first_run = len(seen_models) == 0
    
    print(f"Checking for new model releases on {today}...")
    print(f"Currently tracking {len(seen_models)} models")
    
    # Fetch from all sources
    all_new_models = []
    
    print("Fetching OpenRouter...")
    for model in fetch_openrouter_models():
        if model.name not in seen_models:
            all_new_models.append(model)
            seen_models.add(model.name)
    
    print("Fetching Ollama...")
    for model in fetch_ollama_models():
        if model.name not in seen_models:
            all_new_models.append(model)
            seen_models.add(model.name)
    
    print("Fetching Hugging Face...")
    for model in fetch_huggingface_trending():
        if model.name not in seen_models:
            all_new_models.append(model)
            seen_models.add(model.name)
    
    print(f"Found {len(all_new_models)} new model(s)")
    
    # First run: just seed the database, don't send
    if is_first_run:
        print("First run - seeding database without sending digest")
        # Save to PostgreSQL (via save_seen_models) if available, else JSON
        save_seen_models(seen_models)
        return 0
    
    # Send digest if there are new models
    if all_new_models:
        if send_digest(all_new_models):
            print("Digest sent successfully")
        else:
            print("Failed to send digest", file=sys.stderr)
            return 1
    else:
        print("No new models to report")
    
    # Save state
    save_seen_models(seen_models)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())

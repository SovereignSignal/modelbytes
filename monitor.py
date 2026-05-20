#!/usr/bin/env python3
"""Monitor AI model releases from OpenRouter, Ollama, and Hugging Face.

Posts new model releases to Telegram @modelbytes channel with tiered, LLM-summarized digest.
"""

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set

import requests

# PostgreSQL support (optional - falls back to JSON if not available)
try:
    import psycopg2
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False

# Config
SCRIPT_DIR = Path(__file__).parent.resolve()
STATE_FILE = SCRIPT_DIR / "state" / "model_releases_state.json"

# Database
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = POSTGRES_AVAILABLE and DATABASE_URL

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")

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
    "amazon": "Amazon",
    "ibm": "IBM",
    "allenai": "AI2",
    "tencentarc": "Tencent ARC",
    "resembleai": "Resemble AI",
    "adskailab": "Autodesk AI Lab",
    "perplexity": "Perplexity",
    "cohere": "Cohere",
    "ai21": "AI21 Labs",
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
    "baai", "huggingface", "baidu", "perplexity", "cohere", "ai21",
    "sulphurai", "supertone", "hidream-ai", "zyphra",
    "circlestone-labs", "moonshotai", "bytedance-seed",
    "amazon", "perplexity-ai", "inclusionai",
    "tencentarc", "resembleai", "adskailab", "open-thoughts",
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

    def __post_init__(self):
        if self.performance_scores is None:
            self.performance_scores = {}
        if self.unique_traits is None:
            self.unique_traits = []


def init_database():
    if not USE_POSTGRES:
        return
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
    except Exception as e:
        print(f"DB init error: {e}", file=sys.stderr)


def load_seen_models() -> Set[str]:
    if USE_POSTGRES:
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute("SELECT model_id FROM models")
            seen = {row[0] for row in cur.fetchall()}
            cur.close()
            conn.close()
            return seen
        except Exception as e:
            print(f"Postgres error, falling back: {e}", file=sys.stderr)
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return set(data.get("seen_models", []))
        except Exception:
            pass
    return set()


def save_seen_models(models: Set[str]):
    if USE_POSTGRES:
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute("DELETE FROM models")
            for m in models:
                cur.execute(
                    "INSERT INTO models (model_id, name) VALUES (%s, %s) ON CONFLICT (model_id) DO NOTHING",
                    (m, m))
            conn.commit()
            cur.close()
            conn.close()
            return
        except Exception as e:
            print(f"Postgres save error: {e}", file=sys.stderr)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"seen_models": sorted(list(models))[-5000:], "last_run": datetime.now().strftime("%Y-%m-%d")}
    STATE_FILE.write_text(json.dumps(data, indent=2))


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
            "_ftjob_", "-merged", ".onnx",
            "-distilled", "-distill", "_distilled", "_distill",
            "moved", "deprecated", "archived", "old", "backup",
            "_length", "stella", "text2sql", "_calculator",
            "_seed", "_bs", "_epoch", "_step", "_checkpoint",
            "-finetuned", "-finetune", "_finetuned",
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
        import re as _re
        if _re.search(r'-base$', model_lower) and not _re.search(r'\d-base$', model_lower):
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
        "command-r", "codestral",
        "nvidia/llama", "nemotron", "granite",
        "olmo", "pythia", "glm-", "glm5", "glm-5", "glm-4.7",
        "grok", "grok-4",
        "claude", "gpt-4", "gpt-4o", "o1-", "o3-",
        "gemini-", "gemini2", "gemini3", "gemini-3",
        "arcee", "minimax", "voxcpm",
        "deepseek-v3", "deepseek-v4", "deepseek-ocr",
        "minicpm", "supertonic", "supertone",
        "sulphur", "hidream", "zamba", "zaya",
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
        resp = requests.get("https://openrouter.ai/api/v1/models", timeout=30)
        resp.raise_for_status()
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
                from datetime import timezone
                rd = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d")
            else:
                rd = datetime.now().strftime("%Y-%m-%d")

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
        resp = requests.get("https://ollama.com/library", timeout=30)
        resp.raise_for_status()
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
    "moonshotai", "bytedance-seed", "inclusionai", "ibm",
    "allenai", "amazon", "perplexity-ai", "stabilityai",
    "HiDream-ai", "SulphurAI", "Zyphra",
    "circlestone-labs", "Supertone",
    "TencentARC", "ResembleAI", "ADSKAILab", "open-thoughts",
]


def fetch_org_models(author: str) -> List[ModelRelease]:
    """Fetch models from a specific HF org, sorted by recent."""
    models = []
    try:
        resp = requests.get(
            f"https://huggingface.co/api/models?author={author}&sort=lastModified&direction=-1&limit=10",
            timeout=20)
        resp.raise_for_status()
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

            rd = m.get("createdAt", "")[:10] or datetime.now().strftime("%Y-%m-%d")
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
        resp = requests.get(
            "https://huggingface.co/api/models"
            "?pipeline_tag=text-generation&sort=downloads&direction=-1&limit=30",
            timeout=20)
        resp.raise_for_status()
        for m in resp.json():
            model_id = m.get("id", "")
            author = m.get("author", "")
            tags = m.get("tags", [])
            downloads = m.get("downloads", 0) or 0
            likes = m.get("likes", 0) or 0

            if is_noise_model(model_id, author, tags, downloads, likes):
                continue

            rd = m.get("createdAt", "")[:10] or datetime.now().strftime("%Y-%m-%d")
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
        resp = requests.get("https://huggingface.co/api/trending", timeout=30)
        resp.raise_for_status()
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

            rd = m.get("createdAt", "")[:10] or datetime.now().strftime("%Y-%m-%d")
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
        resp2 = requests.get(
            "https://huggingface.co/api/models",
            params={"sort": "lastModified", "direction": -1, "limit": 50},
            timeout=30)
        resp2.raise_for_status()
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

            rd = m.get("createdAt", "")[:10] or datetime.now().strftime("%Y-%m-%d")
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
               "gemma-2-27b", "gemma-4", "gemma-3", "command-r-plus", "nemotron",
               "sulphur", "minicpm", "zaya", "glm-5", "glm-4.7",
               "minimax", "grok"]
    closed = ["gpt-4", "claude-3", "claude-4", "claude-opus-4", "o1-", "o3-", "gemini-1.5", "gemini-2", "gemini-3", "grok-4",
              "kimi"]
    reasoning = ["reasoning", "r1", "o1", "o3"]
    coding = ["codestral", "coder", "code-", "claude-3.5", "devstral"]
    image_gen = ["dall-e", "flux", "stable-diffusion", "midjourney", "wan2", "pixal"]
    audio = ["lyria", "supertone", "supertonic", "dramabox"]

    if any(p in name for p in premier) or provider in ["meta", "mistral ai", "alibaba"]:
        if "closed" not in traits and model.is_open_source is not False:
            return "premier_open"
    if any(c in name for c in closed) or provider in ["openai", "anthropic", "google"]:
        return "closed_giants"
    if any(r in name for r in reasoning):
        return "reasoning"
    if any(c in name for c in coding):
        return "coding"
    if any(i in name for i in image_gen):
        return "image_gen"
    if any(a in name for a in audio):
        return "audio"
    # Known significant orgs always get meaningful categorization
    sig_org_map = {"tencentarc": "image_gen", "resembleai": "audio", "adskailab": "other",
                   "open-thoughts": "reasoning", "deepseek-ai": "premier_open"}
    if provider in sig_org_map:
        cat = sig_org_map[provider]
        return cat
    # Reasoning/training data orgs
    if provider == "open-thoughts":
        return "reasoning"
    if model.source == "ollama":
        return "local_ready"
    # Give high-engagement unknown orgs a shot at being shown
    if getattr(model, "likes", 0) >= 500 or getattr(model, "downloads", 0) >= 50000:
        return "other"
    return "other"


def build_digest_message(models: List[ModelRelease]) -> str:
    """Build tiered digest message (HTML format)."""
    if not models:
        return "No new models today."

    # Deduplicate by base name
    seen = set()
    deduped = []
    for m in models:
        base = m.name.split("/")[-1].lower().replace(":free", "").replace("-latest", "")
        if base not in seen:
            seen.add(base)
            deduped.append(m)
    models = deduped[:20]

    tiers = {"premier_open": [], "closed_giants": [], "reasoning": [],
             "coding": [], "image_gen": [], "audio": [], "local_ready": [], "other": []}
    for m in models:
        tiers[categorize_model(m)].append(m)

    lines = [
        f"🤖 <b>ModelBytes Digest</b>",
        f"<i>{datetime.now().strftime('%A, %B %d, %Y')}</i>",
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
            if m.release_date and m.release_date != datetime.now().strftime("%Y-%m-%d"):
                specs.append(f"Released: {m.release_date}")
            if m.context_window:
                specs.append(f"Context: {_format_context(m.context_window)}")
            if m.pricing_input is not None and m.pricing_input > 0:
                specs.append(f"${m.pricing_input:.2f}/${m.pricing_output:.2f} per 1M")
            elif m.pricing_input == 0:
                specs.append("FREE")
            if specs:
                lines.append(f"  {' | '.join(specs)}")
            if m.url:
                lines.append(f"  🔗 {m.url}")
            lines.append("")

    _section("PREMIER OPEN WEIGHTS", "🔓", tiers["premier_open"])
    _section("CLOSED GIANTS", "🔒", tiers["closed_giants"])
    _section("SPECIALIZED", "🎯", tiers["reasoning"] + tiers["coding"])
    _section("MULTIMODAL", "🎨", tiers["image_gen"] + tiers["audio"])
    _section("LOCAL READY", "🏠", tiers["local_ready"])

    if tiers["other"]:
        lines.extend(["", "━━━ <b>ALSO TRACKED</b>", ""])
        for m in tiers["other"][:5]:
            lines.append(f"  • {m.name.split('/')[-1]} ({m.source})")
        lines.append("")

    total = len(models)
    lines.extend(["", f"Total: {total} models tracked today"])
    return "\n".join(lines)


def summarize_models(models: List[ModelRelease]) -> str:
    """Use LLM for concise digest if key available."""
    if not models:
        return "No new models today."

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
        if m.pricing_input is not None:
            s += f"\nPricing: {'FREE' if m.pricing_input == 0 else f'${m.pricing_input:.2f}/${m.pricing_output:.2f} per 1M'}"
        if m.url:
            s += f"\nURL: {m.url}"
        info.append(s)

    prompt = f"""You are ModelBytes, an AI model tracker. Write a SHORT Telegram digest.

FORMAT:
<b>🔓 Premier Open</b>
<b>Model Name</b> — Released Apr 12. 2 sentences on why it matters. Specs. <a href="URL">→ OpenRouter</a>

<b>🔒 Closed Giants</b>
(same format)

<b>🎯 Specialized</b>
(same format)

<b>🏠 Local Ready</b>
• model-name — ollama run model-name

RULES:
- ONLY HTML tags: <b>, <i>, <a href>
- One line per model, no bullets except Local Ready
- Release date as "Released Apr 7" (no year)
- Link as <a href="URL">→ Source</a>
- 2 sentences max per model
- SKIP: fine-tunes, ONNX, LoRA, GGUF, embedders, experiments, distilled, personal merges
- No filler verbs: explores, reveals, highlights, offering, showcases, demonstrates, unpacks, breaks down, dives into, worth watching, notable, gaining traction
- HIDE empty sections
- Deduplicate across platforms
- MAX 2800 chars
- End: "X models tracked today"
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
            "max_tokens": 1200,
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
        header = f"🤖 <b>ModelBytes Digest</b>\n<i>{datetime.now().strftime('%A, %B %d, %Y')}</i>"
        return f"{header}\n\n{summary}"
    except Exception as e:
        print(f"LLM failed: {e} — falling back to template")
        return build_digest_message(models)


def send_telegram_post(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        print("Telegram not configured", file=sys.stderr)
        return False
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


def main():
    preview_mode = "--preview" in sys.argv
    if preview_mode:
        sys.argv.remove("--preview")

    init_database()
    seen_models = load_seen_models()
    today = datetime.now().strftime("%Y-%m-%d")
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

    print(f"Found {len(all_new)} new model(s)")

    if is_first_run:
        print("First run — seeding, no digest sent")
        # Seed all current models so they won't be reported as "new" next time
        for m in all_new:
            seen_models.add(m.name)
        save_seen_models(seen_models)
        return 0

    significant = []
    if all_new:
        # Apply noise filter with engagement data
        for m in all_new:
            base = m.name.split("/")[-1]
            author = m.name.split("/")[0] if "/" in m.name else ""
            if not is_noise_model(base, m.provider or "", m.unique_traits or [],
                                  getattr(m, "downloads", 0),
                                  getattr(m, "likes", 0)):
                significant.append(m)

        # If nothing significant, also try with author from name for HF/ORT fallback
        if not significant and len(all_new) <= 10:
            significant = all_new
        elif not significant:
            significant = all_new[:5]

        digest_models = significant[:15] if significant else all_new[:10]
        print(f"Filtered to {len(digest_models)} significant model(s)")

        # Mark posted models as seen so they don't re-appear
        for m in digest_models:
            seen_models.add(m.name)

        # When a large batch is found, also mark noise models as seen
        # to avoid re-scanning hundreds of unknown org repos every run
        if len(all_new) > 10:
            for m in all_new:
                seen_models.add(m.name)

        message = summarize_models(digest_models)

        if preview_mode:
            print("=== PREVIEW ===")
            print(message)
            print(f"=== END ({len(message)} chars) ===")
            print("Preview mode — not sending")
            return 0

        if not send_telegram_post(message):
            return 1
        print("Digest sent")

    else:
        print("No new models")

    # Small batches with no significant models: mark all as seen so
    # we don't keep re-discovering the same noise every run
    if all_new and not significant:
        for m in all_new:
            seen_models.add(m.name)

    save_seen_models(seen_models)
    return 0


if __name__ == "__main__":
    sys.exit(main())

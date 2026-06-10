# ModelBytes

AI model release monitor for Telegram. Tracks new models from OpenRouter, Ollama, and Hugging Face, then posts daily curated digests to the [@ModelBytes](https://t.me/ModelBytes) channel at 16:00 UTC.

## Architecture (v2)

A small Python service on Railway plus a set of scheduled Claude routines (running on Claude.ai subscription, no API costs) that handle editorial taste, organic growth, and health checks:

- **`monitor.py`** — the deterministic core. Fetches, filters, categorizes, posts. Always runs as the safety net.
- **`modelbytes-curator-routine`** (15:30 UTC daily) — generates the editorial digest with taste, writes `pending/<TODAY>.txt` to master; Railway reads + posts it at 16:00 UTC.
- **`modelbytes-supervisor-routine`** (14:00 UTC daily) — audits the system + grows it organically. Auto-commits list additions when bootstrapped; opens PRs for logic changes; opens issues for ambiguous calls.
- **`modelbytes-daily-health`** (17:00 UTC daily) — verifies the post landed.
- **`modelbytes-pr-curator`** (hourly) — reviews open PRs.

See [`docs/architecture.md`](./docs/architecture.md) for the full design and [`docs/operations.md`](./docs/operations.md) for runbooks (rotating the bot token, pausing supervisor autonomy, manually triggering a post, etc.). The digest format (identity tiers + availability tags) is specified in [`docs/superpowers/specs/2026-06-10-builder-digest-format-v3-design.md`](./docs/superpowers/specs/2026-06-10-builder-digest-format-v3-design.md). [`docs/vm-deployment.md`](./docs/vm-deployment.md) is a retired deployment path kept for reference; [`docs/structured-data.md`](./docs/structured-data.md) covers the Postgres-first data roadmap.

## Digest format (v3)

Identity tiers say what kind of model it is; a per-entry tag says how you can use it today:

- 🔓 **OPEN FRONTIER** / 🔒 **CLOSED FRONTIER** / 🎯 **SPECIALIZED** / 🏠 **LOCAL** / 👀 **WATCH**
- Every entry: bold name → *italic differentiator sentence* → hard facts → ⚡ API / 📦 weights availability tag → source link
- Lifecycle moves count: weights landing (WATCH graduations), big price changes, Ollama/OpenRouter arrivals
- Footer: `Total: N items tracked today`

## Features

- 🔓/🔒 Open source vs proprietary classification
- ⭐ High-performance model detection
- ✨ Unique trait tagging (long_context, reasoning, multimodal, MoE)
- 📊 Benchmark scores when available
- 💸 Pricing info for API models
- 🗄️ PostgreSQL state persistence (required — set DATABASE_URL)
- 🚦 Duplicate-post protection via a `posted_digests` ledger
- 🔁 Retrying source fetches with a consistent ModelBytes user agent
- 🤖 Claude-curated editorial digests with daily organic growth via the supervisor routine

## Deploy to Railway

(No public template — set up manually via the steps below.)

### Manual Setup

1. **Create Railway project**
2. **Add PostgreSQL** (Railway provides `DATABASE_URL`)
3. **Set environment variables:**
   - `TELEGRAM_BOT_TOKEN` — From @BotFather
   - `TELEGRAM_CHANNEL_ID` — Your channel ID (use @getidsbot to find it)
4. **Deploy**

## Local Development

```bash
# Clone
git clone https://github.com/SovereignSignal/modelbytes.git
cd modelbytes

# Setup
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Create .env
cp .env.example .env
# Edit .env with your tokens

# Run
python3 monitor.py
```

## Running Tests

```bash
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest tests/ -v
```

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | ✅ |
| `TELEGRAM_CHANNEL_ID` | Telegram channel ID | ✅ |
| `DATABASE_URL` | PostgreSQL connection string (Railway auto-sets; required for posting — `--preview` mode runs without it) | ✅ |
| `MODELBYTES_LLM_KEY` | API key for the deterministic-fallback LLM summarization step (OpenAI-compatible). Falls back to `OPENAI_API_KEY` then `OPENROUTER_API_KEY` if unset. Without any of these, the fallback path produces a template-only digest (no LLM editorial). | ❌ |
| `MODELBYTES_LLM_MODEL` | Model name for fallback summarization. Default: `gpt-4o-mini`. | ❌ |
| `MODELBYTES_LLM_URL` | API base URL. Default: `https://api.openai.com/v1`. Set to OpenRouter or another OpenAI-compatible endpoint to switch providers. | ❌ |
| `MODELBYTES_HTTP_RETRIES` | Source fetch attempts for transient failures. Default: `3`. | ❌ |
| `MODELBYTES_HTTP_BACKOFF_SECONDS` | Base retry delay for source fetches. Default: `1.0`. | ❌ |
| `MODELBYTES_USER_AGENT` | User-Agent sent to model source APIs. Default identifies ModelBytes. | ❌ |

The fallback LLM path only runs when the daily curator routine hasn't produced today's `pending/<TODAY>.txt` (rare). The primary editorial layer is Claude via the [curator routine](./docs/architecture.md), which doesn't use these env vars.

## Sources

- **OpenRouter** — 400+ models with pricing
- **Ollama** — Local LLM models
- **Hugging Face** — Open weights and research models

See [`docs/source-growth.md`](./docs/source-growth.md) for the source expansion rubric and candidate pipeline.

## License

MIT

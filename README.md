# ModelBytes

AI model release monitor for Telegram. Tracks new models from OpenRouter, Ollama, and Hugging Face, then posts daily curated digests to the [@ModelBytes](https://t.me/ModelBytes) channel at 16:00 UTC.

## Architecture (v3 — inline primary)

A small Python service on Railway. **No claude.ai / Claude Code dependency** — editorial taste is produced inline by an OpenAI-compatible writer model, grounded by cited web research:

- **`monitor.py`** — the publisher. A daily 16:00 UTC Railway cron that fetches OpenRouter / Ollama / HuggingFace, filters (`is_noise_model` / `is_significant_release` / `is_stale_release`), dedupes vs Postgres, collapses same-family variants, enriches from HF model cards, then has the writer model emit a format-v3 digest and posts it to Telegram + Slack.
- **Inline writer** — `MODELBYTES_LLM_MODEL` (production: `deepseek-v4-pro` on Ollama Cloud) with `MODELBYTES_LLM_MODEL_FALLBACK` (`gpt-oss:120b`). Grounded by **Parallel.ai web research** (`MODELBYTES_PARALLEL_API_KEY`) so it writes from cited sources, not training knowledge.
- **Content gate** — `validate_digest_for_publish` rejects anything that would harm the channel (stray `<`, unbalanced tags, floods, stale dates) before it reaches Telegram.
- **`pending/<TODAY>.txt`** — a write-back cache of what was published, read by tomorrow's cross-day fact-consistency check. (Earlier docs describe a claude.ai "curator" that wrote this file; **that layer is retired** — see `docs/architecture.md` § "How we got here".)

See [`docs/architecture.md`](./docs/architecture.md) for the full design and [`docs/operations.md`](./docs/operations.md) for runbooks (rotating the bot token, manually triggering a post, reading `publish_runs`). The digest format (identity tiers + availability tags) is specified in [`docs/superpowers/specs/2026-06-10-builder-digest-format-v3-design.md`](./docs/superpowers/specs/2026-06-10-builder-digest-format-v3-design.md). [`docs/vm-deployment.md`](./docs/vm-deployment.md) and [`docs/structured-data.md`](./docs/structured-data.md) cover a retired deployment path and the Postgres-first data roadmap.

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
- 🤖 Inline editorial digest via an OpenAI-compatible writer model (Ollama Cloud) grounded by Parallel.ai cited web research — no claude.ai / Anthropic dependency
- 🛡️ Reliability: every run is recorded in a `publish_runs` audit table, and failures or degradation alert the operator (Telegram DM, Slack fallback). The inline writer is the everyday path; `pending/<TODAY>.txt` is a write-back cache, not a deploy-timed handoff.

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
| `MODELBYTES_ADMIN_CHAT_ID` | Telegram chat to DM on publish failures or degradation (operator alerts). | ❌ |
| `MODELBYTES_OPS_SLACK_CHANNEL_ID` | Slack channel used as a fallback for operator alerts when the Telegram DM can't be reached. | ❌ |
| `MODELBYTES_HEARTBEAT_URL` | Dead-man's-switch ping URL (e.g. healthchecks.io) — the only signal that catches "the cron never fired". | ❌ |
| `MODELBYTES_PENDING_GRACE_SECONDS` | How long the publisher waits for a late curator digest before falling back. Default: `600`. | ❌ |
| `MODELBYTES_ALLOW_SEED` | Set to `1` to let the fallback path seed an empty `models` table (otherwise it refuses, to guard wiped/migrated state). | ❌ |

These power the **inline writer**, which is the everyday digest path (the retired claude.ai curator layer did not use them). The writer has a primary + fallback model; if the primary (`MODELBYTES_LLM_MODEL`) returns empty, it tries `MODELBYTES_LLM_MODEL_FALLBACK` and alerts the operator that the primary was unavailable.

## Sources

- **OpenRouter** — 400+ models with pricing
- **Ollama** — Local LLM models
- **Hugging Face** — Open weights and research models

See [`docs/source-growth.md`](./docs/source-growth.md) for the source expansion rubric and candidate pipeline.

## License

MIT

# ModelBytes

AI model release monitor for Telegram. Tracks new models from OpenRouter, Ollama, and Hugging Face, then posts daily digests to @ModelBytes channel.

## Features

- 🔓/🔒 Open source vs proprietary classification
- ⭐ High-performance model detection
- ✨ Unique trait tagging (long_context, reasoning, multimodal, MoE)
- 📊 Benchmark scores when available
- 💸 Pricing info for API models
- 🗄️ PostgreSQL for state persistence

## Deploy to Railway

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/template/YOUR_TEMPLATE_ID)

### Manual Setup

1. **Create Railway project**
2. **Add PostgreSQL** (Railway provides `DATABASE_URL`)
3. **Set environment variables:**
   - `TELEGRAM_BOT_TOKEN` — From @BotFather
   - `TELEGRAM_CHANNEL_ID` — Your channel ID (e.g., `-1003509386035`)
4. **Deploy**

## Local Development

```bash
# Clone
git clone https://github.com/ClawBack1/modelbytes.git
cd modelbytes

# Setup
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Create .env
cp .env.example .env
# Edit .env with your tokens

# Run
python3 monitor.py --post
```

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | ✅ |
| `TELEGRAM_CHANNEL_ID` | Telegram channel ID | ✅ |
| `DATABASE_URL` | PostgreSQL connection string | ✅ (Railway auto-sets) |
| `POST_IMMEDIATELY` | If "true", posts on first run | ❌ |

## Sources

- **OpenRouter** — 400+ models with pricing
- **Ollama** — Local LLM models
- **Hugging Face** — Open weights and research models

## License

MIT
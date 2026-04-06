# ModelBytes

AI model release monitor for Telegram. Tracks new models from OpenRouter, Ollama, and Hugging Face, then posts daily digests to @ModelBytes channel.

## Features

- 🔓/🔒 Open source vs proprietary classification
- ⭐ High-performance model detection (GPT-4, Claude-3, Gemini, Llama-3.3, etc.)
- ✨ Unique trait tagging (long_context, reasoning, multimodal, MoE)
- 📊 Benchmark scores when available
- 💸 Pricing info for API models

## Sources

- **OpenRouter** - 400+ models with pricing and context windows
- **Ollama** - Local LLM models
- **Hugging Face** - Open weights and research models

## Setup

```bash
# Clone
git clone https://github.com/ClawBack1/modelbytes.git
cd modelbytes

# Install deps
pip install -r requirements.txt

# Run (requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID env vars)
python3 monitor.py --post
```

## Environment

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHANNEL_ID` | Channel ID (e.g., `-1003509386035`) |

## Cron

Daily run at 9 AM PT:
```bash
0 16 * * * cd /opt/modelbytes && TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHANNEL_ID=xxx python3 monitor.py --post
```

## License

MIT
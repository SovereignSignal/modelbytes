# ModelBytes — working notes for Claude

A daily curated digest of notable AI model releases, posted to the public
Telegram channel **@ModelBytes** (and mirrored to Slack) at **16:00 UTC**. The
editorial signal is the product: what a builder/researcher should know about
today, organized into identity tiers and written to be self-explanatory.

> This repo is **public** (MIT). Never commit real secrets, channel/chat IDs,
> Railway project IDs, or internal hostnames — use placeholders
> (`-100XXXXXXXXXX`, `C0XXXXXXXXX`, `<railway-project-id>`). History has been
> scrubbed once; keep it clean.

## Architecture — three layers, best-effort degradation

1. **Curator** (`docs/curator-prompt.md`) — a scheduled claude.ai routine
   (sonnet, daily 15:30 UTC, HuggingFace connector + web search). Researches
   the day's model news, writes a Telegram-HTML digest to
   `pending/<YYYY-MM-DD>.txt`, and commits it to `master`. This is the quality
   path. The routine prompt is the source of truth for **format v3** (see
   `docs/superpowers/specs/2026-06-10-builder-digest-format-v3-design.md`).

2. **Publisher** — `monitor.py`, runs on **Railway** as a daily 16:00 UTC cron.
   - Reads the curated digest **from GitHub raw first** (master is the source of
     truth; the Railway image is stale by construction because auto-deploy does
     not fire on curator pushes), then the baked-in local copy, then a ~10-min
     grace window for a late curator.
   - Validates (`validate_digest_for_publish`), fixes the dateline, posts to
     Telegram + Slack, records a `posted_digests` row (idempotency) and a
     `publish_runs` row (audit).
   - If no curated digest exists: the **deterministic fallback** runs — fetch
     OpenRouter/Ollama/HF, `is_noise_model`/`is_significant_release`/
     `categorize_model`, dedupe vs the `models` table, then `summarize_models()`
     (GLM via Tenspire) or the bare `build_digest_message()` template.

3. **Governance routines** (claude.ai cron) — supervisor (14:00 UTC,
   auto-commits org/tier-list additions when `.supervisor-bootstrapped` exists),
   pr-curator (hourly), daily-health (17:00 UTC, **currently disabled** — see
   below).

## Ops / observability layer (added 2026-06-12)

The publisher tells on itself. Every run records a `publish_runs` row (posted,
blocked, failed, no-models, seeded) and routes problems to the operator:

- `send_ops_alert()` → Telegram DM (`MODELBYTES_ADMIN_CHAT_ID`), falling back to
  a Slack ops channel (`MODELBYTES_OPS_SLACK_CHANNEL_ID`). Isolated try-blocks so
  a Telegram outage still alerts via Slack.
- `ping_heartbeat()` → optional dead-man's switch (`MODELBYTES_HEARTBEAT_URL`,
  e.g. healthchecks.io) — the only thing that catches "cron never fired".
- Alerts fire on: fallback days, blocked/failed publishes, late curator, crash,
  lost `DATABASE_URL`, and content-damage QA warnings (fact drift, floods).
  Cosmetic format-drift warnings do **not** alert (anti-noise).

## Content gates — `validate_digest_for_publish(body, mode)`

The contract: **ERROR only for channel-harm** (HTML Telegram would 400 on,
unbalanced tags, fallback floods/quant-leaks/stale dates, empty body). Format
drift is a **WARNING** — blocking a curated digest publishes the *worse*
fallback instead, so the bar to block curated content is high. Also runs a
cross-day fact-consistency check (catches a param/price silently changing from
a figure published in the last 14 days) and a deterministic dateline rewrite.

## Hard rules

1. **Railway only.** The old self-managed VM (`claw-content-engine`) is retired.
   Never reach for it or the EDIN-VPN tunnel for modelbytes — all ops go through
   Railway (CLI / `railway run`). See `docs/operations.md`.
2. **TDD.** `monitor.py` has a real suite (`tests/`, ~98 tests incl. golden
   tests on the live `pending/*.txt` corpus). Write the failing test first;
   `python3 -m pytest tests/ -q` must stay green. `tests/conftest.py` blanks all
   network/alert/DB side-effects so the suite is safe even with prod env vars.
3. **Curator vs fallback parity.** Both authors share one taxonomy
   (`categorize_model` → open_frontier/closed_frontier/specialized/local/other;
   WATCH is curator-only). When changing the format, change the prompt *and* the
   linter (they are not yet a single source — a known follow-up).
4. **The supervisor edits production lists autonomously.** Keep its blast radius
   small; treat org-list additions as decisions needing corroborating signals.

## Key files

- `monitor.py` — the entire publisher (single file, ~2200 lines).
- `docs/curator-prompt.md` — the live curator routine prompt (keep in sync).
- `docs/architecture.md` — full design; `docs/operations.md` — runbooks;
  `docs/structured-data.md` — the Postgres tables.
- `docs/superpowers/specs/` — design specs (format v3 + the 2026-06-12 review
  follow-ups).
- `pending/<date>.txt` — the curated digest handoff files (also the dedup/
  graduation memory the curator re-reads).

## Conventions

- Commit messages end with the `Co-Authored-By: Claude` trailer.
- Routine prompts are managed via the `schedule` skill / RemoteTrigger; the
  canonical prompt copy lives in `docs/curator-prompt.md`.
- Operational truths that aren't derivable from the code live in Claude's
  auto-memory (the `current-state-*` note is the Read-First).

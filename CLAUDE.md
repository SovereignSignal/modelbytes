# ModelBytes — working notes for Claude

A daily curated digest of notable AI model releases, posted to the public
Telegram channel **@ModelBytes** (and mirrored to Slack) at **16:00 UTC**. The
editorial signal is the product: what a builder/researcher should know about
today, organized into identity tiers and written to be self-explanatory.

> This repo is **public** (MIT). Never commit real secrets, channel/chat IDs,
> Railway project IDs, or internal hostnames — use placeholders
> (`-100XXXXXXXXXX`, `C0XXXXXXXXX`, `<railway-project-id>`). History has been
> scrubbed once; keep it clean.

## Architecture — inline-primary, one system, best-effort degradation

**One publisher:** `monitor.py`, a daily **16:00 UTC** Railway cron. There is
**no claude.ai / Claude Code dependency** in the publish path. Editorial taste
is produced **inline** by an OpenAI-compatible writer model (production:
`MODELBYTES_LLM_MODEL` = `deepseek-v4-pro` on Ollama Cloud, with
`MODELBYTES_LLM_MODEL_FALLBACK` = `gpt-oss:120b`) grounded by **Parallel.ai web
research** (`MODELBYTES_PARALLEL_API_KEY`). This is the everyday path, not a
degraded fallback — set `MODELBYTES_INLINE_PRIMARY=1` (it is, in prod) so the
publisher treats an inline day as normal and does not alert "curator absent".

The daily run, in `main()`:
1. Resolve the day's model set: fetch OpenRouter / Ollama / HuggingFace
   (trending + major orgs + top text-gen), `is_noise_model` /
   `is_significant_release` / `is_stale_release` filters, dedupe vs the Postgres
   `models` table.
2. `discover_recent_releases()` — Parallel.ai cited web research; the freshness
   engine that keeps the channel alive even when the registries are quiet.
3. `collapse_variants()` — group same-(org, base, size) variants; N≥3 collapse
   to one family entry so one org's batch can't spam the digest.
4. `enrich_with_hf_cards()` — pull real params/license/context/benchmarks from
   HF model cards so the writer works from specs, not training knowledge.
5. `summarize_models()` — the writer model emits format-v3 Telegram HTML.
6. `validate_digest_for_publish(body, mode='fallback')` content gates → post to
   Telegram + Slack mirror → record `posted_digests` (idempotency) and a
   `publish_runs` row (audit) → `ping_heartbeat()`.

> **Historical: the claude.ai curator layer is RETIRED** (2026-06). Earlier
> docs and audit plans describe a three-routine claude.ai layer (curator /
> supervisor / daily-health) writing `pending/<TODAY>.txt`. That design was
> replaced by the inline writer above. `pending/*.txt` is now only a write-back
> cache of what was published (for the cross-day fact-consistency check), and
> `docs/curator-prompt.md` + `.supervisor-bootstrapped` + the
> `modelbytes-curator-routines.md` memory are **stale artifacts**, not live
> config. Do not "restore the curator" — it is intentionally gone. See
> `docs/architecture.md` § "How we got here".

The publisher still **reads `pending/<TODAY>.txt` if one is present** (GitHub
raw first, then baked-in, then a grace window) — so a hand-written or
> externally-produced digest still wins. But nothing produces that file in the
> normal flow anymore; the inline path is the default.

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
2. **No claude.ai / Claude Code dependency.** The publish path is inline
   (writer model + Parallel.ai). Do not reintroduce a claude.ai routine,
   RemoteTrigger, or Anthropic API call into the publish flow. `INLINE_PRIMARY=1`
   is the production setting.
3. **TDD.** `monitor.py` has a real suite (`tests/`, ~188 tests incl. golden
   tests on the live `pending/*.txt` corpus). Write the failing test first;
   `python3 -m pytest tests/ -q` must stay green. `tests/conftest.py` blanks all
   network/alert/DB side-effects so the suite is safe even with prod env vars.
4. **Shared publish core.** Telegram send / Slack mirror / ops-alert routing
   live in the vendored `ss_publish/` package (shared with clawbytes). Edit the
   canonical copy at `repos/ss-publish/` and copy into both repos — the
   `test_ss_publish_sync.py` guard fails if they drift.
5. **The inline writer can hallucinate.** The content gate
   (`validate_digest_for_publish`) is the safety net: it rejects stray `<` in
   prose, unbalanced tags, floods, and stale dates before they reach Telegram.
   When changing what specs the writer sees (`enrich_with_hf_cards`,
   `_param_size_from_name`), add a gate test — the writer will invent specs when
   fed `unknown`.

## Key files

- `monitor.py` — the entire publisher (single file, ~2,700 lines).
- `ss_publish/` — vendored shared publish core (Telegram/Slack/ops); see the
  sync guard `tests/test_ss_publish_sync.py`.
- `docs/architecture.md` — full design (read the "How we got here" note on the
  retired curator layer); `docs/operations.md` — runbooks;
  `docs/structured-data.md` — the Postgres tables.
- `docs/superpowers/specs/` — design specs (format v3 + the 2026-06-12 review
  follow-ups).
- `pending/<date>.txt` — write-back cache of what was published (feeds the
  cross-day fact-consistency check). No longer a curator handoff.
- `docs/curator-prompt.md`, `.supervisor-bootstrapped` — **stale artifacts**
  from the retired claude.ai layer; kept for history, not live config.

## Conventions

- Commit messages end with the `Co-Authored-By: Claude` trailer.
- Operational truths that aren't derivable from the code live in Claude's
  auto-memory (the `current-state-*` note is the Read-First).

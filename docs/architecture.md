# ModelBytes Architecture

This document describes how ModelBytes works end-to-end as of v2 (2026-05-21, digest format v3 2026-06-10). For runbook-style operations (token rotation, supervisor pause/resume, manual triggers), see [`operations.md`](./operations.md). For the digest format, see the spec at [`superpowers/specs/2026-06-10-builder-digest-format-v3-design.md`](./superpowers/specs/2026-06-10-builder-digest-format-v3-design.md). The self-managed VM deployment path ([`vm-deployment.md`](./vm-deployment.md)) is **retired** as of 2026-06-08 — production runs on Railway only; the doc is kept as reference. For the audit-fix history that got us here, see the plan files in `docs/superpowers/plans/`.

## What ModelBytes is

A daily curated digest of notable AI model news, posted to the public Telegram channel [@ModelBytes](https://t.me/ModelBytes) (and mirrored to a Slack channel when configured) at 16:00 UTC. The editorial signal is the product — what a builder/researcher should know about today, drawn from OpenRouter, Ollama, and HuggingFace plus the curator's own research, filtered for taste and organized into format v3's identity tiers: OPEN FRONTIER 🔓 / CLOSED FRONTIER 🔒 / SPECIALIZED 🎯 / LOCAL 🏠 / WATCH 👀, each entry leading with an italicized differentiator sentence and carrying a ⚡/📦 availability tag. Lifecycle moves (weights landing, price cuts, platform arrivals) are first-class items, with WATCH→shipped graduations threading one day's digest to the next. A one-line lead "Take" sets the day's tone.

## Two systems, one repo

**Deterministic core** — `monitor.py` in the repo root. The publisher. At post time it resolves the curated digest (GitHub raw first, then the baked-in image copy, then a grace window — see below) and publishes it; only if no curated digest exists does it run the heuristic fallback pipeline (`is_noise_model` / `is_significant_release` / `categorize_model`, dedupe against Postgres). This is the safety net — it must keep posting even when the Claude layer is unavailable.

**Claude layer** — a set of scheduled routines on Claude.ai (no Anthropic API costs; uses the existing subscription via the Anthropic-hosted CCR environment) that handle editorial taste, growth, and health checks:

| Routine | Cadence | Role |
|---|---|---|
| `modelbytes-curator-routine` | Daily 15:30 UTC | Generates the day's editorial digest with taste. Writes `pending/<TODAY>.txt` to master. Replaces the previous OpenAI gpt-4o-mini summarization step. |
| `modelbytes-supervisor-routine` | Daily 14:00 UTC | Audits production state + grows the system organically. Auto-commits list additions (KNOWN_ORGS, PROVIDER_NAMES, etc.) when bootstrapped; opens PRs for logic changes; opens issues for ambiguous calls. |
| `modelbytes-daily-health` | Daily 17:00 UTC | **Currently DISABLED.** It fetched `t.me/s/ModelBytes` to confirm the day's post landed, but that endpoint 403-blocks datacenter IPs so it false-FAILed daily. A receipt-based replacement (the publisher already records `publish_runs` rows it could read) is designed but tabled. Until then, run-level monitoring is the ops layer below, not this routine. |
| `modelbytes-pr-curator` | Hourly | Auto-reviews any open PR that lacks a `🤖 Curator review:` comment. |

Routine IDs and URLs live in the auto-memory file `modelbytes-curator-routines.md`. Manage them at https://claude.ai/code/routines.

## The daily loop (autonomous, no human in the path)

```
14:00 UTC   modelbytes-supervisor-routine
            ├── audit recent posts, fetched data, GitHub issues
            ├── identify growth candidates (orgs/families) + drift indicators
            ├── if .supervisor-bootstrapped on master: auto-commit top 3 list additions
            └── record structured supervisor outcome

15:30 UTC   modelbytes-curator-routine
            ├── fetch sources (OpenRouter / Ollama / HuggingFace)
            ├── run is_noise_model + categorize_model filters
            ├── apply editorial pass (drop weak items, write blurbs, lead "Take" sentence)
            ├── format as Telegram HTML
            └── commit pending/<TODAY>.txt to master via gh

16:00 UTC   Railway cron
            ├── monitor.py runs
            ├── ensure posted_digests exists for duplicate-post protection
            ├── if posted_digests already has today: exit 0
            ├── try_post_pending_curated() resolves the curated digest:
            │     1. GitHub raw  pending/<TODAY>.txt  (master = source of truth)
            │     2. baked-in local copy              (GitHub-outage fallback)
            │     3. _wait_for_pending()              (grace window for a late curator)
            ├── _fix_dateline() → validate_digest_for_publish() content gates
            ├── if curated digest resolved + not blocked: post Telegram (+ message_id)
            │     + Slack mirror, record posted_digests + publish_runs, ping heartbeat, exit 0
            └── if missing/blocked/send-failed: fall through to deterministic fallback pipeline

17:00 UTC   modelbytes-daily-health  — DISABLED (false-FAILs on datacenter-IP 403s);
            run health now lives in the publish_runs ledger + ops alerts, not this routine.

Hourly      modelbytes-pr-curator
            └── review any open PR without a curator review comment
```

## File handoff: `pending/<TODAY>.txt`

The curator routine and the Railway publisher don't talk directly. They communicate via a file in the repo:

- Curator writes `pending/YYYY-MM-DD.txt` (UTC date) containing Telegram-ready HTML — `<b>`, `<i>`, `<a href>` only.
- Railway's `monitor.py::try_post_pending_curated()` first checks Postgres `posted_digests`. If today's date is already marked posted, it exits without sending anything.
- **Resolution order (master is the source of truth):** the publisher fetches the curated digest **from GitHub raw first** — the curator pushes to master and the Railway image is stale by construction, because auto-deploy does not fire on curator pushes. If GitHub raw is unavailable it uses the **baked-in local copy** (the GitHub-outage fallback), and if neither is present it opens a **grace window**, `_wait_for_pending()` polling up to `MODELBYTES_PENDING_GRACE_SECONDS` (default 600s = 10 min, poll interval `MODELBYTES_PENDING_POLL_SECONDS`) for a late-running curator.
- Once a non-empty body is resolved, `_fix_dateline()` deterministically rewrites the dateline to the actual UTC weekday (the curator wrote the wrong weekday on 2 of 3 early days), `validate_digest_for_publish()` runs the content gates (below), and the publisher posts it verbatim, captures the Telegram `message_id`, mirrors to Slack, and records both `posted_digests` and a `publish_runs` audit row. The published body is also written back to the local `pending/<TODAY>.txt` so tomorrow's cross-day fact check sees exactly what readers saw.
- Before any curated or fallback digest is posted, `validate_digest_for_publish(body, mode)` enforces a content-gate contract (not a light QA pass — see [Content gates](#content-gates) below). A QA **block** on the curated path sends an ops alert, records a `blocked` run, and falls through to the fallback rather than going dark.
- If no curated digest is resolved (curator failed or didn't run), `monitor.py` falls back to its deterministic pipeline (fetch → filter → categorize → dedupe vs `models` → `summarize_models()` → post). A successful fallback post also records today's date in `posted_digests` and a `publish_runs` row.

**Why this pattern**: claude.ai routines fire on cron, not on-demand. Inline Anthropic API calls from Railway would cost money. File handoff via the repo gets us "Claude-curated content in production" using only the Claude.ai subscription quota — at the cost of a 30-minute time gap (curator runs at 15:30, post is at 16:00).

**Duplicate protection**: `posted_digests` is the source of truth for whether a UTC date has already posted. This means same-day Railway redeploys, manual re-runs, and stale pending files do not post twice once the first successful send has been recorded. If Postgres is temporarily unavailable, the curated fast-path still tries to post so the channel does not go dark, but the log will say the idempotency ledger could not be checked or written.

**What the fallback looks like**: when no curated digest is resolved, `monitor.py` runs its full pipeline. The pipeline's final step is `summarize_models()`, which calls an OpenAI-compatible API to write the digest body in the same format-v3 entry grammar (differentiator sentence, hard facts, availability tag, link). The API endpoint and key are configured by `MODELBYTES_LLM_KEY` / `MODELBYTES_LLM_MODEL` / `MODELBYTES_LLM_URL` (production = GLM via Tenspire), with `OPENAI_API_KEY` and `OPENROUTER_API_KEY` as key fallbacks (see README). The summarizer request now allows `max_tokens` 8000 (was 3000, which truncated to an empty body twice). If those variables are removed or fail, `summarize_models()` returns early and the deterministic template-only `build_digest_message()` runs instead — the same v3 tier headers and a per-entry ⚡/📦 tag derived from the model's source, without the editorial blurbs. The same content gates apply to the fallback body before it posts (in `mode='fallback'`, which additionally errors on `DIGEST_LIMIT` floods, quant/serving artifacts, and stale release dates). Both authors share one taxonomy: `categorize_model()` returns `open_frontier` / `closed_frontier` / `specialized` / `local` / `other` (WATCH is curator-only, since the deterministic pipeline can't see announced-but-unshipped models).

## Content gates

`validate_digest_for_publish(body, mode='curated'|'fallback')` is a content-gate contract, not a light QA pass. It returns `(possibly-rewritten body, warnings, errors)`:

- **ERROR (blocks publish)** — channel-harm only: tags Telegram would 400 on, unbalanced markup, an empty body. In `mode='fallback'` it also errors on `DIGEST_LIMIT` floods, quant/serving artifacts, and stale release dates. On the curated path a block sends an ops alert, records a `blocked` run, and falls through to the fallback.
- **WARNING (publishes anyway)** — format drift: missing/odd tier header, an entry missing its italic differentiator or link, footer count mismatch, aggregator-sourced link, or no parseable dateline. Blocking a curated digest over drift would publish the *worse* fallback, so the bar to block curated content is deliberately high — drift is logged, and only the content-damage subset (fact drift, floods, quant leaks, stale/expiry) raises an ops alert.
- **Cross-day fact consistency** — flags a parameter count or price that changed versus the most-recent figure published across the last 14 pending files without an explicit correction marker (the check designed to catch the kind of MiniMax 229.9B→428B silent flip). `ModelFact` entries carry 45-day freshness windows (`_fact_active`) so stale correction regexes stop mutating unrelated copy.
- **Dateline rewrite** — `_fix_dateline()` runs first and deterministically rewrites the dateline to the actual UTC weekday before the gates see the body.

## The supervisor bootstrap gate

Auto-commit authority is opt-in via a marker file at the repo root:

- `.supervisor-bootstrapped` present on master → supervisor enters auto-commit mode (with caps: max 3 list-addition commits per run, plus issues/PRs for anything bigger).
- Absent → supervisor stays in propose-only mode (opens a GitHub issue with what it WOULD do).
- Reversing autonomy: `git rm .supervisor-bootstrapped && git push origin master`. Supervisor returns to propose-only on its next run, no routine config change needed.

This gate exists because the supervisor edits production code (`monitor.py`'s `KNOWN_ORGS`, `PROVIDER_NAMES`, etc.). Sov reviews the first proposal issue before turning on autonomous mode, so the supervisor's judgment has been sanity-checked once before any real commits land.

## Authority boundaries

The supervisor's prompt encodes a strict authority hierarchy:

| Change type | Action | Examples |
|---|---|---|
| **AUTO-COMMIT** (bootstrapped) | Direct push to master | Add author to `KNOWN_ORGS` / `MAJOR_HF_ORGS` / `PROVIDER_NAMES`; add family token to `significant_families`; add to a `categorize_model` tier list |
| **OPEN PR** | `gh pr create` | `is_noise_model` logic changes; threshold tweaks; new fetcher modules; schema changes |
| **OPEN ISSUE** | `gh issue create` | Deletions from any list; env var / Railway service changes; Telegram channel changes; ambiguous judgment calls |
| **NEVER** | — | Modify Telegram posts; touch other repos; change architecture via auto-commit |

The curator routine's prompt has its own narrower authority — it can drop / rewrite / reorder / reassign tier / edit the lead sentence within a daily bundle, but cannot add new models (it works only from what `monitor.py` fetched), change tier structure, skip posting, modify state, or self-promote.

## Ops / observability (added 2026-06-12)

The publisher tells on itself. Every run lands a `publish_runs` row and routes anything operator-actionable out of the logs:

- `record_publish_run(...)` — one `publish_runs` row per run with `mode`, `status` (`posted` / `blocked` / `send-failed` / `no-models` / `seeded`), `models_found` / `models_emitted`, `message_chars`, `telegram_message_id`, `slack_ok`, and `error`. Best-effort; never raises. "Why was yesterday weird" is a SQL query, not log archaeology. `fallback_streak()` reads these rows to drive escalating alerts when the curated path has been down for consecutive days.
- `send_ops_alert(text)` — operator notification. Routes to a private Telegram chat (`MODELBYTES_ADMIN_CHAT_ID`, live since 2026-06-12), falling back to a Slack ops channel (`MODELBYTES_OPS_SLACK_CHANNEL_ID`). The two delivery paths are isolated try-blocks, so a Telegram outage still reaches Slack. Never raises — a broken alert must never take down a publish.
- `ping_heartbeat(ok, msg)` — optional dead-man's switch. POSTs to `MODELBYTES_HEARTBEAT_URL` (e.g. healthchecks.io), hitting `/fail` on failure. It is the only signal that catches "the cron never fired at all". Currently unset.
- Alerts fire on: fallback days, blocked / send-failed publishes, a late or missing curator, lost `DATABASE_URL`, an unexpectedly empty `models` table, and content-damage QA warnings. Cosmetic format-drift warnings deliberately do **not** alert, so the operator never learns to ignore the channel.
- `_redact_secrets()` scrubs tokens and the DB URL from every log line and alert. The `__main__` crash handler alerts and pings the heartbeat with `/fail`, then re-raises so Railway still records the failed run.

## Live-mode guards

`main()` hardens the path so a wiped or misconfigured environment fails loud instead of silently skipping the day (exit 0 with nothing posted):

- **Lost `DATABASE_URL`** — alerts, then still attempts the curated fast-path (which needs no DB; ledger writes degrade to best-effort no-ops) rather than blocking a good digest over a missing env var. But it is **fatal (exit 1)** for the fallback pipeline, which genuinely needs the DB to dedupe — without it, `load_seen_models()` returns empty and every fallback day would re-detect "first run" and post nothing forever.
- **Empty `models` table** — an empty table can mean a true first run *or* wiped/migrated state. The fallback path refuses to silently seed unless `MODELBYTES_ALLOW_SEED=1` is set; otherwise it alerts, records a `blocked` run, and exits 1.

## Storage

- **PostgreSQL** — three implemented tables:
  - `models` — the dedup set used by `monitor.py`'s fallback path. `load_seen_models()` / `save_seen_models()` use `INSERT … ON CONFLICT DO NOTHING` (no DELETE-and-rebuild — that was an audit A5 fix in Phase 2b).
  - `posted_digests` — one row per posted UTC date so publisher reruns are idempotent (`post_date` / `source` / `digest_path` / `message_hash` / `posted_at`).
  - `publish_runs` — one audit row per run (`run_at` / `post_date` / `mode` / `status` / `models_found` / `models_emitted` / `message_chars` / `telegram_message_id` / `slack_ok` / `error`); powers `fallback_streak()` and the disabled health routine's eventual receipt-based replacement.
- **GitHub master** — pending and committed config state. The curator's daily output lives at `pending/<TODAY>.txt`. The supervisor's commits to monitor.py's constants accumulate over time. The `.supervisor-bootstrapped` marker controls supervisor authority.

## Structured Operational Data

The production design keeps durable operational state in Postgres, not scattered external notes. Current implemented tables:

- `models` — deduplication memory for fetched model IDs.
- `posted_digests` — one row per posted UTC date for idempotency.
- `publish_runs` — one audit row per publisher run (mode/status/counts/`telegram_message_id`/`slack_ok`/error); see [Ops / observability](#ops--observability-added-2026-06-12).

Recommended next tables are described in [`structured-data.md`](./structured-data.md): per-source fetch summaries, health checks, supervisor decisions, and source candidates. The Claude routine prompts should be updated to write or propose changes against those structured records rather than external note pages.

## Sources

- **OpenRouter** (`https://openrouter.ai/api/v1/models`) — model API catalog with pricing + descriptions
- **Ollama** (`https://ollama.com/library`) — local LLM availability
- **HuggingFace** (`https://huggingface.co/api/*`) — trending + per-org listings + top text-generation

All source fetches go through `monitor.py::_http_get()`, which sends a stable ModelBytes user agent and retries transient 429/5xx failures. Tuning knobs: `MODELBYTES_HTTP_RETRIES`, `MODELBYTES_HTTP_BACKOFF_SECONDS`, and `MODELBYTES_USER_AGENT`.

Public APIs, no authentication required (HF may rate-limit anonymous traffic; now retryable and visible in logs). For the growth rubric and candidate pipeline, see [`source-growth.md`](./source-growth.md); for the working queue, see [`source-candidates.md`](./source-candidates.md).

## Known follow-ups (not blocking)

- **Broader filter golden tests** — `categorize_model` has regression coverage now, but `is_noise_model()` and `is_significant_release()` still need fixture-based tests before larger taxonomy changes.
- **Filter-list consolidation (audit A12)** — `KNOWN_ORGS`, `MAJOR_HF_ORGS`, `PROVIDER_NAMES`, `significant_families`, and the `categorize_model` tier lists overlap and drift. Consolidation needs the broader golden tests first.
- **Source growth loop** — the source expansion rubric and candidate queue exist, but the supervisor prompt still needs to be updated to use them automatically.

## How v2 got here

The audit history is preserved as plan files in [`docs/superpowers/plans/`](./superpowers/plans/):

- `2026-05-20-modelbytes-audit-fixes-phase1.md` — original 20-item audit + the seven mechanical fixes
- `2026-05-20-modelbytes-audit-fixes-phase2a-config-cleanup.md` — Railway cron-only reshape, `--post` / `POST_IMMEDIATELY` cleanup
- `2026-05-20-modelbytes-audit-fixes-phase2b-postgres-only.md` — drop JSON state, UPSERT semantics
- `2026-05-20-modelbytes-v2-phase3a-curator-routine.md` — curator routine + pending-file fast-path

Each plan describes its scope, file structure, and the audit items it closed. Together they're the canonical record of what changed and why.

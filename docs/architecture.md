# ModelBytes Architecture

This document describes how ModelBytes works end-to-end as of v2 (2026-05-21). For runbook-style operations (token rotation, supervisor pause/resume, manual triggers), see [`operations.md`](./operations.md). For the self-managed VM deployment path, see [`vm-deployment.md`](./vm-deployment.md). For the audit-fix history that got us here, see the plan files in `docs/superpowers/plans/`.

## What ModelBytes is

A daily curated digest of new AI model releases, posted to the public Telegram channel [@ModelBytes](https://t.me/ModelBytes) at 16:00 UTC. The editorial signal is the product — what new models a builder/researcher should know about today, drawn from OpenRouter, Ollama, and HuggingFace, filtered for taste, tiered by category, and accompanied by a one-line lead "Take" setting the day's tone.

## Two systems, one repo

**Deterministic core** — `monitor.py` in the repo root. Fetches sources, applies the heuristic filter pipeline (`is_noise_model` / `is_significant_release` / `categorize_model`), dedupes against Postgres, and posts to Telegram. This is the safety net — it must keep posting even when the Claude layer is unavailable.

**Claude layer** — a set of scheduled routines on Claude.ai (no Anthropic API costs; uses the existing subscription via the Anthropic-hosted CCR environment) that handle editorial taste, growth, and observability:

| Routine | Cadence | Role |
|---|---|---|
| `modelbytes-curator-routine` | Daily 15:30 UTC | Generates the day's editorial digest with taste. Writes `pending/<TODAY>.txt` to master. Replaces the previous OpenAI gpt-4o-mini summarization step. |
| `modelbytes-supervisor-routine` | Daily 14:00 UTC | Audits production state + grows the system organically. Auto-commits list additions (KNOWN_ORGS, PROVIDER_NAMES, etc.) when bootstrapped; opens PRs for logic changes; opens issues for ambiguous calls. |
| `modelbytes-daily-health` | Daily 17:00 UTC | Verifies the day's post landed and looks sane. Logs to Notion; opens GH issue on FAIL. |
| `modelbytes-pr-curator` | Hourly | Auto-reviews any open PR that lacks a `🤖 Curator review:` comment. |

Routine IDs and URLs live in the auto-memory file `modelbytes-curator-routines.md`. Manage them at https://claude.ai/code/routines.

## The daily loop (autonomous, no human in the path)

```
14:00 UTC   modelbytes-supervisor-routine
            ├── audit recent posts, fetched data, GitHub issues
            ├── identify growth candidates (orgs/families) + drift indicators
            ├── if .supervisor-bootstrapped on master: auto-commit top 3 list additions
            └── log to Notion "ModelBytes Supervisor Log"

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
            ├── try_post_pending_curated() → reads pending/<TODAY>.txt
            ├── if file exists: post verbatim, record posted_digests, exit 0
            └── if missing: fall through to deterministic monitor.py pipeline

17:00 UTC   modelbytes-daily-health
            ├── fetch t.me/s/ModelBytes, find today's post
            ├── verify timestamp ~16:00 UTC, header + footer + non-empty body
            └── log status (PASS/WARN/FAIL) to Notion; GH issue on FAIL

Hourly      modelbytes-pr-curator
            └── review any open PR without a curator review comment
```

## File handoff: `pending/<TODAY>.txt`

The curator routine and the Railway publisher don't talk directly. They communicate via a file in the repo:

- Curator writes `pending/YYYY-MM-DD.txt` (UTC date) containing Telegram-ready HTML — `<b>`, `<i>`, `<a href>` only.
- Railway's `monitor.py::try_post_pending_curated()` first checks Postgres `posted_digests`. If today's date is already marked posted, it exits without sending anything.
- If today's pending file is present and non-empty, Railway posts it verbatim, records the date in `posted_digests`, and exits.
- If absent (curator failed or didn't run), `monitor.py` falls back to its deterministic pipeline (fetch → filter → categorize → `summarize_models()` via `gpt-4o-mini` → post). A successful fallback post also records today's date in `posted_digests`.

**Why this pattern**: claude.ai routines fire on cron, not on-demand. Inline Anthropic API calls from Railway would cost money. File handoff via the repo gets us "Claude-curated content in production" using only the Claude.ai subscription quota — at the cost of a 30-minute time gap (curator runs at 15:30, post is at 16:00).

**Duplicate protection**: `posted_digests` is the source of truth for whether a UTC date has already posted. This means same-day Railway redeploys, manual re-runs, and stale pending files do not post twice once the first successful send has been recorded. If Postgres is temporarily unavailable, the curated fast-path still tries to post so the channel does not go dark, but the log will say the idempotency ledger could not be checked or written.

**What the fallback looks like**: when no `pending/<TODAY>.txt` exists, `monitor.py` runs its full pipeline. The pipeline's final step is `summarize_models()`, which calls an OpenAI-compatible API to write the digest body. The API endpoint and key are configured by `MODELBYTES_LLM_KEY` / `MODELBYTES_LLM_MODEL` / `MODELBYTES_LLM_URL`, with `OPENAI_API_KEY` and `OPENROUTER_API_KEY` as key fallbacks (see README). Railway production currently has shared OpenAI-compatible fallback variables configured, so fallback days should produce LLM-written digests. If those variables are removed or fail, `summarize_models()` returns early and the deterministic template-only `build_digest_message()` runs instead. Same output format as a curated digest (tier headers, model entries, link, footer), without the editorial blurbs.

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

## Storage

- **PostgreSQL** (Railway-provided) — the `models` table holds the dedup set used by `monitor.py`'s fallback path. `load_seen_models()` / `save_seen_models()` use `INSERT … ON CONFLICT DO NOTHING` (no DELETE-and-rebuild — that was an audit A5 fix in Phase 2b). The `posted_digests` table records one row per posted UTC date so publisher reruns are idempotent.
- **GitHub master** — pending and committed config state. The curator's daily output lives at `pending/<TODAY>.txt`. The supervisor's commits to monitor.py's constants accumulate over time. The `.supervisor-bootstrapped` marker controls supervisor authority.
- **Notion** — observability log surfaces. "ModelBytes Daily Health Log" (one line/day, append-only); "ModelBytes Supervisor Log" (one section/day, append-only).

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

# ModelBytes Structured Data Roadmap

ModelBytes should use Postgres as the durable system of record for production state and operational telemetry. External note pages were useful during local experiments, but they should not be part of the production design.

## Current Tables

| Table | Status | Purpose |
|---|---|---|
| `models` | implemented | Deduplicates model IDs already seen by the fallback pipeline. |
| `posted_digests` | implemented | Records one posted digest per UTC date so reruns are idempotent. |
| `publish_runs` | implemented | One audit row per `monitor.py` run, including failures. |

### `publish_runs` schema

`record_publish_run(...)` writes one row best-effort on **every** run — posted, blocked, send-failed, no-models, or seeded — so a failed run is just as visible as a successful one.

Shipped columns:

- `id`
- `run_at` (`timestamptz`)
- `post_date` (`date`)
- `mode` (`varchar`, e.g. `curated`, `fallback-llm`, `fallback-template`)
- `status` (`posted`, `blocked`, `send-failed`, `no-models`, `seeded`)
- `models_found`
- `models_emitted`
- `message_chars`
- `telegram_message_id` (`bigint`)
- `slack_ok` (`boolean`)
- `error` (`text`)

Why it matters: gives a daily audit trail without reading raw logs. It powers `fallback_streak()` (which drives escalating fallback alerts) and is the intended data source for a future re-enabled daily-health reader.

## Recommended Next Tables

These are intentionally small and can be added incrementally.

### `source_fetches`

One row per source per run.

Useful fields:

- `run_id`
- `source`
- `status`
- `fetched_count`
- `kept_count`
- `duration_ms`
- `http_status`
- `error`

Why it matters: shows which sources are noisy, flaky, slow, or worth expanding.

### `health_checks`

One row per health-check routine run.

Useful fields:

- `checked_at`
- `post_date`
- `status` (`pass`, `warn`, `fail`)
- `telegram_post_found`
- `posted_digest_record_found`
- `notes`
- `github_issue_url`

Why it matters: health state becomes queryable and can drive alerting.

### `supervisor_runs`

One row per supervisor routine run.

Useful fields:

- `started_at`
- `finished_at`
- `status`
- `proposed_changes`
- `auto_commits`
- `prs_opened`
- `issues_opened`
- `notes`

Why it matters: keeps autonomy auditable after the system grows beyond a few constants.

### `source_candidates`

Structured version of `docs/source-candidates.md` once the source-growth loop is stable.

Useful fields:

- `name`
- `url`
- `source_type`
- `status` (`candidate`, `probing`, `accepted`, `rejected`)
- `freshness_score`
- `metadata_score`
- `noise_risk`
- `notes`

Why it matters: lets the supervisor discover and rank source candidates without making code changes immediately.

## Migration Order

`models`, `posted_digests`, and `publish_runs` are already shipped. Remaining order:

1. Add `source_fetches` and teach `monitor.py` to write one source summary per fetcher.
2. Add `health_checks` and update the health routine prompt to write structured results.
3. Add `supervisor_runs` and update the supervisor routine prompt to record decisions.
4. Promote `docs/source-candidates.md` into `source_candidates` only after the markdown queue has proven useful.

## Design Notes

- Keep posting resilient: telemetry write failures should not block Telegram sends.
- Keep dates UTC everywhere.
- Treat GitHub issues as the escalation surface, not the primary log.
- Keep the first schema small and append-only; analytics can come later.

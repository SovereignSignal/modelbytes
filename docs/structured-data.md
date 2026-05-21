# ModelBytes Structured Data Roadmap

ModelBytes should use Postgres as the durable system of record for production state and operational telemetry. External note pages were useful during local experiments, but they should not be part of the production design.

## Current Tables

| Table | Status | Purpose |
|---|---|---|
| `models` | implemented | Deduplicates model IDs already seen by the fallback pipeline. |
| `posted_digests` | in PR | Records one posted digest per UTC date so reruns are idempotent. |

## Recommended Next Tables

These are intentionally small. They can be added incrementally once the VM deployment path is stable.

### `publish_runs`

One row per execution of `monitor.py`.

Useful fields:

- `id`
- `started_at`
- `finished_at`
- `mode` (`curated`, `fallback`, `preview`, `seed`, `health`)
- `status` (`success`, `skipped`, `failed`)
- `post_date`
- `models_found`
- `models_emitted`
- `message_chars`
- `error`

Why it matters: gives the team a daily audit trail without reading raw logs.

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

1. Add `publish_runs` and `source_fetches`.
2. Teach `monitor.py` to write one run summary and one source summary per fetcher.
3. Add `health_checks` and update the health routine prompt to write structured results.
4. Add `supervisor_runs` and update the supervisor routine prompt to record decisions.
5. Promote `docs/source-candidates.md` into `source_candidates` only after the markdown queue has proven useful.

## Design Notes

- Keep posting resilient: telemetry write failures should not block Telegram sends.
- Keep dates UTC everywhere.
- Treat GitHub issues as the escalation surface, not the primary log.
- Keep the first schema small and append-only; analytics can come later.

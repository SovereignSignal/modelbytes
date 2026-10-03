# Release Events v1

A tiny cross-service contract for immediate release notifications, plus the
ModelBytes rules for when a model is allowed to emit one.

ModelBytes forwarding ships **inert**. A deploy of this code does not POST
and does not write the outbox unless all three are set:

| Variable | Required to forward |
|---|---|
| `MODELBYTES_RELEASE_FORWARDING` | exactly `1` |
| `RELEASE_EVENTS_URL` | endpoint URL |
| `RELEASE_EVENTS_TOKEN` | bearer token |

Leave the flag unset until the Release Bot receiver is merged, deployed, and
verified. Unset the flag to stop forwarding immediately. The outbox table is
additive; leave it on rollback.

Do not point `RELEASE_EVENTS_URL` at a host from a test, a fixture, or a log
paste. Tests use `https://events.invalid/`.

## Event

```json
{
  "schema": "release-event/v1",
  "id": "software:github:openclaw/openclaw:v2026.10.1",
  "kind": "software",
  "name": "OpenClaw",
  "version": "2026.10.1",
  "source": "clawbytes",
  "source_type": "github_release",
  "url": "https://github.com/openclaw/openclaw/releases/tag/v2026.10.1",
  "published_at": "2026-10-03T18:00:00Z",
  "summary": "optional grounded release notes",
  "metadata": {"repo": "openclaw/openclaw"}
}
```

Required: `schema`, `id`, `kind`, `name`, `version`, `source`, `url`.
`kind` is `software` or `model`.

The event id is the producer's observation id and the outbox primary key.
Consumers should treat a repeat of the same id as idempotent. The receiver
may also build its own canonical key from `kind`, `metadata.model_id` (or
`metadata.repo` / `metadata.package`), and `version`.

### Model identity

ModelBytes ids look like `model:<org>/<normalized-base>`:

- lowercased org and base name
- a leading catalog prefix (`huggingface/`, `openrouter/`, `ollama/`) is
  removed only when an org segment remains
- OpenRouter serving suffixes (`:free`, `:batch`) and quant/LoRA tails
  (`-gguf`, `-awq`, `-fp8`, …) are stripped
- `-instruct`, `-chat`, `-it`, and `-base` stay. Those are capability tiers
  in `collapse_variants` and are different models here too
- the id does **not** contain the source URL, the catalog name, or the
  release date

`version` is the explicit version and/or size token from the name (`4`,
`3.1-8b`, `32b`). When the name has neither, `version` is the normalized
model id (`org/base`). It is never a calendar date and never `"new"`.

`published_at` and `metadata.release_date` carry the release date.
`metadata.catalog` is the fetcher source (`huggingface-org`, `openrouter`,
`discovery`, …). `metadata.urls` lists the URLs we had. `url` prefers the
primary source (Hugging Face, vendor, paper) over an OpenRouter catalog link.
Pricing fields are not copied onto the event; availability and price stay in
the Telegram digest.

The same weights announced on Hugging Face and listed on OpenRouter share
one id. A second catalog row in the same batch is not a second event.

## When ModelBytes forwards

Forwarding runs only after a digest has passed `validate_digest_for_publish`
with no QA errors, `send_telegram_post` has returned success, and the model
actually appears in the published HTML (its URL, or a bold entry name).

`--preview` returns before that seam. It does not POST and does not write
the outbox, even if the three env vars are set.

`summarize_models` does not forward. The old `modelbytes_runner.py`
monkeypatch (emit every candidate before the summarizer, including during
preview) is gone. Production `startCommand` is `python monitor.py`.

A model qualifies only when all of the following hold:

- It is not a quantization, GGUF/AWQ/GPTQ/ONNX build, LoRA, or fine-tune
  (`-sft`, `-dpo`, `-finetune`, abliterations, …).
- It is not an OpenRouter `:free` / `:batch` serving SKU.
- It is not a price-only or availability-only change (those stay in the
  digest). A `price_change` / `availability_change` trait or a description
  that is itself a price change is enough to reject.
- It has a parseable release date inside the same freshness window as
  `is_stale_release` (14 days), **or** it is the undated exception below.
- It has a primary URL. `openrouter.ai` and aggregator hosts are not primary.
- It is not an OpenRouter catalog row whose normalized base was already
  announced by a primary source in the same batch.

Undated exception (the only way a missing date qualifies): official org
(`KNOWN_ORGS` / the Hugging Face org list) + web discovery + `confidence=high`
+ a primary-source URL. An undated Hugging Face catalog row does not qualify.

The curated fast-path (`pending/<date>.txt`) has no `ModelRelease` rows. The
seam is invoked there with an empty list, so a hand-written digest does not
invent events. Pending outbox rows are still retried at the start of `main`.

## Outcomes

`release_events.emit_release_event` returns `EmitResult.outcome`:

| Response | Outcome | Outbox status | Next run |
|---|---|---|---|
| 200 / 201 / 202 / 204 | `delivered` | `delivered` | not retried |
| 200 body `duplicate`, `already_seen`, or `owned_by_poller`; or HTTP 409 | `duplicate` | `duplicate` | not retried |
| 408, 429, 5xx, timeout, connection error | `retryable` | `pending` | retried |
| other 4xx (including 400 and 401) | `rejected` | `rejected` | kept, not retried |
| missing URL/token, or missing required fields | `rejected` | not queued | — |

A forward failure is a stderr warning. It does not change the publish exit
code, the Telegram post, or `posted_digests`.

## Outbox

Service disk is ephemeral, so the queue is Postgres:

```sql
CREATE TABLE IF NOT EXISTS release_event_outbox (
    id TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    status TEXT NOT NULL,
    attempts INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

The table is created on first use while forwarding is enabled
(`CREATE TABLE IF NOT EXISTS`). Disabled runs do not connect for this table.

Each armed run, including seed, quiet-day, and no-models returns, retries
`status = 'pending'` once before the early return. Preview skips that.
Delivered, duplicate, and rejected rows are not replayed.

There is no backlog import from `posted_digests` or `models`. The first run
after the flag is turned on queues only models published in that run.

## Process start

`railway.toml` `startCommand` is `python monitor.py`. Cron stays
`0 16 * * *`. The image `CMD` is the same command.

`monitor.py`'s `__main__` block calls `_handle_crash` on an exception
(ops alert, heartbeat `/fail`, `publish_runs` crash row). Preview crashes
still do not page. `modelbytes_runner.py` is a shim that runs monitor as
`__main__` for anyone still invoking the old filename; it does not call
`monitor.main()` directly.

## Rollout

1. Receiver merged, deployed, and verified.
2. Merge this publisher with `MODELBYTES_RELEASE_FORWARDING` unset. Confirm
   the deploy SHA, a normal 16:00 UTC digest, the stderr line
   `release forwarding: disabled`, and that a crash still reaches
   `_handle_crash`.
3. With explicit approval, set the flag to `1` (URL and token already
   present). The next run logs `release forwarding: no pending events` and
   then `sending` / `delivered` only for models in that digest.

## Transport

Producers POST JSON to `RELEASE_EVENTS_URL` with
`Authorization: Bearer $RELEASE_EVENTS_TOKEN`. Failure to forward must never
block the producer's normal publishing path.

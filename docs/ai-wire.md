# AI Wire ingest

ModelBytes tells the AI Wire registry about models that were actually in a
digest Telegram has already accepted. The push ships **inert**.

| Variable | Required to push |
|---|---|
| `AI_WIRE_ENABLED` | exactly `1` |
| `AI_WIRE_URL` | site origin, no path |
| `AI_WIRE_INGEST_TOKEN` | bearer token |

Leave the flag unset until the registry is deployed. Unset it to stop
immediately. Nothing is queued in Postgres, so turning the flag on does not
replay old days by itself.

Do not point `AI_WIRE_URL` at a host from a test or a log paste. Tests use
`https://wire.invalid`.

## When it runs

Both the curated fast-path and the inline path call the push only after:

1. `validate_digest_for_publish` returned no errors,
2. `send_telegram_post` returned success,
3. `posted_digests` and `publish_runs` have been written and the heartbeat
   has been pinged.

`--preview` returns before that. It does not POST.

The body is one batch, `POST {AI_WIRE_URL}/api/ingest/items`, header
`Authorization: Bearer {AI_WIRE_INGEST_TOKEN}`, JSON `{"items": [...]}`
(at most 100). Timeout is 5 seconds. A timeout, connection error, 408, 429,
or 5xx is tried once more. Other 4xx responses are not retried. The token is
not logged.

| Log | Meaning |
|---|---|
| `ai_wire push ok n=` | HTTP 2xx. `n` is the response `upserted` count when present, else the number of items sent. |
| `ai_wire push failed:` | The attempt (and the one retry, when one was allowed) failed. The digest still stands. |
| `ai_wire push skipped: no items` | The flag is on, but the sent HTML had no parseable model entry. |

This does not page the operator. Release-event forwarding is a separate flag
and a separate receiver.

## What is included

An item is built from a digest entry (bold name, or an ALSO TRACKED bullet),
not from the candidate list. A model the writer dropped is not pushed. The
body is cut with the same 4096-character rule as the Telegram send, so an
entry that landed only in `posted_digests.body` after truncation is not
pushed either. The
same normalized model in two sections is one item. A non-OpenRouter URL wins
over a catalog link when both are present.

| Field | Value |
|---|---|
| `source_bot` | `modelbytes` |
| `channel` | `ModelBytes` |
| `kind` | `model` |
| `canonical_key` | `model:<org>/<name>`, lowercase. Same normalization as release forwarding: catalog prefix, `:free` / `:batch`, and quant/LoRA tails drop; `-instruct` / `-chat` stay. A title with no org stays `model:<slug>` and does not invent one. |
| `title` | The name printed in the digest |
| `url` | The entry's link, or the model's primary link when the entry has none |
| `lane` | The tier header above the entry (`OPEN FRONTIER`, `CLOSED FRONTIER`, `SPECIALIZED`, `LOCAL`, `WATCH`, `ALSO TRACKED`) |
| `modality` | `text`, `image`, `video`, `audio`, or `multimodal` when the matched model has one |
| `org` | The org segment of the canonical key, when there is one |
| `summary` | The italic sentence, plain text, at most 600 characters |
| `published_at` | The model's release date, else a `Released Mon D` fragment in the entry |
| `posted_at` | When this bot posted |
| `channel_post_url` | `https://t.me/ModelBytes/<telegram_message_id>` when that id was returned |

## Backfill

Recent posts are already in Postgres: `posted_digests.body` is the HTML, and
`publish_runs.telegram_message_id` (latest `posted` row for that date) is the
channel link. No new table.

```bash
python monitor.py --ai-wire-backfill
python monitor.py --ai-wire-backfill --days 30
python monitor.py --ai-wire-backfill --apply
```

The default is a dry-run (no HTTP). `--days` defaults to 14 and caps at 90.
It selects that many newest digests that have a body, not a calendar window.
`--apply` POSTs one batch per digest, and only when the three variables above
are set. With no `DATABASE_URL`, or no stored bodies, the command prints
`no posted digest history` and exits 0. It never sends a Telegram message.

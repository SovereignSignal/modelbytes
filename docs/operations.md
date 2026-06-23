# ModelBytes Operations Runbook

Day-to-day operational tasks for ModelBytes. For the design behind these mechanics, see [`architecture.md`](./architecture.md).

> **⚠️ ARCHITECTURE NOTE (2026-06): the claude.ai routine layer is RETIRED.**
> The publish path is **inline-primary** (writer model + Parallel.ai research,
> no Claude Code dependency). Several sections of this runbook describe
> operating the now-retired routines and are marked **`[RETIRED]`** below.
> They are kept for historical context only — the routines do not exist and
> the runbooks do not apply:
> - Pausing / Re-enabling the supervisor's autonomy (`[RETIRED]`)
> - Manually triggering a routine (`[RETIRED]`)
> - When the curator misses (no `pending/<TODAY>.txt` was pushed) (`[RETIRED]` —
>   the inline path IS the everyday path now; a missing `pending/` file is normal)
> - The daily-health watchdog (`[RETIRED]`)
> - Decommissioning a routine (`[RETIRED]`)
>
> The **live** runbooks are: Observing the system, Ops alerts, Reading the
> `publish_runs` audit, Rotating the Telegram bot token, The LLM fallback
> chain, Manually triggering a Telegram post, When source fetches are flaky.

## Observing the system

| What | Where |
|---|---|
| Channel output | https://t.me/ModelBytes (or `https://t.me/s/ModelBytes` for HTML-scrapable history) |
| GitHub issues / PRs | https://github.com/SovereignSignal/modelbytes |
| Structured state | Postgres tables (`models`, `posted_digests`, `publish_runs`; see `docs/structured-data.md`) |
| Per-run audit | `publish_runs` table — one row per `monitor.py` run (see "Reading the publish_runs audit" below) |
| Ops alerts | Telegram DM to the admin chat, falling back to the ops Slack channel (see "Ops alerts" below) |
| Railway service state | Railway dashboard → `modelbytes` project → `modelbytes` service |

A clean day looks like: 1 post at 16:00 UTC in the channel, today's UTC date present in `posted_digests`, a `posted`-status row in `publish_runs`, no ops alert in the admin chat.

> Health signal comes from the in-process ops alert layer plus the `publish_runs` audit (see "Ops alerts" below). The old `daily-health` claude.ai routine is retired.

## Pre-publish factual QA

Every digest passes through `monitor.py::validate_digest_for_publish()` immediately before Telegram send. The QA pass is intentionally small and deterministic: it fixes known high-confidence fact slips, logs warnings for missing canonical source/license/parameter metadata, and blocks only severe errors that would make a post unsafe or empty.

When adding a model that the curator may mention repeatedly, add a `ModelFact` entry in `monitor.py` with canonical URL, release date, license, total parameters, active parameters, and confidence. The fallback LLM prompt receives those fields and is explicitly told not to invent missing facts. `ModelFact` entries carry a 45-day freshness window (`_fact_active`) so old correction regexes stop mutating unrelated copy after the fact goes stale.

## Ops alerts

**Live since 2026-06-12.** The publisher tells on itself in-process — it does not depend on the (now-disabled) `daily-health` watchdog. `monitor.py::send_ops_alert(text)` routes problems to the operator:

1. **Primary:** a Telegram DM to `MODELBYTES_ADMIN_CHAT_ID`.
2. **Fallback:** a message to the ops Slack channel `MODELBYTES_OPS_SLACK_CHANNEL_ID`.

The two are in isolated `try` blocks, so a Telegram outage still reaches Slack (and vice versa). `send_ops_alert()` never raises — a broken alert path cannot take down a publish run. All alert text (and every log line) passes through `_redact_secrets()`, which scrubs the bot token and the `DATABASE_URL` before anything is sent or logged.

Alerts fire on: fallback days (the curator missed), blocked/failed publishes, late curator (grace window expired), a lost `DATABASE_URL`, content-damage QA warnings (fact drift, floods), and an uncaught crash (the `__main__` crash handler alerts + heartbeats, then re-raises). Cosmetic format-drift warnings do **not** alert (anti-noise). Escalating fallback alerts are driven by `fallback_streak()` reading consecutive fallback rows out of `publish_runs`.

### Setting up the admin chat

The admin chat ID is the operator's own Telegram chat with the bot. To capture it:

1. **DM the bot** (`@ModelBytes_bot`) and send `/start` (or any message) from the operator account.
2. **Read the chat id** from `getUpdates`:
   ```bash
   curl -s "https://api.telegram.org/bot<BOT_TOKEN>/getUpdates" | python3 -m json.tool
   ```
   Find `result[].message.chat.id` — a positive integer for a direct chat (e.g. `12345678`). If `getUpdates` is empty, send the bot another message and retry (Telegram only returns recent, un-consumed updates).
3. **Set the Railway var** (keep it out of shell history):
   ```bash
   printf '%s' '<chat-id>' | railway variable set MODELBYTES_ADMIN_CHAT_ID --stdin --service modelbytes --environment production
   ```
4. Optionally set `MODELBYTES_OPS_SLACK_CHANNEL_ID` (a `C0XXXXXXXXX` channel id, requires `SLACK_BOT_TOKEN`) as the alert fallback.

To verify, force a fallback-path run (see "Manually triggering a Telegram post") on a day with no pending file and confirm the alert lands in the DM.

### Heartbeat (dead-man's switch)

`ping_heartbeat(ok, msg)` POSTs to `MODELBYTES_HEARTBEAT_URL` (e.g. a healthchecks.io check URL), hitting the `/fail` endpoint on failure. This is the **only** thing that catches "cron never fired at all" — every other alert assumes the process actually ran. It is **optional and currently UNSET**. To enable: create a healthchecks.io check with the expected schedule (daily near 16:00 UTC, with grace), then:

```bash
printf '%s' '<heartbeat-ping-url>' | railway variable set MODELBYTES_HEARTBEAT_URL --stdin --service modelbytes --environment production
```

### Reading the publish_runs audit

Every `monitor.py` run records exactly one `publish_runs` row, so this table is the ground truth for "what did the publisher do today and why". Recent rows:

```sql
SELECT run_at, post_date, mode, status,
       models_found, models_emitted, message_chars,
       telegram_message_id, slack_ok, error
FROM publish_runs
ORDER BY run_at DESC
LIMIT 20;
```

`mode` is `curated` or `fallback`. `status` values:

| Status | Meaning |
|---|---|
| `posted` | Digest sent to Telegram successfully (`telegram_message_id` populated; `slack_ok` shows the mirror result). |
| `blocked` | A digest existed but `validate_digest_for_publish` raised an ERROR — nothing was sent. Investigate the `error` field. |
| `send-failed` | Validation passed but the Telegram send failed (e.g. dead token, 4xx/5xx) — check `error`, then the token-rotation runbook. |
| `no-models` | Fallback path ran but found no significant releases after filtering/dedup, so there was nothing to post. |
| `seeded` | An empty `models` table was seeded (only happens with `MODELBYTES_ALLOW_SEED=1`); no digest posted that run. |

To see the recent fallback streak that drives escalating alerts:

```sql
SELECT run_at, status FROM publish_runs
WHERE mode = 'fallback'
ORDER BY run_at DESC LIMIT 10;
```

## [RETIRED] Pausing the supervisor's autonomy

If the supervisor starts proposing bad additions (or proactively, before a risky period like an upcoming release window), revoke its auto-commit authority:

```bash
cd modelbytes
git pull --ff-only origin master
git rm .supervisor-bootstrapped
git commit -m "chore: pause supervisor (reason here)"
git push origin master
```

On its next 14:00 UTC run, the supervisor will see the marker is gone, return to propose-only mode, and open a fresh proposal issue instead of auto-committing. Re-enable later with the bootstrap commit pattern (see below).

## [RETIRED] Re-enabling the supervisor's autonomy

After reviewing a propose-only issue (the supervisor opens these when it's not bootstrapped):

```bash
cd modelbytes
git pull --ff-only origin master
echo "bootstrapped $(date -u +%Y-%m-%d): reason" > .supervisor-bootstrapped
git add .supervisor-bootstrapped
git commit -m "chore: bootstrap supervisor auto-commits"
git push origin master
```

The supervisor's next run will see the marker present and resume auto-committing within its prompt's criteria (max 3 list-additions per run, list-additions only — no logic changes, no deletions).

## Rotating the Telegram bot token

The bot token has died on us twice in the recent past (each rotation requires regenerating via @BotFather). When 401 errors start appearing:

1. **Open Telegram → @BotFather**
2. `/mybots` → pick **@ModelBytes_bot** (or whatever bot is configured)
3. **API Token** → tap → either **Copy** (in case the token was glitched but still valid) or **Revoke current token** then **Copy** the new one
4. **Verify the new token immediately** with `/getMe`:
   ```bash
   curl -s "https://api.telegram.org/bot<NEW_TOKEN>/getMe"
   ```
   Should return `{"ok":true, "result": {...}}`. If not, the token didn't take.
5. **Update Railway env var** — keep the token out of shell history:
   ```bash
   printf '%s' '<NEW_TOKEN>' | railway variable set TELEGRAM_BOT_TOKEN --stdin --service modelbytes --environment production
   ```
6. Railway will redeploy automatically once the env var changes. The new image will use the new token starting with the next cron fire.

**If the channel was dark while the token was bad**: there's no auto-recovery — the missed cron is just missed. You can manually trigger a one-off post (see below) once the token is fixed.

## The LLM fallback chain

`monitor.py`'s deterministic pipeline (the safety net when the curator routine misses a day) ends with `summarize_models()`, which calls an OpenAI-compatible API to write digest blurbs. The relevant env vars:

| Env var | Default | Purpose |
|---|---|---|
| `MODELBYTES_LLM_KEY` | (none) | Primary API key. |
| `OPENAI_API_KEY` | (none) | Fallback if `MODELBYTES_LLM_KEY` is unset. |
| `OPENROUTER_API_KEY` | (none) | Fallback if both above are unset. |
| `MODELBYTES_LLM_MODEL` | `gpt-4o-mini` | Model identifier. |
| `MODELBYTES_LLM_URL` | `https://api.openai.com/v1` | API base URL. Set to OpenRouter's URL to switch providers. |

**Current state on Railway**: the checked-in `railway.toml` only declares required service variables, but production also has shared OpenAI-compatible fallback variables configured in Railway. If the curator routine misses (rare), the fallback path should produce an LLM-written digest. If those shared variables are removed, unset, or fail at request time, the fallback path runs `build_digest_message()` instead — a template-only digest with model names, specs, and links but no LLM-written blurbs. Format is the same; editorial voice is absent.

**To enable LLM-driven fallback on Railway**:

```bash
# Pick a provider. OpenAI or OpenRouter both work.
# Use --stdin to keep the key out of shell history.
printf '%s' '<api-key>' | railway variable set MODELBYTES_LLM_KEY --stdin --service modelbytes --environment production
```

After the next deploy, fallback days will produce LLM-written digests instead of template-only.

**To verify the fallback path locally**:

```bash
DATABASE_URL="" TELEGRAM_BOT_TOKEN="" python3 monitor.py --preview
```

If `MODELBYTES_LLM_KEY` (or the fallbacks) is set in your local env, you'll see the LLM-written digest. If not, you'll see the template-only one and a `No LLM key — falling back to template digest` line in stderr.

## Environment variables

Required (the service will not publish without these):

| Env var | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token for the publishing bot. |
| `TELEGRAM_CHANNEL_ID` | Target channel (`-100XXXXXXXXXX`). |
| `DATABASE_URL` | Postgres for the `models` / `posted_digests` / `publish_runs` tables. |

Optional / tuning:

| Env var | Default | Purpose |
|---|---|---|
| `MODELBYTES_LLM_KEY` / `MODELBYTES_LLM_MODEL` / `MODELBYTES_LLM_URL` | see "The LLM fallback chain" | Fallback summarizer (prod = GLM via Tenspire). |
| `SLACK_BOT_TOKEN` + `MODELBYTES_SLACK_CHANNEL_ID` | (none) | Slack mirror of the daily post. |
| `MODELBYTES_ADMIN_CHAT_ID` | (none) | Telegram DM target for ops alerts (primary). **Live since 2026-06-12.** |
| `MODELBYTES_OPS_SLACK_CHANNEL_ID` | (none) | Slack channel for ops alerts (fallback when the Telegram DM fails). |
| `MODELBYTES_HEARTBEAT_URL` | (none) | Dead-man's-switch ping target (e.g. healthchecks.io). Currently UNSET. |
| `MODELBYTES_PENDING_GRACE_SECONDS` | `600` (10 min) | How long `_wait_for_pending` polls GitHub raw for a late curator before giving up to the fallback path. |
| `MODELBYTES_PENDING_POLL_SECONDS` | `120` | Poll interval inside the grace window. |
| `MODELBYTES_ALLOW_SEED` | unset | Set to `1` to allow seeding an empty `models` table (see "Live-mode guards"). |
| `MODELBYTES_HTTP_RETRIES` / `MODELBYTES_HTTP_BACKOFF_SECONDS` / `MODELBYTES_USER_AGENT` | see "When source fetches are flaky" | Retrying HTTP helper knobs. |

## Live-mode guards

`main()` refuses to silently no-op when its state looks wrong (these guards exist because a wiped/migrated DB used to make every day skip silently):

- **Lost `DATABASE_URL`**: fires an ops alert, then **still attempts the curated path** (which needs no DB — the digest comes from GitHub raw and is idempotent only via `posted_digests`, so a duplicate is possible but a post still happens). For the **fallback path** a missing `DATABASE_URL` is **fatal** (exit 1), because dedup/audit are not optional there.
- **Empty `models` table**: the run refuses to silently seed and post a from-nothing digest. It exits unless `MODELBYTES_ALLOW_SEED=1` is set, in which case it seeds (recorded as a `seeded` row in `publish_runs`) rather than treating the empty table as "no news today".

## Manually triggering a Telegram post

Useful for: testing after a token rotation, posting an off-schedule digest, validating that the fast-path works after a code change.

```bash
cd modelbytes
git pull --ff-only origin master   # make sure you have the latest pending file if curator already ran
railway link --project modelbytes --environment production --service modelbytes
railway run python3 monitor.py
```

If the curator routine has already produced today's pending file, this will post the curated digest. If today's UTC date already exists in the `posted_digests` table, the run exits without posting again. If no pending file exists, the deterministic pipeline runs and posts whatever it generates.

## [RETIRED] Manually triggering a routine

Routines can be triggered ad-hoc from claude.ai. From this Claude session you can also use the `RemoteTrigger` tool:

```
RemoteTrigger(action="run", trigger_id="<trigger_id>")
```

Live trigger IDs are not listed here — they have churned (the routines were
recreated once after a deletion, so any hardcoded list goes stale; daily-health
is currently **disabled**). Find the current IDs at https://claude.ai/code/routines,
via `RemoteTrigger(action="list")`, or in the operator's
`modelbytes-curator-routines` notes.

## [RETIRED] When the curator misses (no `pending/<TODAY>.txt` was pushed)

Railway will fall through to the deterministic pipeline at 16:00 UTC. The channel still gets a post — normally LLM-summarized from the shared Railway fallback variables, or template-only if the LLM env vars are unavailable. To investigate:

1. **Open the curator routine's log** at https://claude.ai/code/routines — its most recent run's output explains what happened.
2. **Common causes**:
   - Fetcher 403s from the Anthropic CCR sandbox (some endpoints not on the network allowlist) — the routine should log this and exit cleanly without a partial pending file.
   - `gh push` failed (auth issue, branch conflict).
   - The routine ran out of token budget mid-task.
3. If the issue is transient: trigger the routine manually (see above).
4. If recurring: tune the prompt or relax the cron timing.

## When source fetches are flaky

All source fetches use a shared retrying HTTP helper. It retries transient `429`, `500`, `502`, `503`, and `504` responses, plus network exceptions, and logs the source name with each retry.

Tuning knobs:

```bash
MODELBYTES_HTTP_RETRIES=3
MODELBYTES_HTTP_BACKOFF_SECONDS=1.0
MODELBYTES_USER_AGENT="ModelBytes/1.0 (+https://github.com/SovereignSignal/modelbytes)"
```

If one source is failing but the others are healthy, the fetcher logs the error and returns an empty list for that source. The daily post should still proceed from the remaining sources.

## [RETIRED] The daily-health watchdog

The `daily-health` routine (17:00 UTC) is **disabled**. It scraped `t.me/s/ModelBytes` to confirm the day's post landed, but that endpoint **403-blocks datacenter IPs**, so the check false-FAILed every day from a cloud runner. A receipt-based replacement (confirm the post from the captured `telegram_message_id` / `publish_runs` row rather than by scraping the public web view) is **designed but tabled**.

Until it returns, health signal comes from two in-process sources instead of the watchdog:

1. **Ops alerts** (Telegram DM → Slack fallback) fire in-process on fallback days, blocked/failed publishes, a late curator, a lost `DATABASE_URL`, content-damage warnings, and crashes. See "Ops alerts" above.
2. **The `publish_runs` audit** — query for a missing or non-`posted` row for today's UTC date. See "Reading the publish_runs audit" above.

The one gap neither covers is "the cron never fired at all" — only the optional `MODELBYTES_HEARTBEAT_URL` dead-man's switch catches that.

If a `health-incident` GitHub issue from a past run is still open, close it once confirmed stale:

```bash
gh issue list --repo SovereignSignal/modelbytes --label health-incident --state open
```

Most common real failure remains: the cron ran but Telegram returned 401 (dead token), which now surfaces as a `send-failed` row in `publish_runs` plus an ops alert. Follow the bot-token-rotation runbook above.

## Cleaning up stale `pending/` files

The publisher does not delete pending files after posting. Duplicate posts are prevented by the `posted_digests` table, but old pending files can still be cleaned up for repo hygiene:

```bash
cd modelbytes
git pull --ff-only origin master
git rm pending/YYYY-MM-DD.txt
git commit -m "chore: remove posted pending file"
git push origin master
```

If you intentionally need to repost a date, remove the matching row from `posted_digests` first. Treat that as a production operation: verify the channel state before and after.

## [RETIRED] Decommissioning a routine

To delete a routine: visit https://claude.ai/code/routines, find the routine, delete it. Then update `[[modelbytes-curator-routines]]` to move the entry into a `## Retired` section with the date and reason.

`modelbytes-daily-health` is already disabled (it false-FAILed by scraping a web view that 403s datacenter IPs — see "The daily-health watchdog" above). Its job has been superseded by the in-process ops alert layer and the `publish_runs` audit, so a receipt-based re-enable is tabled rather than urgent. Health and supervisor outcomes now live in structured storage (`publish_runs`) rather than external note pages.

## When the v2 loop entirely fails

Worst-case fallback: the cron service is cron-only, so `railway redeploy` doesn't help (it doesn't trigger a run). To force a post NOW:

1. **From your local machine** (Mac, Linux):
   ```bash
   cd modelbytes && git pull
   railway run python3 monitor.py
   ```
   This uses Railway env vars (token + DATABASE_URL) but runs Python on your machine. Hits the fast-path if a pending file exists, otherwise runs the deterministic pipeline. Will post to Telegram.

2. **From any environment where Railway CLI is authed**:
   Same command as above. The Telegram token comes from Railway env, so the post comes from the right bot.

3. **If even Railway CLI is broken**: hit the Telegram API directly with `curl` using the bot token + a hand-written message. Last resort.

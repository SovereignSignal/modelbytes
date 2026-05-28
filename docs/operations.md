# ModelBytes Operations Runbook

Day-to-day operational tasks for ModelBytes. For the design behind these mechanics, see [`architecture.md`](./architecture.md).

## Observing the system

| What | Where |
|---|---|
| Channel output | https://t.me/ModelBytes (or `https://t.me/s/ModelBytes` for HTML-scrapable history) |
| Routine outputs | https://claude.ai/code/routines |
| GitHub issues / PRs | https://github.com/SovereignSignal/modelbytes |
| Structured state | Postgres tables (`models`, `posted_digests`; more planned in `docs/structured-data.md`) |
| Railway service state | Railway dashboard → `model-bytes` project → `modelbytes` service |

A clean day looks like: 1 post at 16:00 UTC in the channel, today's UTC date present in `posted_digests`, health check PASS once structured health records exist, and no new GitHub issues with the `health-incident` or `supervisor-drift` labels.

## Pre-publish factual QA

Every digest passes through `monitor.py::validate_digest_for_publish()` immediately before Telegram send. The QA pass is intentionally small and deterministic: it fixes known high-confidence fact slips, logs warnings for missing canonical source/license/parameter metadata, and blocks only severe errors that would make a post unsafe or empty.

When adding a model that the curator may mention repeatedly, add a `ModelFact` entry in `monitor.py` with canonical URL, release date, license, total parameters, active parameters, and confidence. The fallback LLM prompt receives those fields and is explicitly told not to invent missing facts.

## Pausing the supervisor's autonomy

If the supervisor starts proposing bad additions (or proactively, before a risky period like an upcoming release window), revoke its auto-commit authority:

```bash
cd modelbytes
git pull --ff-only origin master
git rm .supervisor-bootstrapped
git commit -m "chore: pause supervisor (reason here)"
git push origin master
```

On its next 14:00 UTC run, the supervisor will see the marker is gone, return to propose-only mode, and open a fresh proposal issue instead of auto-committing. Re-enable later with the bootstrap commit pattern (see below).

## Re-enabling the supervisor's autonomy

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

## Manually triggering a Telegram post

Useful for: testing after a token rotation, posting an off-schedule digest, validating that the fast-path works after a code change.

```bash
cd modelbytes
git pull --ff-only origin master   # make sure you have the latest pending file if curator already ran
railway link --project model-bytes --environment production --service modelbytes
railway run python3 monitor.py
```

If the curator routine has already produced today's pending file, this will post the curated digest. If today's UTC date already exists in the `posted_digests` table, the run exits without posting again. If no pending file exists, the deterministic pipeline runs and posts whatever it generates.

## Manually triggering a routine

Routines can be triggered ad-hoc from claude.ai. From this Claude session you can also use the `RemoteTrigger` tool:

```
RemoteTrigger(action="run", trigger_id="<trigger_id>")
```

Trigger IDs are in the `[[modelbytes-curator-routines]]` auto-memory file. As of 2026-05-21:

- Curator: `trig_017i1diXxpkQYsAL2MFU5yPe`
- Supervisor: `trig_01T7SJqAbNraE3ET11z9VCi5`
- Daily-health: `trig_01Eade8Cqc5wjBayzJencZAC`
- PR curator: `trig_016bmdGxfvVwuxwF7Jnv7fcy`

## When the curator misses (no `pending/<TODAY>.txt` was pushed)

Railway will fall through to the deterministic pipeline at 16:00 UTC. The channel still gets a post — normally LLM-summarized from the shared Railway fallback variables, or template-only if the LLM env vars are unavailable. To investigate:

1. **Open the curator routine's log** at https://claude.ai/code/routines/trig_017i1diXxpkQYsAL2MFU5yPe — its most recent run's output explains what happened.
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

## When the daily-health check FAILs

The routine will open a GitHub issue with the `health-incident` label. Open it for details:

```bash
gh issue list --repo SovereignSignal/modelbytes --label health-incident --state open
```

Most common failure: the cron ran but Telegram returned 401 (dead token). Follow the bot-token-rotation runbook above.

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

## Decommissioning a routine

To delete a routine: visit https://claude.ai/code/routines, find the routine, delete it. Then update `[[modelbytes-curator-routines]]` to move the entry into a `## Retired` section with the date and reason.

Don't delete `modelbytes-daily-health` until the supervisor has run successfully on 2-3 days — daily-health is the canary that catches things the supervisor might miss. When routine prompts are refreshed, health and supervisor outcomes should go to structured storage rather than external note pages.

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

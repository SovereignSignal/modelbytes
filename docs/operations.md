# ModelBytes Operations Runbook

Day-to-day operational tasks for ModelBytes. For the design behind these mechanics, see [`architecture.md`](./architecture.md).

## Observing the system

| What | Where |
|---|---|
| Channel output | https://t.me/ModelBytes (or `https://t.me/s/ModelBytes` for HTML-scrapable history) |
| Routine outputs | https://claude.ai/code/routines |
| GitHub issues / PRs | https://github.com/SovereignSignal/modelbytes |
| Notion daily health log | Notion page "ModelBytes Daily Health Log" (created on first health-check run) |
| Notion supervisor log | Notion page "ModelBytes Supervisor Log" |
| Railway service state | Railway dashboard → `model-bytes` project → `modelbytes` service |

A clean day looks like: 1 post at 16:00 UTC in the channel, 1 health-check log line in Notion saying PASS, no new GitHub issues with the `health-incident` or `supervisor-drift` labels.

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

## Manually triggering a Telegram post

Useful for: testing after a token rotation, posting an off-schedule digest, validating that the fast-path works after a code change.

```bash
cd modelbytes
git pull --ff-only origin master   # make sure you have the latest pending file if curator already ran
railway link --project model-bytes --environment production --service modelbytes
TZ=UTC railway run python3 monitor.py
```

`TZ=UTC` is needed when running locally because `try_post_pending_curated()` uses naive `datetime.now()` — without it, the function looks for `pending/<LOCAL_DATE>.txt` and misses the curator's `pending/<UTC_DATE>.txt`. On Railway containers this isn't a problem (Railway containers run UTC).

If the curator routine has already produced today's pending file, this will post the curated digest. If not, the deterministic pipeline runs and posts whatever it generates.

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

Railway will fall through to the deterministic pipeline at 16:00 UTC. The channel still gets a post — just the heuristic-only version. To investigate:

1. **Open the curator routine's log** at https://claude.ai/code/routines/trig_017i1diXxpkQYsAL2MFU5yPe — its most recent run's output explains what happened.
2. **Common causes**:
   - Fetcher 403s from the Anthropic CCR sandbox (some endpoints not on the network allowlist) — the routine should log this and exit cleanly without a partial pending file.
   - `gh push` failed (auth issue, branch conflict).
   - The routine ran out of token budget mid-task.
3. If the issue is transient: trigger the routine manually (see above).
4. If recurring: tune the prompt or relax the cron timing.

## When the daily-health check FAILs

The routine will open a GitHub issue with the `health-incident` label. Open it for details:

```bash
gh issue list --repo SovereignSignal/modelbytes --label health-incident --state open
```

Most common failure: the cron ran but Telegram returned 401 (dead token). Follow the bot-token-rotation runbook above.

## Cleaning up stale `pending/` files

The publisher doesn't currently delete the pending file after posting. To avoid duplicate posts from same-day Railway redeploys (rare but possible), manually clean up:

```bash
cd modelbytes
git pull --ff-only origin master
git rm pending/2026-MM-DD.txt
git commit -m "chore: remove posted pending file"
git push origin master
```

A proper fix (Postgres `posted_dates` table) is on the follow-up list.

## Decommissioning a routine

To delete a routine: visit https://claude.ai/code/routines, find the routine, delete it. Then update `[[modelbytes-curator-routines]]` to move the entry into a `## Retired` section with the date and reason.

Don't delete `modelbytes-daily-health` until the supervisor has run successfully on 2-3 days — daily-health is the canary that catches things the supervisor might miss.

## When the v2 loop entirely fails

Worst-case fallback: the cron service is cron-only, so `railway redeploy` doesn't help (it doesn't trigger a run). To force a post NOW:

1. **From your local machine** (Mac, Linux):
   ```bash
   cd modelbytes && git pull
   TZ=UTC railway run python3 monitor.py
   ```
   This uses Railway env vars (token + DATABASE_URL) but runs Python on your machine. Hits the fast-path if a pending file exists, otherwise runs the deterministic pipeline. Will post to Telegram.

2. **From any environment where Railway CLI is authed**:
   Same command as above. The Telegram token comes from Railway env, so the post comes from the right bot.

3. **If even Railway CLI is broken**: hit the Telegram API directly with `curl` using the bot token + a hand-written message. Last resort.

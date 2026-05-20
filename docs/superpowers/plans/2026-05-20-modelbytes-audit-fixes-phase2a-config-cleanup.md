# ModelBytes Audit Fixes — Phase 2a (Config & Scheduling Cleanup)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Resolve audit items **B10** (three scheduling paths → cron-only), **A2** (strip `--post` from configs), and **A4** (remove `POST_IMMEDIATELY` from docs). Config-only changes. No source or test changes.

**Architecture:** Replace the non-standard `[[cron]]` block in `railway.toml` with `[deploy].cronSchedule`, which is the canonical Railway pattern for a "cron-only" service (the service runs only on schedule, not on deploy). Delete the unused systemd unit file. Strip `--post` and `POST_IMMEDIATELY` from configs/docs since both are no-ops in current code.

**Tech Stack:** Railway TOML config, systemd unit file (deleted), Markdown docs.

---

## Background: why this works

Per Railway's [config-as-code reference](https://docs.railway.com/config-as-code/reference), the documented schema for cron is `deploy.cronSchedule`, NOT a top-level `[[cron]]` array. The current `railway.toml`'s `[[cron]]` block is almost certainly being ignored — the daily 16:00 UTC post we observe in `@ModelBytes` is configured via the Railway dashboard's service settings.

Per the [cron-jobs guide](https://docs.railway.com/cron-jobs): "Services configured as cron jobs are expected to execute a task, and terminate as soon as that task is finished." When `cronSchedule` is set in `[deploy]`, the service runs ONLY at scheduled times. No idle pattern needed.

**Risk:** if the Railway dashboard already has a cron schedule configured AND we add one in code, the in-code one wins (per docs: "Configuration defined in code will always override values from the dashboard"). So after merge, the in-code `cronSchedule = "0 16 * * *"` is authoritative and any dashboard setting is shadowed.

---

## File Structure

**Modify:**
- `railway.toml` — replace `[[cron]]` block with `cronSchedule` in `[deploy]`; strip `--post` from `startCommand`
- `README.md` — remove `--post` from Local Development; remove `POST_IMMEDIATELY` row from env var table
- `.env.example` — remove the `POST_IMMEDIATELY` block (comment + commented-out line)

**Delete:**
- `modelbytes.service` — unused systemd unit; openclaw is dev-only per project owner

**No changes:** `monitor.py`, `tests/`, `Dockerfile`, `requirements.txt`, `requirements-dev.txt`.

---

## Task 1: Reshape `railway.toml` for cron-only deploys (audit B10) and strip `--post` from it (audit A2 part 1)

**Files:**
- Modify: `railway.toml`

**Step 1: Read the current file to confirm baseline**

```bash
cat railway.toml
```

Expected current content:
```toml
[build]
builder = "DOCKERFILE"
dockerfilePath = "Dockerfile"

[deploy]
startCommand = "python monitor.py --post"
restartPolicyType = "ON_FAILURE"

[[cron]]
name = "daily-check"
schedule = "0 16 * * *"  # 9 AM PT = 16:00 UTC
command = "python monitor.py --post"

[env]
TELEGRAM_BOT_TOKEN = { required = true }
TELEGRAM_CHANNEL_ID = { required = true }
DATABASE_URL = { required = true }
```

**Step 2: Replace the file with the canonical cron-only shape**

Write the entire file as:

```toml
[build]
builder = "DOCKERFILE"
dockerfilePath = "Dockerfile"

[deploy]
startCommand = "python monitor.py"
cronSchedule = "0 16 * * *"
restartPolicyType = "ON_FAILURE"

[env]
TELEGRAM_BOT_TOKEN = { required = true }
TELEGRAM_CHANNEL_ID = { required = true }
DATABASE_URL = { required = true }
```

Three differences from before:
1. `startCommand` no longer has `--post` (audit A2)
2. New line `cronSchedule = "0 16 * * *"` inside `[deploy]` (audit B10 — replaces the `[[cron]]` block)
3. The entire `[[cron]]` block is removed (audit B10)

**Step 3: Verify TOML still parses**

```bash
python3 -c "import tomllib; tomllib.loads(open('railway.toml').read()); print('OK')"
```
(Python 3.11+ has `tomllib`. If unavailable, use: `python3 -c "import sys; data = open('railway.toml').read(); print('OK' if '[deploy]' in data and 'cronSchedule' in data and '[[cron]]' not in data and '--post' not in data else 'FAIL')"` )

Expected: `OK`.

**Step 4: Commit**

```bash
git add railway.toml
git commit -m "fix: move cron to [deploy].cronSchedule, drop deploy-time auto-post (audit B10, A2)

Railway's documented schema uses deploy.cronSchedule, not a top-level
[[cron]] array (which was being ignored). With cronSchedule set, the
service runs only at scheduled times — no deploy-time auto-run.

Also strips --post from startCommand: the flag is silently ignored
by main() and the script's only behavior is to post anyway."
```

---

## Task 2: Delete `modelbytes.service` (audit B10 part 2)

**Files:**
- Delete: `modelbytes.service`

**Context:** This file is a systemd unit for running `monitor.py` from `/opt/modelbytes` on `ubuntu-openclaw`. Project owner confirmed openclaw runs the script for dev/cooperation purposes but does NOT post on a schedule (no timer or cron points at this unit). The file is dead weight in the repo and confuses the deploy story.

**Step 1: Delete the file**

```bash
git rm modelbytes.service
```

**Step 2: Confirm the file is gone**

```bash
ls modelbytes.service 2>&1
```
Expected: `ls: modelbytes.service: No such file or directory`.

**Step 3: Commit**

```bash
git commit -m "chore: delete unused modelbytes.service systemd unit (audit B10)

The unit points at /opt/modelbytes on ubuntu-openclaw but no timer or
cron fires it — openclaw is dev/co-operation tooling per project
owner, not a scheduled poster. The Railway cron is the live path."
```

---

## Task 3: Strip `--post` from README and remove `POST_IMMEDIATELY` (audits A2 part 2, A4)

**Files:**
- Modify: `README.md`
- Modify: `.env.example`

**Step 1: Read current README to find the references**

```bash
grep -n -- '--post\|POST_IMMEDIATELY' README.md
```

Expected matches (approximate):
- Local Development section: `python3 monitor.py --post`
- Environment Variables table: a row for `POST_IMMEDIATELY`

**Step 2: Edit `README.md` — strip `--post` in Local Development**

Find the line (in the Local Development section's bash block):
```
python3 monitor.py --post
```
Replace with:
```
python3 monitor.py
```

**Step 3: Edit `README.md` — remove the `POST_IMMEDIATELY` table row**

Find the row in the Environment Variables table that documents `POST_IMMEDIATELY`. The current table reads:

```markdown
| Variable | Description | Required |
|----------|-------------|----------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | ✅ |
| `TELEGRAM_CHANNEL_ID` | Telegram channel ID | ✅ |
| `DATABASE_URL` | PostgreSQL connection string | ✅ (Railway auto-sets) |
| `POST_IMMEDIATELY` | If "true", posts on first run | ❌ |
```

Delete only the `POST_IMMEDIATELY` row. Result:

```markdown
| Variable | Description | Required |
|----------|-------------|----------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | ✅ |
| `TELEGRAM_CHANNEL_ID` | Telegram channel ID | ✅ |
| `DATABASE_URL` | PostgreSQL connection string | ✅ (Railway auto-sets) |
```

**Step 4: Edit `.env.example` — remove the `POST_IMMEDIATELY` block**

Read the current file:
```bash
cat .env.example
```

Expected current content (approximate):
```
# ModelBytes Configuration

# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHANNEL_ID=-100XXXXXXXXXX

# Database (Railway auto-sets this, or use local Postgres)
DATABASE_URL=postgresql://user:pass@localhost:5432/modelbytes

# Optional: Post immediately on first run (for testing)
# POST_IMMEDIATELY=true
```

Remove the last two lines:
```
# Optional: Post immediately on first run (for testing)
# POST_IMMEDIATELY=true
```

Result:
```
# ModelBytes Configuration

# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHANNEL_ID=-100XXXXXXXXXX

# Database (Railway auto-sets this, or use local Postgres)
DATABASE_URL=postgresql://user:pass@localhost:5432/modelbytes
```

(Preserve trailing newline if present.)

**Step 5: Verify no remaining stale references**

```bash
grep -n -- '--post\|POST_IMMEDIATELY' README.md .env.example
```
Expected: no output.

**Step 6: Confirm tests still pass (no source change, but sanity check)**

```bash
python3 -m pytest tests/ -v
```
Expected: 3 passed.

**Step 7: Commit**

```bash
git add README.md .env.example
git commit -m "docs: strip --post and POST_IMMEDIATELY references (audits A2, A4)

Both were no-ops in code. --post is silently ignored by main();
POST_IMMEDIATELY was never read. Removing them from README and
.env.example so the docs match the actual behavior."
```

---

## Phase 2a wrap-up

- [ ] **Step 1: Final sanity check — show all changes vs master**

```bash
git log --oneline master..HEAD
git diff --stat master..HEAD
```

Expected: 3 new commits, files changed: `railway.toml`, `modelbytes.service` (deleted), `README.md`, `.env.example`.

- [ ] **Step 2: Run the test suite one more time**

```bash
python3 -m pytest tests/ -v
```
Expected: 3 passed.

- [ ] **Step 3: Push the branch**

```bash
git push -u origin phase2a-config-cleanup
```

- [ ] **Step 4: Open the PR**

```bash
gh pr create --base master --head phase2a-config-cleanup --title "Phase 2a: config & scheduling cleanup (cron-only Railway, strip --post, remove POST_IMMEDIATELY)" --body "..."
```

PR body should cover: scope (3 commits, config-only), the Railway change (cronSchedule replaces [[cron]], stops deploy-time auto-post), the systemd deletion, the `--post` / `POST_IMMEDIATELY` strip, test plan (verify next 16:00 UTC cron still fires after merge), and the Phase 2b/2c outlook.

---

## Self-Review

**Spec coverage:** Phase 2a addresses B10 (Task 1 + Task 2), A2 (Task 1 + Task 3), A4 (Task 3). All other audit items remain deferred to Phase 2b (state backend) and Phase 2c (filter tests + refactor).

**Placeholder scan:** No TBDs. Task 3's expected file content is approximate ("the current table reads") because the exact byte content depends on the prior Phase 1 README edit; the implementer reads the actual content in Step 1 of each task before editing.

**Risk:** Two real risks:

1. **Railway cron behavior after merge.** Once `[deploy].cronSchedule` is set, the service becomes cron-only. The next 16:00 UTC run is the verification milestone. If Railway has a dashboard-configured cron AND the in-code one matches, no change. If they disagree, in-code wins. There is no way to be 100% sure ahead of merge without dashboard access — accept this as the verification step.

2. **Pre-merge `startCommand` change.** Before merge, `startCommand` is `"python monitor.py --post"` (always-on, attempts to post on deploy). After merge it's `"python monitor.py"` AND cron-only. The transition is instant on deploy — no double-state.

**Timing for merge:** As in Phase 1, avoid 15:30–16:30 UTC. Current Phase 2a development is ~20:30 UTC, ample buffer.

# ModelBytes v2 — Phase 3a (Inline Curator via Routine)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** Ship the first half of v2 — a Claude.ai routine generates a curated digest 30 min before each cron, commits it to the repo as `pending/<TODAY>.txt`, and Railway publish reads + posts it. Fallback to existing deterministic monitor.py if the pending file is missing. No Anthropic API costs (routine uses Claude.ai subscription).

**Architecture:** Two-part ship — (1) modify `monitor.py` so it checks for `pending/<TODAY>.txt` before running the full pipeline (PR + merge); (2) deploy the `modelbytes-curator-routine` via the schedule skill (separate, no-PR change).

**Tech stack:** Python 3.11, `gh` CLI, claude.ai scheduled routine, Telegram bot HTTP API.

---

## File Structure

**Modify:**
- `monitor.py` — add `try_post_pending_curated()` function called near the start of `main()`. If a `pending/<TODAY>.txt` exists, read it, post to Telegram, move to `posted/<TODAY>.txt`, commit + push, exit 0. Otherwise: existing logic runs unchanged.

**Create:**
- `pending/.gitkeep` — directory marker so the path exists in the repo
- `posted/.gitkeep` — same
- `tests/test_pending_curated.py` — unit tests for the new `try_post_pending_curated()` function

**Unchanged:** All other monitor.py logic, all fetchers, the filter pipeline, the deterministic `summarize_models()` path. We're adding a fast-path read-from-file branch BEFORE the existing logic; the existing logic stays as fallback.

---

## Task 1: Add pending-file fast-path to `monitor.py`

### Step 1.1: Create the new directories with `.gitkeep`

```bash
mkdir -p pending posted
touch pending/.gitkeep posted/.gitkeep
```

### Step 1.2: Write the failing test

Create `tests/test_pending_curated.py` with EXACTLY:

```python
"""Tests for the pending-curated-file fast-path."""
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure repo root is on sys.path so we can import monitor
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


def _write_pending(tmp_path: Path, date_str: str, content: str) -> Path:
    pending_dir = tmp_path / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    f = pending_dir / f"{date_str}.txt"
    f.write_text(content)
    return f


def test_try_post_pending_returns_false_when_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pending").mkdir()
    (tmp_path / "posted").mkdir()
    # No pending/<TODAY>.txt exists
    result = monitor.try_post_pending_curated()
    assert result is False


def test_try_post_pending_posts_and_moves_when_file_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pending").mkdir()
    (tmp_path / "posted").mkdir()
    today = monitor.datetime.now().strftime("%Y-%m-%d")
    _write_pending(tmp_path, today, "🤖 ModelBytes Digest test body\n\n3 models tracked today")

    sent = []
    def fake_send(msg: str) -> bool:
        sent.append(msg)
        return True

    monkeypatch.setattr(monitor, "send_telegram_post", fake_send)
    # Also avoid actually pushing to git
    monkeypatch.setattr(monitor, "_git_commit_posted", lambda date_str: None)

    result = monitor.try_post_pending_curated()
    assert result is True
    assert len(sent) == 1
    assert "ModelBytes Digest" in sent[0]
    # File should have been moved
    assert not (tmp_path / "pending" / f"{today}.txt").exists()
    assert (tmp_path / "posted" / f"{today}.txt").exists()


def test_try_post_pending_skips_empty_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pending").mkdir()
    (tmp_path / "posted").mkdir()
    today = monitor.datetime.now().strftime("%Y-%m-%d")
    _write_pending(tmp_path, today, "")

    monkeypatch.setattr(monitor, "send_telegram_post", lambda msg: True)
    result = monitor.try_post_pending_curated()
    # Empty file = no fast-path; fall through to deterministic
    assert result is False


def test_try_post_pending_skips_when_already_posted(tmp_path, monkeypatch):
    """If posted/<TODAY>.txt already exists, do not re-post."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pending").mkdir()
    (tmp_path / "posted").mkdir()
    today = monitor.datetime.now().strftime("%Y-%m-%d")
    _write_pending(tmp_path, today, "🤖 ModelBytes Digest test")
    # Already-posted marker
    (tmp_path / "posted" / f"{today}.txt").write_text("already posted earlier today")

    sent = []
    monkeypatch.setattr(monitor, "send_telegram_post",
                        lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(monitor, "_git_commit_posted", lambda date_str: None)

    result = monitor.try_post_pending_curated()
    # Should report True (idempotent: today's post already handled) but not re-send
    assert result is True
    assert len(sent) == 0
```

### Step 1.3: Run the test — expect failures

```bash
python3 -m pytest tests/test_pending_curated.py -v
```

Expected: 4 failures, all on `AttributeError: module 'monitor' has no attribute 'try_post_pending_curated'`.

### Step 1.4: Add `try_post_pending_curated()` to `monitor.py`

Insert this function BEFORE `def main():` (around line 920, before the existing main):

```python
def _git_commit_posted(date_str: str) -> None:
    """Commit the moved file from pending/ → posted/ via git + push."""
    try:
        import subprocess
        subprocess.run(
            ["git", "add", f"pending/{date_str}.txt", f"posted/{date_str}.txt"],
            check=False, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"chore: archive curated digest for {date_str}"],
            check=False, capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "master"],
            check=False, capture_output=True, timeout=30,
        )
    except Exception as e:
        print(f"git commit/push of posted/ failed (non-fatal): {e}", file=sys.stderr)


def try_post_pending_curated() -> bool:
    """Fast-path: post a pre-curated digest if one exists.

    Returns True if today's digest was handled (either posted now or already
    posted), False if no pending file exists or it was empty/invalid.
    The caller falls through to the deterministic pipeline on False.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    pending_path = Path("pending") / f"{today}.txt"
    posted_path = Path("posted") / f"{today}.txt"

    # Idempotency: if today is already posted, don't re-post.
    if posted_path.exists():
        print(f"Today's digest already posted ({posted_path}) — skipping pipeline.")
        return True

    if not pending_path.exists():
        return False

    body = pending_path.read_text().strip()
    if not body:
        print(f"Pending file {pending_path} is empty — falling back to pipeline.")
        return False

    print(f"Pending curated digest found at {pending_path} ({len(body)} chars). Posting.")
    if not send_telegram_post(body):
        print("Telegram send of curated digest failed — leaving pending file in place.",
              file=sys.stderr)
        return False

    # Move pending → posted and commit
    posted_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.rename(posted_path)
    _git_commit_posted(today)
    print(f"Posted curated digest for {today}; archived to {posted_path}.")
    return True
```

You'll also need to add `from pathlib import Path` to the imports. (Check whether it was already removed in Phase 2b — if so, re-add it.)

### Step 1.5: Wire it into `main()`

At the top of `main()`, right after the `if preview_mode:` argv-parsing block, insert:

```python
    # Fast-path: if a routine pre-generated today's curated digest, post it
    # and exit. Falls through to the deterministic pipeline on miss.
    if not preview_mode and try_post_pending_curated():
        return 0
```

`preview_mode` skips this check because preview is a manual debug tool — we want to see what the full pipeline would do, not short-circuit.

### Step 1.6: Run the tests — expect green

```bash
python3 -m pytest tests/test_pending_curated.py -v
python3 -m pytest tests/ -v   # full suite
```

Expected: all pass (3 prior + 3 new Postgres state + 4 new curated = 10 total).

### Step 1.7: Smoke test — preview mode still works without DATABASE_URL

```bash
DATABASE_URL="" TELEGRAM_BOT_TOKEN="" python3 monitor.py --preview 2>&1 | tail -10
```

Expected: fetchers run, no pending-file check (preview_mode skips it), digest renders.

### Step 1.8: Commit

```bash
git add pending/ posted/ monitor.py tests/test_pending_curated.py
git commit -m "feat: add pending-curated-file fast-path to monitor.py (v2 Phase 3a)

monitor.py now checks for pending/<TODAY>.txt at the start of main(). If
present, it posts the file's contents verbatim to Telegram, archives the
file to posted/<TODAY>.txt, and commits the move. If absent, the existing
deterministic pipeline runs as fallback.

This unblocks a claude.ai routine writing the curated digest 30min ahead
of the 16:00 UTC cron — Anthropic API not required, routine runs on
Claude.ai subscription.

Tests cover four cases: no file → fallback, file exists → post + move,
empty file → fallback, already-posted → skip (idempotent)."
```

---

## Task 2: Deploy the curator routine (separate, no PR)

After Task 1 is merged and Railway redeploys, deploy a claude.ai routine via the `schedule` skill or `RemoteTrigger` tool.

### Routine spec

- **Name**: `modelbytes-curator-routine`
- **Cron**: `30 15 * * *` (15:30 UTC daily — 30min before publish-daily's 16:00 UTC)
- **Environment**: Default Cloud Environment (`env_011CUR5JpR2LXbZFV1woxSEA`)
- **Model**: `claude-sonnet-4-6` (heavier reasoning task than the daily-health watcher)
- **Sources**: `https://github.com/SovereignSignal/modelbytes`
- **Allowed tools**: Bash, Read, Write, Edit, Glob, Grep
- **MCP connections**: none required (uses gh + curl)

### Routine prompt (to be passed at deploy time)

The prompt instructs Claude to:

1. `pip install --user requests psycopg2-binary` so monitor.py imports work
2. Run the fetchers via `python3 -c` snippets that import monitor.py
3. Apply filter + categorize using monitor.py functions
4. Fetch `https://t.me/s/ModelBytes` and parse last 7 days of post bodies for anti-repetition context
5. Apply editorial pass (the curator's job — drop weak items, rewrite blurbs, reorder, reassign tier, edit lead "Take" sentence)
6. Format as Telegram HTML using the same conventions as today's `build_digest_message` output
7. Write to `pending/<TODAY>.txt` (working in the cloned repo)
8. `git add pending/<TODAY>.txt && git commit -m "feat: curated digest for <TODAY>" && git push origin master`
9. Report what was generated and committed

Full prompt text is embedded in the deploy step below.

### Deploy via RemoteTrigger

```python
RemoteTrigger(
  action="create",
  body={
    "name": "modelbytes-curator-routine",
    "cron_expression": "30 15 * * *",
    "enabled": true,
    "job_config": {
      "ccr": {
        "environment_id": "env_011CUR5JpR2LXbZFV1woxSEA",
        "session_context": {
          "model": "claude-sonnet-4-6",
          "sources": [{"git_repository": {"url": "https://github.com/SovereignSignal/modelbytes"}}],
          "allowed_tools": ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
        },
        "events": [{"data": {
          "uuid": "<fresh-uuid>",
          "session_id": "",
          "type": "user",
          "parent_tool_use_id": null,
          "message": {"role": "user", "content": "<curator prompt here>"}
        }}]
      }
    }
  }
)
```

### After deployment

1. Trigger the routine manually via `RemoteTrigger(action="run", trigger_id=...)` to validate end-to-end without waiting for cron.
2. Confirm `pending/<TODAY>.txt` appears on master (visible in GitHub).
3. Wait for next 16:00 UTC Railway cron to pick it up, or manually redeploy to trigger immediate run.
4. Confirm the @ModelBytes channel receives the curated post.
5. Confirm `posted/<TODAY>.txt` appears on master afterward (the publish moved + committed it).

### Update memory

Update `[[modelbytes-curator-routines]]` with the new routine entry.

---

## Hard constraints (Phase 3a)

- **The deterministic fallback path must keep working.** No removing the `summarize_models()` call. No making Postgres optional in fallback. The pending-file fast-path is layered ON TOP, not replacing.
- **`--preview` mode is unchanged.** It skips the pending-file check entirely. Preview is for debugging the full pipeline.
- **Telegram send failures must not lose the pending file.** If `send_telegram_post()` returns False, leave the pending file in place so the next cron can retry.
- **Git push failures must not block the post.** If the post succeeded but the commit/push fails, log it and proceed. The next cron will re-attempt (idempotency via the posted/ marker prevents re-posting).

## What's NOT in this phase

- Supervisor routine — Phase 3b
- Retiring `modelbytes-daily-health` — Phase 3b
- Removing OpenAI `gpt-4o-mini` from `summarize_models()` — kept as fallback; could be removed in a later phase once curator-routine reliability is proven.
- Phase 2c.1 (filter golden tests) — still relevant, deprioritized for now; will be revisited as part of supervisor work in Phase 3b (supervisor needs tests as safety net).

---

## Self-review

**Spec coverage:** Phase 3a closes the "inline curator" piece of v2, adapted for no-API-costs constraint via routine + file handoff.

**Risk:**
1. **Routine prompt quality unknown until first run.** First triggered run is the validation.
2. **Idempotency depends on the `posted/<TODAY>.txt` marker.** If git push of posted/ fails, the file exists locally on the next cron but doesn't on the repo — Railway clones fresh each deploy, so we'd re-post. Mitigation: the test covers the marker-exists case; we should also make sure the publish-daily Railway service has git push credentials. Railway containers typically don't have a writable git checkout — this needs verification.
3. **Bigger issue (deferred to Phase 3b):** Railway containers may not have gh auth or git push access by default. Need to set up `GITHUB_TOKEN` env var for the Railway service and configure git remote URL with that token. If this is too complex, simpler alternative: don't commit posted/ from Railway; just delete the pending file locally (lost on next deploy). Idempotency then relies on Railway not re-running within the same day (which is the case unless something forces a redeploy).

Given the deployment complexity around git push from Railway, **simpler design for Task 1**: don't commit/push the `posted/` marker. Just delete the pending file locally. Idempotency relies on the cron only running once per day. If a redeploy happens after a successful post on the same day, the deterministic pipeline runs again (one duplicate post — acceptable for an edge case).

**REVISION**: Task 1 should NOT call `_git_commit_posted`. Instead, the local file is deleted. The posted/ directory becomes irrelevant in this version (can drop it).

Wait — but the routine writes pending/<TODAY>.txt to GitHub master, and Railway clones fresh on each deploy. So the file IS in the Railway container at run time. Posting + deleting locally doesn't help idempotency across reruns since the next deploy re-clones with the file present.

The CORRECT idempotent design: the routine deletes the pending file from master AFTER it confirms the post landed. OR: the publisher Railway-side commits the deletion back to master.

The simplest is: publisher commits the deletion. Railway needs git push for this. Provision GITHUB_TOKEN as a Railway env var; Railway service does `git push https://x:$GITHUB_TOKEN@github.com/SovereignSignal/modelbytes master`.

**OK keeping the original design with the push.** The plan above is correct. Add GITHUB_TOKEN to Railway env as part of the Task 1 work.

(I'll add a note to the implementer about this.)

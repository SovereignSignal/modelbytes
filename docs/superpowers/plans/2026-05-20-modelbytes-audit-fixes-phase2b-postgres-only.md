# ModelBytes Audit Fixes — Phase 2b (State Backend: Postgres-only)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Resolve audit items **B9** (drop the Postgres-vs-JSON dual backend), **A5** (`save_seen_models` DELETE+INSERT inefficiency), **A6** (lexical-not-recency JSON eviction), and **A8** (ephemeral Docker state). Make Postgres the only state backend, replace DELETE+INSERT with batched UPSERT, and remove all dead JSON code paths.

**Architecture:** Three commits.

1. **State refactor + test** — `init_database`, `load_seen_models`, `save_seen_models` collapse to a Postgres-only implementation that uses `INSERT … ON CONFLICT DO NOTHING` (batched via `executemany`). When `DATABASE_URL` is unset, the functions degrade gracefully (return empty set / no-op) so `--preview` mode and local dev still work without a database. Production is unaffected because `railway.toml` already enforces `DATABASE_URL = { required = true }`.
2. **Cleanup** — drop `state/` from `.gitignore` (no longer written), delete `RESTORE_NOTE.txt` (a one-time May-16 restore notice now made obsolete by removing the JSON state file path).
3. **Docs** — update `README.md` so "PostgreSQL for state persistence" reads accurately (it's required, no longer optional).

**Tech Stack:** Python 3.11, `psycopg2-binary`, `pytest` with `unittest.mock` for the UPSERT guard test.

---

## Background: why graceful degradation, not strict failure

The original audit recommended "Postgres-only" — strict interpretation = error if `DATABASE_URL` is missing. But on inspection:

- `railway.toml` already has `DATABASE_URL = { required = true }`, so Railway rejects deploys without it. Production failure mode is "won't deploy", not "deploy then crash."
- Local dev / openclaw doesn't typically have a Postgres instance handy. Without graceful degradation, `python3 monitor.py --preview` would error out at the `init_database()` call before reaching the preview branch.
- The existing first-run logic in `main()` (`if is_first_run: print("First run — seeding, no digest sent"); return 0`) already handles the "empty state set" case correctly.

So when `DATABASE_URL` is unset, the right behavior is: empty `seen_models` set → first-run path → seed (no-op save) → exit cleanly. This is what `--preview` mode wants too.

In production, this code path is unreachable (Railway enforces the env var). It only matters for local dev.

---

## File Structure

**Modify:**
- `monitor.py` — refactor `init_database`, `load_seen_models`, `save_seen_models`; remove `STATE_FILE`, `POSTGRES_AVAILABLE`, `USE_POSTGRES`, `SCRIPT_DIR`; drop the `try: import psycopg2` guard (psycopg2 is now a hard dependency, already in `requirements.txt`); drop the `from pathlib import Path` and `import json` imports if no other code uses them.
- `.gitignore` — remove `state/` line.
- `README.md` — update the "Features" line ("PostgreSQL for state persistence" → "PostgreSQL required for state persistence"), and the env var table description for `DATABASE_URL`.

**Create:**
- `tests/test_postgres_state.py` — unit test using `unittest.mock` that asserts `save_seen_models` issues an `INSERT … ON CONFLICT DO NOTHING` and does NOT issue a `DELETE FROM models`.

**Delete:**
- `RESTORE_NOTE.txt` — obsolete restoration notice from 2026-05-16; the warning it contains ("Do NOT delete state/model_releases_state.json") is no longer true.

**Unchanged:**
- The `models` table schema in `init_database` (richer than current usage, but we don't migrate or extend it here — that's a future enrichment opportunity).
- `requirements.txt` (already has `psycopg2-binary`).
- `requirements-dev.txt`, `Dockerfile`, `railway.toml`, `tests/test_main_wires_ollama.py`, `tests/test_orgs_unique.py`, `tests/test_digest_other_capacity.py`.

---

## Task 1: Postgres-only state refactor + UPSERT guard test

**Files:**
- Modify: `monitor.py`
- Create: `tests/test_postgres_state.py`

### Step 1.1: Write the failing test

Create `tests/test_postgres_state.py` with this content:

```python
"""Guard that save_seen_models UPSERTs into models and does not DELETE+INSERT."""
from unittest.mock import MagicMock, patch

import monitor


def _setup_pg_mocks():
    """Returns (mock_cursor, context-manager mocks) wired so connect/cursor work."""
    mock_cur = MagicMock()
    mock_cur_ctx = MagicMock()
    mock_cur_ctx.__enter__ = MagicMock(return_value=mock_cur)
    mock_cur_ctx.__exit__ = MagicMock(return_value=None)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur_ctx
    return mock_cur, mock_conn


def _all_sql_issued(mock_cur):
    """All SQL strings issued through execute() or executemany()."""
    sql_strings = []
    for call in mock_cur.execute.call_args_list:
        if call.args:
            sql_strings.append(call.args[0])
    for call in mock_cur.executemany.call_args_list:
        if call.args:
            sql_strings.append(call.args[0])
    return sql_strings


def test_save_seen_models_uses_upsert_not_delete():
    """save_seen_models must not DELETE FROM models; must use ON CONFLICT."""
    mock_cur, mock_conn = _setup_pg_mocks()
    with patch.object(monitor, "DATABASE_URL", "postgres://fake"), \
         patch.object(monitor.psycopg2, "connect", return_value=mock_conn):
        monitor.save_seen_models({"foo/bar", "baz/qux"})

    sql_strings = _all_sql_issued(mock_cur)
    assert sql_strings, "save_seen_models issued no SQL"

    for sql in sql_strings:
        assert "DELETE" not in sql.upper(), (
            f"save_seen_models still issues DELETE — found: {sql!r}"
        )

    upsert_found = any("ON CONFLICT" in sql.upper() for sql in sql_strings)
    assert upsert_found, (
        f"save_seen_models did not issue any ON CONFLICT statement. SQL issued: {sql_strings}"
    )


def test_save_seen_models_noop_without_database_url():
    """With no DATABASE_URL, save_seen_models must not attempt to connect."""
    with patch.object(monitor, "DATABASE_URL", ""), \
         patch.object(monitor.psycopg2, "connect") as mock_connect:
        monitor.save_seen_models({"foo/bar"})
    assert not mock_connect.called, (
        "save_seen_models tried to connect to Postgres despite DATABASE_URL being unset"
    )


def test_load_seen_models_returns_empty_set_without_database_url():
    """With no DATABASE_URL, load_seen_models returns an empty set without connecting."""
    with patch.object(monitor, "DATABASE_URL", ""), \
         patch.object(monitor.psycopg2, "connect") as mock_connect:
        result = monitor.load_seen_models()
    assert result == set(), f"expected empty set, got {result!r}"
    assert not mock_connect.called, "load_seen_models tried to connect despite no DATABASE_URL"
```

### Step 1.2: Run tests — confirm new tests fail

```bash
python3 -m pytest tests/test_postgres_state.py -v
```

Expected: 3 failures.
- `test_save_seen_models_uses_upsert_not_delete` — fails because current `save_seen_models` issues `DELETE FROM models`.
- `test_save_seen_models_noop_without_database_url` — fails because current code has no `DATABASE_URL` check inside the function (`USE_POSTGRES` is computed at module load — see Step 1.3).
- `test_load_seen_models_returns_empty_set_without_database_url` — similar reason.

Note: the second and third tests patch `monitor.DATABASE_URL` to empty after module load. Since `USE_POSTGRES = POSTGRES_AVAILABLE and DATABASE_URL` is set at import time, patching `DATABASE_URL` alone won't change `USE_POSTGRES`. The current code will still try to connect because `USE_POSTGRES` was True at import. After the refactor in Step 1.3, the check is on `DATABASE_URL` directly, so these tests will pass.

### Step 1.3: Refactor `monitor.py` — module-level state config

In the imports/config section near the top of `monitor.py` (lines ~7-32), replace this block:

```python
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Set

import requests

# PostgreSQL support (optional - falls back to JSON if not available)
try:
    import psycopg2
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False

# Config
SCRIPT_DIR = Path(__file__).parent.resolve()
STATE_FILE = SCRIPT_DIR / "state" / "model_releases_state.json"

# Database
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = POSTGRES_AVAILABLE and DATABASE_URL
```

With:

```python
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Set

import psycopg2
import requests

# Database — Postgres is the only state backend.
# When DATABASE_URL is unset (local dev, --preview mode), state functions
# degrade gracefully: load returns empty set, save is a no-op.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
```

What changed:
- Dropped `import json` (no longer used after removing JSON state file path).
- Dropped `from pathlib import Path` (only used for `STATE_FILE`).
- Dropped the `try: import psycopg2 / except ImportError` guard — `psycopg2-binary` is in `requirements.txt`, so this is a hard dependency. If the import fails, fail loudly at module load.
- Dropped `SCRIPT_DIR`, `STATE_FILE`, `POSTGRES_AVAILABLE`, `USE_POSTGRES`. Only `DATABASE_URL` remains.

### Step 1.4: Refactor `init_database`

Replace the current `init_database` function (around lines 133-163) with:

```python
def init_database():
    """Create the models table if it doesn't exist. No-op without DATABASE_URL."""
    if not DATABASE_URL:
        return
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS models (
                    id SERIAL PRIMARY KEY,
                    model_id VARCHAR(255) UNIQUE NOT NULL,
                    name VARCHAR(500),
                    provider VARCHAR(255),
                    source VARCHAR(50),
                    url TEXT,
                    description TEXT,
                    context_window INTEGER,
                    pricing_input NUMERIC(10,6),
                    pricing_output NUMERIC(10,6),
                    architecture VARCHAR(100),
                    release_date DATE,
                    is_open_source BOOLEAN,
                    unique_traits TEXT[],
                    discovered_at TIMESTAMP DEFAULT NOW(),
                    last_updated TIMESTAMP DEFAULT NOW()
                )
            """)
        conn.commit()
    finally:
        conn.close()
```

What changed:
- Removed the `USE_POSTGRES` check; now checks `DATABASE_URL` directly.
- Removed the broad `except Exception` swallow; if Postgres is down we want a loud failure.
- Used `with conn.cursor()` and try/finally for connection cleanup.

### Step 1.5: Refactor `load_seen_models`

Replace `load_seen_models` (around lines 166-184) with:

```python
def load_seen_models() -> Set[str]:
    """Load the set of seen model IDs from Postgres. Empty set without DATABASE_URL."""
    if not DATABASE_URL:
        return set()
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT model_id FROM models")
            return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()
```

What changed:
- Removed `USE_POSTGRES` check; check `DATABASE_URL` directly.
- Removed all the JSON file-reading code path.
- Removed broad `except Exception` swallow; if Postgres is down let the cron fail loudly.

### Step 1.6: Refactor `save_seen_models`

Replace `save_seen_models` (around lines 187-205) with:

```python
def save_seen_models(models: Set[str]):
    """Persist the set of seen model IDs to Postgres. No-op without DATABASE_URL."""
    if not DATABASE_URL or not models:
        return
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO models (model_id, name) VALUES (%s, %s) "
                "ON CONFLICT (model_id) DO NOTHING",
                [(m, m) for m in models],
            )
        conn.commit()
    finally:
        conn.close()
```

What changed:
- Removed `USE_POSTGRES` check; check `DATABASE_URL` directly.
- Early return if the input set is empty (no-op).
- **No more `DELETE FROM models`** (audit A5). Just batched INSERT with ON CONFLICT DO NOTHING.
- `cur.executemany(...)` for a single batched SQL statement instead of per-row `cur.execute(...)` in a Python loop.
- Removed the entire JSON file-writing fallback (audit A6 — lexical eviction was here — and audit A8 — ephemeral state in Docker — both go away).

### Step 1.7: Run the tests — confirm all pass

```bash
python3 -m pytest tests/ -v
```

Expected: 6 passed (3 from prior phases + 3 new from Step 1.1).

### Step 1.8: Smoke test — `--preview` mode works without DATABASE_URL

```bash
DATABASE_URL="" TELEGRAM_BOT_TOKEN="" TELEGRAM_CHANNEL_ID="" timeout 60 python3 monitor.py --preview 2>&1 | tail -25
```

Expected: fetchers run, "Found N new model(s)" line appears, "First run — seeding, no digest sent" line appears, and the script exits 0 without traceback. May time out at 60s due to network calls; partial output is acceptable evidence as long as no crash occurred at `init_database` or `load_seen_models`.

### Step 1.9: Commit Task 1

```bash
git add monitor.py tests/test_postgres_state.py
git commit -m "refactor: Postgres-only state with UPSERT, drop JSON fallback (audits B9, A5, A6, A8)

State persistence collapses to a single backend: Postgres. The JSON
file path, the POSTGRES_AVAILABLE try/except guard, the dual-backend
USE_POSTGRES flag, and the SCRIPT_DIR / STATE_FILE constants are all
removed.

save_seen_models now uses INSERT ... ON CONFLICT DO NOTHING via
executemany — no more DELETE FROM models on every save (A5), no
more lexical-order eviction (A6), no more ephemeral Docker state
(A8). The schema is unchanged; only the write path is.

When DATABASE_URL is unset (local dev, --preview mode), all three
state functions degrade gracefully: load returns empty set, save
is a no-op, init does nothing. Production is unaffected because
railway.toml enforces DATABASE_URL = { required = true }.

Adds tests/test_postgres_state.py with three guards:
  - save_seen_models uses ON CONFLICT, not DELETE
  - save_seen_models is a no-op without DATABASE_URL
  - load_seen_models returns empty set without DATABASE_URL"
```

---

## Task 2: Remove stale .gitignore entry, delete RESTORE_NOTE.txt

**Files:**
- Modify: `.gitignore`
- Delete: `RESTORE_NOTE.txt`

### Step 2.1: Remove `state/` from `.gitignore`

Read the current file:
```bash
cat .gitignore
```

Expected content:
```
state/
*.pyc
__pycache__/
.env
```

Edit to remove the `state/` line:
```
*.pyc
__pycache__/
.env
```

### Step 2.2: Delete `RESTORE_NOTE.txt`

```bash
git rm RESTORE_NOTE.txt
```

Confirm:
```bash
ls RESTORE_NOTE.txt 2>&1
```
Expected: file not found.

### Step 2.3: Run tests one more time

```bash
python3 -m pytest tests/ -v
```
Expected: 6 passed.

### Step 2.4: Commit Task 2

```bash
git add .gitignore
git commit -m "chore: remove obsolete state/ gitignore entry and RESTORE_NOTE (Phase 2b cleanup)

After Phase 2b, the script no longer writes state/model_releases_state.json,
so the gitignore entry is dead config.

RESTORE_NOTE.txt was a one-time 2026-05-16 restoration notice that warned
'Do NOT delete state/model_releases_state.json — it prevents duplicate
posts'. That file is no longer the source of dedup state (Postgres is),
so the note is now misleading. Removing both."
```

---

## Task 3: Update README to reflect Postgres-required state

**Files:**
- Modify: `README.md`

### Step 3.1: Read the current README

```bash
cat README.md
```

### Step 3.2: Update the Features list

Find this line in the "## Features" section:
```markdown
- 🗄️ PostgreSQL for state persistence
```

Replace with:
```markdown
- 🗄️ PostgreSQL state persistence (required — set DATABASE_URL)
```

### Step 3.3: Update the Environment Variables table

Find the `DATABASE_URL` row:
```markdown
| `DATABASE_URL` | PostgreSQL connection string | ✅ (Railway auto-sets) |
```

Replace with:
```markdown
| `DATABASE_URL` | PostgreSQL connection string (Railway auto-sets; required for posting — `--preview` mode runs without it) | ✅ |
```

The other rows stay unchanged.

### Step 3.4: Verify

```bash
grep -n 'PostgreSQL' README.md
grep -n 'DATABASE_URL' README.md
```
Expected: the Features line and the env var table row both show the new content.

```bash
python3 -m pytest tests/ -v
```
Expected: still 6 passed.

### Step 3.5: Commit Task 3

```bash
git add README.md
git commit -m "docs: clarify Postgres is required (Phase 2b)

The Features line previously read 'PostgreSQL for state persistence'
which sounded optional; the env var table description said 'Railway
auto-sets' without noting that the script needs it for any non-preview
run. Both are updated to reflect Phase 2b's hard requirement: posting
needs Postgres; --preview mode runs without."
```

---

## Phase 2b wrap-up

- [ ] **Step 1: Show all changes vs master**

```bash
git log --oneline master..HEAD
git diff --stat master..HEAD
```

Expected: 4 commits (plan + 3 task commits). Files changed: `monitor.py`, `tests/test_postgres_state.py` (new), `.gitignore`, `RESTORE_NOTE.txt` (deleted), `README.md`, plus the plan doc.

- [ ] **Step 2: Final test suite run**

```bash
python3 -m pytest tests/ -v
```
Expected: 6 passed.

- [ ] **Step 3: Final preview smoke test**

```bash
DATABASE_URL="" TELEGRAM_BOT_TOKEN="" TELEGRAM_CHANNEL_ID="" timeout 60 python3 monitor.py --preview 2>&1 | tail -20
```
Expected: runs the full fetch pipeline (no DB needed), prints first-run-seeding line, exits cleanly. Network may slow this; partial output is acceptable.

- [ ] **Step 4: Push branch**

```bash
git push -u origin phase2b-postgres-only
```

- [ ] **Step 5: Open PR**

```bash
gh pr create --base master --head phase2b-postgres-only --title "Phase 2b: Postgres-only state with UPSERT" --body "..."
```

PR body should cover: scope (4 audits closed via 3 commits), the UPSERT semantics, the graceful-degradation choice for `DATABASE_URL`-unset, the obsolete RESTORE_NOTE removal, the test plan (3 new tests, watch next cron post for production verification).

---

## Self-Review

**Spec coverage:** Tasks 1-3 close B9 (PG-vs-JSON consolidation), A5 (DELETE+INSERT → UPSERT), A6 (lexical eviction — JSON path gone), A8 (ephemeral Docker state — JSON path gone). Items A11, A12, B13, B14, B15, C19 remain for Phase 2c+.

**Placeholder scan:** No TBDs. Test code is verbatim. The mock setup uses `unittest.mock.patch.object(monitor.psycopg2, "connect", ...)` which assumes `monitor.psycopg2` is a module-level attribute. After Step 1.3 the `import psycopg2` is unconditional at module level, so `monitor.psycopg2` is the actual psycopg2 module — patching its `.connect` method works for the test.

**Risk:**

1. **Postgres connection-pool behavior.** Each call to `load_seen_models` / `save_seen_models` opens and closes a fresh connection. For a script that runs once per cron tick, this is fine. If `monitor.py` ever becomes a long-running process, switch to a connection pool. Out of scope here.

2. **Existing data in the Railway `models` table.** Going from DELETE+INSERT to ON CONFLICT DO NOTHING means existing rows persist forever (no `last_updated` refresh). This is correct behavior — `model_id` is a stable identifier; once seen, it stays seen. Schema-wise nothing changes.

3. **The graceful-degradation choice for `DATABASE_URL`.** If Railway's `DATABASE_URL` is ever unset (env var deleted), the cron runs silently as "first run, no post" every day — no posts. Acceptable because: (a) `railway.toml` enforces the env var at deploy time, so this state is unreachable in production; (b) if it does happen, the Telegram channel goes silent — easy to spot.

**Timing for merge:** Same window as before — avoid 15:30–16:30 UTC. Current Phase 2b drafting is ~20:50 UTC, ample buffer.

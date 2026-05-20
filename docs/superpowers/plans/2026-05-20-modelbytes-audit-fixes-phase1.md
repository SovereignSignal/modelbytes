# ModelBytes Audit Fixes — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the safe, decision-free fixes from the 2026-05-20 audit of `monitor.py` — wire Ollama back into the fetcher loop, remove dead `--send` argv parsing, deduplicate `MAJOR_HF_ORGS`, clean up no-op `pass` blocks, hoist inline imports, and fix the `ALSO TRACKED` truncation cap.

**Architecture:** A single-file Python cron job (`monitor.py`) fetches AI model listings from OpenRouter and HuggingFace, classifies them, and posts a tiered digest to Telegram. This phase introduces no architectural change. It adds a small `pytest` test directory (`tests/`) with golden fixtures for the targeted fixes only — broader filter-logic test coverage is deferred to Phase 2.

**Tech Stack:** Python 3.11, `requests`, `psycopg2-binary`, `pytest` (newly added, dev-only).

---

## Out of scope for Phase 1 (deferred, decisions needed)

These audit items are **not** addressed here because they need your input first or depend on the Phase 0 investigation:

| Audit ID | Item | Why deferred |
|---|---|---|
| A2 | `--post` flag in configs but not code | Decision: implement the flag, or strip from README/`railway.toml`/`modelbytes.service`? |
| A4 | `POST_IMMEDIATELY` env var dead | Decision: implement, or remove from README/`.env.example`? |
| A5, B9 | Postgres save inefficiency / PG-vs-JSON dual backend | Decision: drop Postgres or drop JSON? Affects A6, A8 too. |
| A6 | Lexical eviction in state file | Only matters if JSON stays. Resolves with B9. |
| A8 | Ephemeral state in Docker | Only matters if JSON stays on Railway. Resolves with B9. |
| A11, A12 | Noise-filter double-call / overlapping org lists | Refactor — needs golden tests (B13) first to lock current behavior. |
| B10 | Three scheduling paths | Phase 0 investigates; decommissioning is its own change. |
| B13 | No tests | Phase 2 — broad golden fixtures across `is_noise_model` / `is_significant_release` / `categorize_model`. |
| B14 | No HTTP retries | Small standalone feature, slot into Phase 2 or Phase 3. |
| B15 | Naive datetime / TZ inconsistency | Touches scheduling — defer until B10 is resolved. |
| C19 | `print` vs `logging` | Cosmetic, do alongside Phase 3 refactor. |

---

## File Structure

**Modify:**
- `monitor.py` — six surgical edits, all in this file
- `requirements.txt` — add `pytest` (or use a dev-requirements file; see Task 0)

**Create:**
- `requirements-dev.txt` — separate dev deps so the Docker image stays slim
- `tests/__init__.py` — empty marker
- `tests/conftest.py` — shared `pytest` fixtures (model-list mock factory)
- `tests/test_main_wires_ollama.py` — guard for Task 1 (Ollama wiring)
- `tests/test_orgs_unique.py` — guard for Task 3 (no duplicate orgs)
- `tests/test_digest_other_capacity.py` — guard for Task 6 (`ALSO TRACKED` cap)

**Why a separate dev-requirements file:** Adding `pytest` to `requirements.txt` would balloon the Railway image and ship test runners to prod. `requirements-dev.txt` is the standard split.

---

## Phase 0 — Investigation (no code changes)

Before any push to GitHub: confirm which deployment is actually posting to `@ModelBytes` so we don't double-post when we ship Phase 1.

### Task 0a: Confirm the live Telegram cadence

- [ ] **Step 1: Inspect the Telegram channel history**

Open `t.me/ModelBytes` (or whichever channel `TELEGRAM_CHANNEL_ID` resolves to) in a browser. Note the timestamps of the last 7 posts.

Expected outcomes:
- **One post/day at ~16:00 UTC** → Railway cron is live. Whether systemd is also firing is the question.
- **Two posts/day** → Both are live; double-post already happening.
- **No posts in 24h** → Neither is firing on a daily schedule; cadence may be manual.

- [ ] **Step 2: Record findings in this file under "Phase 0 results" (below).**

### Task 0b: Check Railway deployment status

- [ ] **Step 1: List Railway services for the modelbytes project**

If you have the Railway CLI configured locally:
```bash
railway status
railway logs --tail 50
```

Otherwise check the Railway dashboard for: last deploy time, last cron run time, last log line.

- [ ] **Step 2: Record findings in "Phase 0 results".**

### Task 0c: Check systemd state on `ubuntu-openclaw`

- [ ] **Step 1: SSH to the producer VM and inspect**

```bash
ssh ubuntu-openclaw@<host>
systemctl status modelbytes.service
systemctl list-timers | grep modelbytes
journalctl -u modelbytes.service --since "7 days ago" | tail -50
```

Key questions:
- Is `modelbytes.service` enabled?
- Is there a `modelbytes.timer` (the repo has no `.timer` file, but one may exist on the host)?
- What does the journal say about recent runs?

- [ ] **Step 2: Record findings in "Phase 0 results".**

### Phase 0 results (executed 2026-05-20 ~19:34 UTC)

**Telegram cadence (`t.me/s/ModelBytes`, last 12 days):**

- **Steady daily post at 16:00 UTC** on May 9, 10, 11, 13, 14, 15, 17, 18, 19, 20. (May 12 missed — single skipped day, likely a transient Railway issue.)
- **Today's post landed cleanly** at 2026-05-20 16:00:17 UTC: "Wednesday, May 20, 2026" header, 1 model tracked (`HRM-Text-1B`). LLM summarization path is succeeding (not the template fallback).
- **Off-hours posts are manual triggers**, not a second scheduler. Pattern: 01:01 UTC May 20, 01:26 UTC May 19, 21:30 UTC May 14, 19:44 UTC May 16 (the day of the vault wipe), plus a cluster of 6 posts on May 15 16:00–16:51 UTC (looks like a deploy + debug session).
- **The May 13 "double" at 16:00:18 + 16:00:26 UTC** is 8 seconds apart — consistent with a Railway retry or worker handoff, **not** a separate host firing. No evidence of two schedulers running at 16:00.
- **TZ bug observed in the wild (audit B15):** The off-hours post at 2026-05-20 01:01 UTC has a header that reads "Tuesday, May 19, 2026" — because the host is Pacific TZ and `datetime.now()` is naive. Worth bumping up in Phase 2 priority.

**Railway state:**

- Railway MCP requires interactive `railway login` (not authenticated in this session). Skipped direct API check.
- **Indirect confirmation:** the regular 16:00 UTC daily cadence from the Telegram data IS the Railway cron firing. The cron exists per `railway.toml` and is observably running. No further verification needed for Phase 1 go/no-go.

**systemd on `ubuntu-openclaw`:**

- User confirmed in chat: "openclaw is running and i don't want to shut it down, my thinking was that the claw could help to co-operate this but doesn't post for it."
- Phase 0 verification of this expectation is **implicit**: if openclaw were also firing the service on a schedule, we'd see additional posts at non-16:00 UTC times forming a regular pattern. We see only sporadic, irregular off-hours posts — consistent with manual triggers, not a second cron.
- **No change to openclaw is needed for Phase 1.** If Phase 1 ships and Sov later confirms openclaw has no timer/cron, we can mark this fully resolved.

**Decision: Safe to push Phase 1 to GitHub master? — YES, with caveats:**

1. Railway is the single live poster; pushing to master triggers a Railway deploy.
2. Today's 16:00 UTC cron has already run (~3.5h ago at decision time). Next run is May 21 ~16:00 UTC, giving ~20h buffer to verify the new code locally before it goes live.
3. Recommended: run `python3 monitor.py --preview` locally (with `TELEGRAM_BOT_TOKEN=""` to ensure no accidental send) before push, to confirm the new code produces a valid-looking digest.
4. Recommended: monitor the May 21 16:00 UTC run via `t.me/s/ModelBytes` to verify Ollama models appear and the `ALSO TRACKED` section uses the new format.

---

## Phase 1 — Safe Mechanical Fixes

### Task 1: Wire Ollama back into the fetcher loop (audit A1)

**Files:**
- Modify: `monitor.py:937-942` (the `for source_name, fetcher in [...]` block in `main()`)
- Test: `tests/test_main_wires_ollama.py`

**Context for the engineer:** `monitor.py` defines `fetch_ollama_models` at line 465 and `categorize_model` returns `local_ready` when `model.source == "ollama"` (line 729). But the fetcher list in `main()` does not include Ollama, so the function and the tier are both dead. Re-add it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_main_wires_ollama.py`:

```python
"""Guard that main() wires fetch_ollama_models into the fetcher loop."""
import ast
from pathlib import Path

MONITOR = Path(__file__).resolve().parent.parent / "monitor.py"


def test_main_includes_ollama_fetcher():
    """The fetcher list inside main() must reference fetch_ollama_models by name."""
    tree = ast.parse(MONITOR.read_text())
    main_fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    referenced = {
        node.id
        for node in ast.walk(main_fn)
        if isinstance(node, ast.Name)
    }
    assert "fetch_ollama_models" in referenced, (
        "main() does not reference fetch_ollama_models — Ollama source is unwired."
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_main_wires_ollama.py -v
```
Expected: FAIL with `AssertionError: main() does not reference fetch_ollama_models — Ollama source is unwired.`

- [ ] **Step 3: Edit `monitor.py:937-942` to add Ollama to the fetcher list**

Replace:
```python
    for source_name, fetcher in [
        ("OpenRouter", fetch_openrouter_models),
        ("HuggingFace-Trending", fetch_huggingface_trending),
        ("HuggingFace-Orgs", fetch_major_orgs),
        ("HuggingFace-Top-TextGen", fetch_hf_text_generation),
    ]:
```

with:
```python
    for source_name, fetcher in [
        ("OpenRouter", fetch_openrouter_models),
        ("Ollama", fetch_ollama_models),
        ("HuggingFace-Trending", fetch_huggingface_trending),
        ("HuggingFace-Orgs", fetch_major_orgs),
        ("HuggingFace-Top-TextGen", fetch_hf_text_generation),
    ]:
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/test_main_wires_ollama.py -v
```
Expected: PASS.

- [ ] **Step 5: Smoke-test the script doesn't blow up at import time**

```bash
python3 -c "import monitor; print('import OK')"
```
Expected: `import OK`.

- [ ] **Step 6: Commit**

```bash
git add monitor.py tests/test_main_wires_ollama.py
git commit -m "fix: re-wire Ollama into fetcher loop (audit A1)

fetch_ollama_models was defined but never called from main();
categorize_model has a dead local_ready branch keyed on this source."
```

---

### Task 2: Remove dead `--send` argv parsing (audit A3)

**Files:**
- Modify: `monitor.py:922-927`

**Context:** `send_mode` is assigned and never read. Removing it is pure cleanup; no test needed because the change has no observable behavior (an unused variable is unused either way). A `py_compile` is enough.

- [ ] **Step 1: Edit `monitor.py:922-927`**

Replace:
```python
def main():
    preview_mode = "--preview" in sys.argv
    send_mode = "--send" in sys.argv
    if preview_mode:
        sys.argv.remove("--preview")
    if send_mode:
        sys.argv.remove("--send")
```

with:
```python
def main():
    preview_mode = "--preview" in sys.argv
    if preview_mode:
        sys.argv.remove("--preview")
```

- [ ] **Step 2: Verify the file still compiles**

```bash
python3 -m py_compile monitor.py && echo "OK"
```
Expected: `OK`.

- [ ] **Step 3: Verify preview mode still works (no Telegram side effect)**

```bash
TELEGRAM_BOT_TOKEN="" TELEGRAM_CHANNEL_ID="" python3 monitor.py --preview 2>&1 | tail -20
```
Expected: prints either a preview digest or a "Preview mode — not sending" line. Does NOT attempt to call Telegram.

- [ ] **Step 4: Commit**

```bash
git add monitor.py
git commit -m "chore: remove dead --send argv parsing (audit A3)

send_mode was assigned but never read; argv was popped for no effect."
```

---

### Task 3: Deduplicate `nvidia` in `MAJOR_HF_ORGS` (audit A7)

**Files:**
- Modify: `monitor.py:489-499`
- Test: `tests/test_orgs_unique.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_orgs_unique.py`:

```python
"""Guard that MAJOR_HF_ORGS has no duplicates."""
import monitor


def test_major_hf_orgs_unique():
    orgs = monitor.MAJOR_HF_ORGS
    duplicates = [o for o in set(orgs) if orgs.count(o) > 1]
    assert not duplicates, f"Duplicate orgs in MAJOR_HF_ORGS: {duplicates}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_orgs_unique.py -v
```
Expected: FAIL with `AssertionError: Duplicate orgs in MAJOR_HF_ORGS: ['nvidia']`.

- [ ] **Step 3: Edit `monitor.py:489-499` to remove the second `"nvidia"`**

Remove the standalone `"nvidia"` on line 497 (the one between `"circlestone-labs"` and `"Supertone"`). Keep the one on line 491 (between `"google"` and `"microsoft"`).

After:
```python
MAJOR_HF_ORGS = [
    "deepseek-ai", "meta-llama", "mistralai", "Qwen", "google",
    "anthropic", "openai", "nvidia", "microsoft", "x-ai",
    "z-ai", "zai-org", "arcee-ai", "openbmb", "minimaxai",
    "NousResearch", "tiiuae", "01-ai", "baai", "xiaomi",
    "moonshotai", "bytedance-seed", "inclusionai", "ibm",
    "allenai", "amazon", "perplexity-ai", "stabilityai",
    "HiDream-ai", "SulphurAI", "Zyphra",
    "circlestone-labs", "Supertone",
    "TencentARC", "ResembleAI", "ADSKAILab", "open-thoughts",
]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/test_orgs_unique.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add monitor.py tests/test_orgs_unique.py
git commit -m "fix: remove duplicate nvidia from MAJOR_HF_ORGS (audit A7)

nvidia appeared twice, doubling HF API calls per run for that org."
```

---

### Task 4: Remove no-op `pass` blocks (audit C16, C18)

**Files:**
- Modify: `monitor.py:256-258`, `monitor.py:342-345`

**Context:** Two `pass` blocks that do nothing useful — pure structural noise. Both are inside `is_noise_model`. No test needed; behavior is unchanged.

- [ ] **Step 1: Edit `monitor.py:256-258` to remove the dead block**

Remove:
```python
    # Known orgs pass through (but still filtered by other rules below if egregious)
    if author_prefix in KNOWN_ORGS:
        pass

```

(The comment and the dead `if`/`pass` together. The blank line above the next comment block can stay.)

- [ ] **Step 2: Edit `monitor.py:342-345` to remove the `pass`-with-comment**

Replace:
```python
            if any(f in model_lower for f in known_families):
                pass  # Allow through — model family matches
            else:
                return True
```

with:
```python
            if not any(f in model_lower for f in known_families):
                return True
```

- [ ] **Step 3: Verify the file still compiles and tests pass**

```bash
python3 -m py_compile monitor.py && python3 -m pytest tests/ -v
```
Expected: `OK` and all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add monitor.py
git commit -m "chore: remove no-op pass blocks in is_noise_model (audit C16, C18)

Two dead branches: 'if author_prefix in KNOWN_ORGS: pass' did nothing,
and the inverted 'if/pass/else: return True' is clearer as a single
'if not ...: return True'."
```

---

### Task 5: Hoist inline imports to module top (audit C17)

**Files:**
- Modify: `monitor.py:1-14` (top-of-file imports), `monitor.py:287` (remove `import re as _re`), `monitor.py:441` (remove `from datetime import timezone`)

**Context:** `re` is already imported at the top, so `import re as _re` inside `is_noise_model` is redundant — just use `re` directly. `from datetime import timezone` is only imported inline inside `fetch_openrouter_models`; it should be on the top-level `datetime` import.

- [ ] **Step 1: Update the top `datetime` import**

Replace `monitor.py:12`:
```python
from datetime import datetime
```
with:
```python
from datetime import datetime, timezone
```

- [ ] **Step 2: Remove inline `from datetime import timezone` at line 441**

In `fetch_openrouter_models`, replace:
```python
            created = m.get('created', 0)
            if created:
                from datetime import timezone
                rd = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d")
```
with:
```python
            created = m.get('created', 0)
            if created:
                rd = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d")
```

- [ ] **Step 3: Remove inline `import re as _re` and use the top-level `re`**

At `monitor.py:285-292`, replace:
```python
    # '-base' as standalone suffix = classifier noise, but 'X-2-base' = real model
    if "-base" in model_lower:
        # Only flag as noise if '-base' is the LAST segment (classifier pattern)
        import re as _re
        if _re.search(r'-base$', model_lower) and not _re.search(r'\d-base$', model_lower):
```
with:
```python
    # '-base' as standalone suffix = classifier noise, but 'X-2-base' = real model
    if "-base" in model_lower:
        # Only flag as noise if '-base' is the LAST segment (classifier pattern)
        if re.search(r'-base$', model_lower) and not re.search(r'\d-base$', model_lower):
```

- [ ] **Step 4: Verify the file still compiles and tests pass**

```bash
python3 -m py_compile monitor.py && python3 -m pytest tests/ -v
```
Expected: `OK` and all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add monitor.py
git commit -m "chore: hoist inline imports to module top (audit C17)

import re was already at the top — drop the redundant aliased import.
from datetime import timezone is needed in two places, so put it
on the top-level datetime import."
```

---

### Task 6: Fix `ALSO TRACKED` truncation cap (audit C20)

**Files:**
- Modify: `monitor.py:796-800` (the `if tiers["other"]:` block in `build_digest_message`)
- Test: `tests/test_digest_other_capacity.py`

**Context:** `digest_models` is sliced to 15 in `main()`, but `_section` for "other" caps display at 5 with `tiers["other"][:5]`. If many of the 15 fall into the `other` tier, up to 10 silently vanish.

**Constraint to honor:** Telegram messages have a hard 4096-char ceiling, and `summarize_models` enforces a softer 2800-char target. The fix shouldn't blow either ceiling. Bumping the cap to 10 stays well within budget (each line is one model name, ~30 chars).

- [ ] **Step 1: Write the failing test**

Create `tests/test_digest_other_capacity.py`:

```python
"""Guard that build_digest_message's ALSO TRACKED section shows up to 10 models."""
import monitor


def _other_model(i: int) -> monitor.ModelRelease:
    """Build a ModelRelease that lands in the 'other' tier."""
    return monitor.ModelRelease(
        name=f"unknown-org/throwaway-{i}",
        provider="unknown-org",
        source="huggingface",
        url=f"https://huggingface.co/unknown-org/throwaway-{i}",
        description="",
        likes=600,  # over the 'other' threshold in categorize_model
    )


def test_other_section_shows_at_least_eight():
    """Given 12 'other' models, the digest should render 8+ of them, not silently drop them."""
    models = [_other_model(i) for i in range(12)]
    message = monitor.build_digest_message(models)
    rendered = sum(
        1
        for i in range(12)
        if f"throwaway-{i}" in message
    )
    assert rendered >= 8, (
        f"ALSO TRACKED section dropped {12 - rendered} of 12 models — "
        f"only {rendered} rendered."
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_digest_other_capacity.py -v
```
Expected: FAIL with `... only 5 rendered.`

- [ ] **Step 3: Edit `monitor.py:796-800` to raise the cap**

Replace:
```python
    if tiers["other"]:
        lines.extend(["", "━━━ <b>ALSO TRACKED</b>", ""])
        for m in tiers["other"][:5]:
            lines.append(f"  • {m.name.split('/')[-1]} ({m.source})")
        lines.append("")
```

with:
```python
    if tiers["other"]:
        lines.extend(["", "━━━ <b>ALSO TRACKED</b>", ""])
        for m in tiers["other"][:10]:
            lines.append(f"  • {m.name.split('/')[-1]} ({m.source})")
        if len(tiers["other"]) > 10:
            lines.append(f"  …and {len(tiers['other']) - 10} more")
        lines.append("")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/test_digest_other_capacity.py -v
```
Expected: PASS.

- [ ] **Step 5: Run the full suite**

```bash
python3 -m pytest tests/ -v
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add monitor.py tests/test_digest_other_capacity.py
git commit -m "fix: raise ALSO TRACKED cap from 5 to 10, count overflow (audit C20)

digest_models was sliced to 15 in main() but the 'other' tier was
truncated at 5 in build_digest_message — up to 10 models could vanish
silently. Raise the cap and surface any overflow as a count."
```

---

### Task 7: Add dev-requirements file and document test invocation

**Files:**
- Create: `requirements-dev.txt`
- Modify: `README.md`

**Context:** Phase 1 introduces `pytest` as a dev dependency. We don't want it in `requirements.txt` (which is what the Dockerfile installs into the production image). A separate `requirements-dev.txt` is the standard split.

- [ ] **Step 1: Create `requirements-dev.txt`**

```
-r requirements.txt
pytest>=7.0.0
```

- [ ] **Step 2: Add a "Running tests" section to README.md**

Append after the "Local Development" section:

```markdown
## Running Tests

```bash
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest tests/ -v
```
```

- [ ] **Step 3: Verify**

```bash
python3 -m pip install -r requirements-dev.txt --user --quiet
python3 -m pytest tests/ -v
```
Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add requirements-dev.txt README.md
git commit -m "build: add requirements-dev.txt for pytest

Keep pytest out of the production image while documenting how to
run the test suite locally."
```

---

## Phase 1 wrap-up

- [ ] **Step 1: Sanity-run preview mode end-to-end**

```bash
TELEGRAM_BOT_TOKEN="" TELEGRAM_CHANNEL_ID="" python3 monitor.py --preview 2>&1 | tee /tmp/modelbytes-preview.log
```
Expected:
- Fetches print (OpenRouter, Ollama, HF-Trending, HF-Orgs, HF-Top-TextGen)
- "Found N new model(s)" line
- A preview digest is printed
- No Telegram send attempt

- [ ] **Step 2: Diff against `origin/master` to review the cumulative change**

```bash
git fetch origin
git log --oneline origin/master..HEAD
git diff --stat origin/master..HEAD
```

- [ ] **Step 3: Push to GitHub — ONLY IF Phase 0 cleared this as safe**

```bash
git push origin master
```

**Do NOT push** if Phase 0 revealed any of:
- Both deployments are confirmed double-posting (decommission one first)
- `monitor.service` on `ubuntu-openclaw` has a timer firing in the next 24h that hasn't been coordinated
- Railway is in the middle of a deploy or failed state

- [ ] **Step 4: Watch the next cron run**

After push, observe the next 16:00 UTC run on the live deployment. Verify:
- It completes without error
- Ollama models appear in the digest (or at least the "Local Ready" tier is populated)
- The "ALSO TRACKED" section, if present, shows the new format

---

## Self-Review

**Spec coverage:** Phase 1 addresses audit items A1, A3, A7, C16, C17, C18, C20 directly. All other items are explicitly listed in "Out of scope for Phase 1" with the reason for deferral. ✓

**Placeholder scan:** No TBDs, no "add appropriate X", no "similar to Task N", no "TODO". Phase 0 has empty result fields that get filled during execution; that's by design, not a placeholder.  ✓

**Type consistency:** No new types introduced. Test files only import `monitor` and use existing names (`ModelRelease`, `MAJOR_HF_ORGS`, `build_digest_message`). ✓

**Risk:** The single highest-risk action is `git push origin master`. Phase 0 is the gate. Reaffirmed in the wrap-up step.

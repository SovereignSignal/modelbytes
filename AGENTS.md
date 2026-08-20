# AGENTS.md

Project-specific guidance for coding agents. See `CLAUDE.md` for full
architecture notes and `docs/` for design/runbooks.

## Cursor Cloud specific instructions

ModelBytes is a **single cron-driven Python publisher** (`monitor.py`), not a
web app or long-running server. There is no HTTP server, REST API, or
interactive CLI beyond the `--preview` flag. "Running the app" means invoking
`monitor.py` once; it fetches model registries, builds a digest, and (in live
mode) posts to Telegram, then exits.

- Dependencies are installed into a local `venv/` (git-ignored) by the startup
  update script. Use `venv/bin/python` / `venv/bin/pytest` (do not rely on the
  system Python, which is PEP 668 externally-managed). Python here is 3.12; the
  production `Dockerfile` pins 3.11, but the suite and `--preview` run cleanly on
  3.12.
- Tests: `venv/bin/python -m pytest tests/ -q` (~202 tests, a few seconds).
  `tests/conftest.py` zeroes the grace window and blanks all
  network/alert/DB/heartbeat side-effects, so the suite is safe to run even with
  production env vars exported. Keep it green (this repo is TDD — write the
  failing test first).
- There is no configured linter/formatter (no ruff/flake8/black/mypy config). A
  byte-compile check `venv/bin/python -m py_compile monitor.py ss_publish/*.py`
  is the closest "lint" gate.
- Safe local run (no Telegram send, no DB writes, no alerts):
  `venv/bin/python monitor.py --preview`. This still hits the **live public read
  APIs** (OpenRouter, Ollama, Hugging Face), so it needs outbound network and
  its output varies day to day. With no `MODELBYTES_LLM_KEY` set it prints the
  deterministic template digest ("No LLM key — falling back to template
  digest") — that is expected in this environment, not a failure.
- Do NOT run `monitor.py` without `--preview` unless you intend a real publish:
  live mode is gated only by `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHANNEL_ID` being
  set. For a live end-to-end post you also need `DATABASE_URL` (Postgres) and,
  for editorial quality, the LLM + Parallel.ai keys — see the env var table in
  `README.md`. None of these are required for `--preview` or tests.
- Production is the Railway project `modelbytes` / service `modelbytes` /
  environment `production` (cron `0 16 * * *`). Manual trigger runbook:
  `docs/operations.md` § "Manually triggering a Telegram post".
- Off-network `railway run` injects `DATABASE_URL` with host
  `postgres.railway.internal`, which does not resolve on this Cloud VM. The
  publisher retries `DATABASE_PUBLIC_URL` on that DNS failure only (auth
  errors still raise). Wire it on the `modelbytes` service as
  `${{Postgres.DATABASE_PUBLIC_URL}}`. Do not print that URL. `--preview`
  crashes must not ops-alert (2026-08-13 false CRASHED page).
- Manual re-runs should set `MODELBYTES_PENDING_GRACE_SECONDS=0`. The default
  600s grace window waits for a retired curator `pending/<today>.txt` that
  will not appear.
- Idempotency is `posted_digests`, not `publish_runs`. A `blocked` row does
  not prevent a same-day retry. A `posted` row does — do not live-run twice
  on a day that already has `telegram_message_id` set.
- `pending/<YYYY-MM-DD>.txt` is a write-back cache of what was published
  (feeds the next day's fact-consistency check). After a successful live
  publish, commit that file to master so the image's cache matches the
  channel. The publisher writes it locally; it does not auto-push.

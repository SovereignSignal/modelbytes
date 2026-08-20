# ModelBytes Source Growth Playbook

ModelBytes should grow its coverage gradually, with source quality improving over time instead of expanding through one-off guesses. This playbook defines how to find, evaluate, and add new model-release sources.

## Goals

- Find important model releases before they are already everywhere.
- Prefer sources with stable identifiers, dates, authors, links, and enough metadata to filter.
- Keep the daily digest selective. More sources should improve recall, not lower taste.
- Let the supervisor routine propose source growth, while keeping new fetcher logic behind PR review.

## Source Types

| Type | Examples | Why it helps | Risk |
|---|---|---|---|
| Model catalogs | OpenRouter, Hugging Face, Ollama-like registries | Structured model metadata and links | Can include a lot of low-signal entries |
| Lab/vendor release feeds | Major lab blogs, changelog feeds, model cards | High precision for primary announcements | Many formats; may require per-site adapters |
| Community trend surfaces | Trending repos, curated release lists, benchmark leaderboards | Finds fast-moving community releases | Can amplify hype or repeated fine-tunes |
| Paper/code indexes | Research indexes with code/model links | Catches research-first model drops | Often lacks deployment-ready metadata |
| Regional ecosystems | Non-US model hubs and provider catalogs | Better global coverage | APIs and language handling vary |

## Evaluation Rubric

A new source candidate should score well on most of these before implementation:

- **Freshness**: exposes recent releases or last-modified ordering.
- **Stable IDs**: provides a durable model slug, repo ID, or canonical URL.
- **Attribution**: identifies the author, lab, org, or provider.
- **Metadata**: includes tags, task type, license, context, pricing, downloads, likes, or dates.
- **Noise profile**: has enough signal to filter fine-tunes, experiments, mirrors, and quant-only copies.
- **Access**: works without secrets, or the required secret is low-risk and easy to rotate.
- **Operational fit**: tolerates daily polling and has clear failure behavior.

## Organic Growth Loop

Quality and coverage are the same loop. A new fetcher on top of a deaf fact-check or a filter that drops SPECIALIZED modalities adds spam, not recall. The sequenced plan (memory → list bugs → then 2–3 fetchers → recall audit) lives in [`superpowers/specs/2026-08-20-coverage-and-quality-expansion.md`](./superpowers/specs/2026-08-20-coverage-and-quality-expansion.md).

1. **Observe misses**
   - Compare recent Telegram posts and `publish_runs` rows against models that later prove important (OpenRouter `created` ≤14d is a cheap independent set).
   - Track which misses were source gaps versus filter mistakes versus ranking overflow versus writer-stripped entries.
   - Once `posted_digests` stores bodies, run a small recall audit (ground-truth list vs posted names) before adding another source. The Grant Wire 2026-06-28 audit is the method.

2. **Capture candidates**
   - Record candidate source name, URL, source type, why it matters, likely metadata fields, and failure risks.
   - Prefer a small markdown queue before adding code, so candidates can accumulate and be ranked.

3. **Probe manually**
   - Fetch a sample response.
   - Count how many entries survive current noise filters.
   - Identify whether the source needs a new fetcher or just a new org/family/provider entry.

4. **Add guardedly**
   - Constants/list additions can be supervisor auto-commits when bootstrapped.
   - New fetchers, schema changes, auth, thresholds, and deletion decisions should be PRs.
   - Every new source needs at least one parser/filter test and one "empty/error response" test.

5. **Review after launch**
   - Watch the next 3-5 digests for source-specific noise.
   - If a source produces repeated low-signal entries, tighten its fetcher or disable it.

## Suggested Repo Shape

The current repo is still small enough to keep `monitor.py` as the core, but future source work should avoid making the file harder to reason about.

Good next steps:

- Use `docs/source-candidates.md` as the supervisor-owned queue.
- Add a lightweight `SourceResult` or logging summary so each run reports fetched, filtered, and emitted counts per source.
- Move source-specific fetchers into a `sources/` package once there are more than 5-6 fetchers.
- Add fixtures under `tests/fixtures/sources/` before larger parser work.

## Candidate Backlog

Ranked candidates (probed 2026-08-20) live in [`source-candidates.md`](./source-candidates.md). Categories still worth revisiting after that queue:

- More direct provider feeds for labs already appearing in the digest (Phase 2 lab RSS).
- Model hubs outside the Hugging Face / Ollama / OpenRouter triangle (ModelScope probed; noise profile still needed).
- Lifecycle diffs (LiteLLM pricing) rather than new catalogs — quiet-day fuel for format v3.
- Research-release indexes where model cards or code links are part of the metadata (HF Daily Papers).

Do **not** add Reddit/HN/Bluesky (wrong product — that is ClawBytes), auth-gated inference catalogs, or HTML-only leaderboards.

Each candidate should go through the rubric above before it becomes code.

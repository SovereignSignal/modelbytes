# ModelBytes digest format v3 — identity tiers + action tags

**Date:** 2026-06-10
**Status:** Approved by Sov (interactive design session; live Telegram preview of both candidate formats, hybrid chosen)
**Supersedes:** the v2 tier scheme (PREMIER OPEN / CLOSED GIANTS / MULTIMODAL / SPECIALIZED / LOCAL READY) in both the curator prompt and `monitor.py`'s fallback pipeline.

## Goals

1. **One canonical taxonomy** shared by the curator routine and the `monitor.py` fallback, ending the split-brain where fallback days look different from curated days.
2. **A constant stream**: the digest should have substance every day, including days with no brand-new model releases, without padding or fine-tune spam.
3. **Reader value first**: every entry must say *why this model matters* before any specs.
4. **Reliability**: rebuild the daily-health watchdog so silent publish failures get caught (the 2026-05/06 quality collapse went unnoticed for two weeks because it was deleted).

## The taxonomy (identity tiers)

Sections describe *what kind of model it is*. Fixed order, empty tiers hidden:

| Tier | Header | Holds |
|---|---|---|
| 1 | `━━━ OPEN FRONTIER 🔓` | Open-weight releases pushing capability |
| 2 | `━━━ CLOSED FRONTIER 🔒` | API-only lab models |
| 3 | `━━━ SPECIALIZED 🎯` | Domain models (coding, audio, vision, video, embeddings-adjacent products) regardless of openness |
| 4 | `━━━ LOCAL 🏠` | Models whose headline is "runs on your hardware" (small open weights, Ollama-ready) |
| 5 | `━━━ WATCH 👀` | Announced / preview / weights-pending — not yet generally usable |

One model = one entry = one tier, the most *distinctive* one. (A 5B open coder is SPECIALIZED or LOCAL, not both; a 230B MoE with a live API and weights pending sits in OPEN FRONTIER with the pending note inline.)

## Entry grammar (mandatory, both authors)

```
<b>Model Name</b> — <i>{ONE sentence: the differentiator / value prop. Why does
this model exist and why should a builder care?}</i> {Hard facts: params
(total/active), context, headline benchmark + whose runs}. {⚡ or 📦 availability
tag}. <a href="URL">→ Named Source</a>
```

The availability tag is the action information (formerly the USE NOW / RUN LOCAL headers):

- `⚡ API live: $X/$Y per 1M (provider)` — hosted access, always with pricing if public
- `⚡ Private preview: (where)` — gated access
- `📦 Open weights, {license} · {where: HF / Ollama / OpenRouter}` — downloadable

## Lifecycle events are first-class entries

A model already covered in the last 14 days may reappear **only when its state changes**, and the entry must say what changed:

- **Graduation**: weights land for a WATCH item → entry in its new tier, marked `(was WATCH, Jun N)`, leading that tier. This is the continuity thread between digests.
- **Price move**: major cut/increase → entry with the delta (`now $0.14/1M, was $0.24`).
- **Availability landing**: model arrives on Ollama / OpenRouter / a major cloud.
- **Major version bump**.

This is the "constant stream" mechanism: slow news days are filled by real ecosystem movement, never padding. All existing noise filters (fine-tunes, quants, LoRA, merges, re-uploads) stay.

Graduation detection is **prompt-driven, zero new state** (approach A): the curator already reads the last 14 days of `pending/*.txt` for dedupe; WATCH entries live in those files, so it checks each one for shipped status during research. If this proves flaky, a curator-maintained `state/watchlist.json` is the designed upgrade path (approach B), not implemented now.

## Footer

`Total: N items tracked today` — "items", not "models", since lifecycle events count. `monitor.py`'s deterministic `📊 Surfaced N · scanned M today` fallback footer is unchanged.

## monitor.py changes (fallback parity)

- `categorize_model()` returns the new tier keys: `open_frontier`, `closed_frontier`, `specialized`, `local`, `other`. (The deterministic pipeline cannot know "announced-but-unshipped", so it never emits `watch` — WATCH is curator-only.) Old keys map: premier_open→open_frontier, closed_giants→closed_frontier, reasoning/coding/image_gen/audio→specialized, local_ready→local.
- `build_digest_message()` renders the new headers and appends a deterministic availability tag per entry from source metadata (openrouter→⚡ API, huggingface→📦 weights, ollama→📦 Ollama).
- `summarize_models()`'s LLM prompt rewritten to the entry grammar above, with the differentiator sentence required.

## Daily-health watchdog (rebuilt)

Scheduled claude.ai routine, daily 17:00 UTC: fetch `https://t.me/s/ModelBytes`, verify today's post exists with timestamp ≈16:00 UTC, the digest header, at least one tier section, a non-empty body, and a footer. PASS → done quietly. FAIL/WARN → open a GitHub issue on SovereignSignal/modelbytes with what it saw. Format checks are tolerant of tier *variety* (any subset of the five tiers is valid) but strict on header/footer presence.

## Out of scope (recorded follow-ups)

- Supervisor routine prompt update to track WATCH items / feed the new taxonomy.
- `state/watchlist.json` (approach B) if prompt-driven graduations miss.
- Filter-list consolidation (audit A12) — still blocked on broader golden tests.

## Runtime note

As of 2026-06-10, modelbytes runs **only on Railway** (project `<railway-project-id>`, service `modelbytes`, cron 16:00 UTC, auto-deploy-on-push from master). The VM (the-vm) is fully retired for modelbytes — timers confirmed disabled + inactive today.

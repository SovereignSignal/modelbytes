# ModelBytes coverage and quality expansion

**Date:** 2026-08-20
**Status:** Proposal (research pass; no fetcher code in this change)
**Owner ask:** how to expand coverage *and* quality, without trading one for the other
**Related:** [`source-growth.md`](../../source-growth.md), [`source-candidates.md`](../../source-candidates.md), [`structured-data.md`](../../structured-data.md), format v3 spec, GitHub issues #15 #22 #24 #28

## What this system is optimizing for

ModelBytes is a **daily taste filter**, not a model firehose. Expanding coverage means catching releases a builder would regret missing. Expanding quality means every posted entry is true, new, correctly tiered, and self-explanatory. More sources that survive `is_noise_model` but fail editorial taste make the channel worse.

The Grant Wire recall audit (2026-06-28, Notion) is the right method: measure misses against an independent ground-truth set, then add the *leaky* source — not every possible source.

## How the pipeline actually works today

One Railway cron (`monitor.py`, 16:00 UTC):

1. Fetch OpenRouter catalog, Ollama HTML library scrape, HF trending, ~50 `MAJOR_HF_ORGS`, HF top text-gen.
2. Drop stale (>14 days) and already-seen IDs.
3. Rank by `is_significant_release` then engagement; cap at `DIGEST_LIMIT = 15`.
4. Collapse same-(org, base, size) variants (N≥3).
5. Enrich the cap set from HF model cards.
6. `discover_recent_releases()` — one Parallel.ai search with seven generic queries; result is **writer context**, not `ModelRelease` objects.
7. Writer model emits format-v3 HTML; unverified links and stale dates are stripped; `validate_digest_for_publish(mode='fallback')` gates Telegram.

The claude.ai curator/supervisor/health layer is retired. Format v3 still describes WATCH, graduations, and price moves; the inline path does not implement them.

## Diagnosis — where coverage leaks

### 1. Known-org fetchers that return nothing (verified 2026-08-20)

HF's `author=` query is case-sensitive. Live probes:

| `MAJOR_HF_ORGS` entry | API result | Correct slug |
|---|---|---|
| `inclusionai` | 0 models | `inclusionAI` (Ling-3.0, LLaDA2.2 live) |
| `minimaxai` | 0 models | `MiniMaxAI` (already added as a second entry; lowercase leftover is issue #28) |
| `bytedance` (not listed) | 0 | `ByteDance` (53 models; issue #35) |

Issue #24 has been open since 2026-06-15. Until casing is fixed, **adding the org does not fetch the org**.

`is_significant_release`'s `significant_orgs` list is a *subset* of `KNOWN_ORGS` (issue #15). Cohere, inclusionAI, moonshotai, thinkingmachines, poolside, meituan-longcat, internlm, Wan-AI, sarvamai, etc. only rank as significant via a family-token match or ≥100k downloads. On a busy OpenRouter day they lose the 15-slot cap.

### 2. The noise filter fights the taxonomy

Format v3 puts coding / audio / image / video in **SPECIALIZED**. `is_noise_model` drops those pipeline tags wholesale:

`text-to-image`, `image-to-text`, `text-to-video`, `text-to-speech`, `music-generation`, `voice`, plus architectures `whisper` / `clip` / `sam`.

Higgs Audio v3 and SCAIL-2 reached the 2026-06-13 digest via curator research, not the HF fetcher. Black Forest Labs (FLUX) and Wan-AI were added to org lists, but their cards are typically `text-to-image` / `text-to-video` and would be dropped at fetch. Coverage expansion that adds vision/audio orgs without relaxing this gate is a no-op.

OpenRouter is the exception: it does **not** call `is_noise_model`, so closed API models and multimodal SKUs can land — including `:batch` and `:free` duplicates of the same slug (Gemini 3.7 Flash + Flash batch both appeared in the 14-day window).

### 3. WATCH and lifecycle died with the curator

`categorize_model` never returns `watch`. The writer prompt does not mention WATCH, graduations, or price moves. Parallel research can *see* an announcement, but:

- Hits are not turned into models, so they never enter ranking / collapse / HF-card enrich.
- The writer may only use URLs copied character-for-character from Parallel (good for honesty, bad if Parallel returns aggregators).
- There is no watchlist state, so a weights-landing day cannot graduate yesterday's WATCH item.

That is the coverage hole for closed-lab launches (OpenAI / Anthropic / Google / xAI) that are not on HF and not yet on OpenRouter.

### 4. Fetchers are catalogs, not "what changed"

Live OpenRouter snapshot 2026-08-20: **416** models, **19** created in the last 14 days, **8** in the last 7. Those 19 include GLM 5.3, Grok 4.6, Qwen3.8 2.4T, DeepSeek V4 Pro 0813, Gemini 3.7 Flash, Seed 2.0 Code, Solar Pro 4, Muse Glimmer — the actual news. The publisher still ingests the full catalog and relies on the Postgres `models` table to suppress yesterday. After a few quiet days the table is drained and Parallel is the only freshness engine.

Ollama is still an HTML scrape of `/library` with **no dates** (the code comments "currently low signal"). `https://ollama.com/api/tags` is not a stable public catalog (response shape looks like a node-local tag list).

HF `sort=createdAt` is a spam hose (anonymous LoRA dumps with 0 likes). Trending + per-org is the right HF shape; the org list is the lever.

### 5. Parallel discovery is generic

Seven queries of the form "new AI model release {month}". That is how aggregator URLs (`aireleasetracker.com`, `llm-stats.com`) reached the writer and, before 2026-08-13, blocked the day's post on `http://` hrefs. Lab-specific queries (Anthropic news, DeepSeek GitHub releases, Qwen blog, xAI notes) would raise precision without a new fetcher class.

## Diagnosis — where quality leaks

### 1. The digest has no durable memory (highest-leverage quality bug)

After a successful post, `monitor.py` writes `pending/<TODAY>.txt` on the **local filesystem** of an ephemeral Railway cron. `posted_digests` stores `message_hash`, not the body.

The last file committed to git is `pending/2026-06-16.txt`. Cross-day fact-consistency (`_check_fact_consistency`) and "already covered" (`_recent_digest_names`) therefore only see a May–June corpus. The code even documents this as an accepted limitation *when the supervisor was committing pending files daily*. That loop is gone.

Consequences since ~2026-06-23:

- The writer can repeat a model that shipped last week.
- Silent param/price flips versus last week's post are not flagged (the MiniMax 229B→428B class of bug).
- WATCH graduation cannot work even if the prompt asked for it.

Format v3 already named the fix: persist the published body (Postgres `posted_digests.body`, or git write-back) and read history from what readers actually saw.

### 2. `sig_org_map` is dead code (issue #22)

`categorize_model` looks up `provider.lower()`, which is the **display name** (`"Inclusion AI"`). The map is keyed by raw slugs (`"inclusionai"`). Intended OPEN FRONTIER / SPECIALIZED orgs land in ALSO TRACKED. Tests construct `provider="tencentarc"` and therefore do not catch production.

### 3. Writer quality is bounded by what we feed it

`enrich_with_hf_cards` only runs on `source.startswith("huggingface")` and only on the ≤15 digest set. OpenRouter rows often already have pricing + context, but license/params/benchmarks stay empty unless `hugging_face_id` is followed (OpenRouter now returns that field on 167/416 models; unused). When facts are `unknown`, the writer invents them and the gate only catches a handful of hardcoded `ModelFact` slips.

### 4. No per-source observability

`publish_runs` stores `models_found` / `models_emitted` as totals. There is no `source_fetches` row (already designed in `structured-data.md`). We cannot tell whether a thin day is "HF orgs empty because of casing" vs "Parallel returned aggregators" vs "writer stripped every entry."

### 5. Editorial list drift

Seven overlapping lists (`KNOWN_ORGS`, `MAJOR_HF_ORGS`, `PROVIDER_NAMES`, `significant_families`, `significant_orgs`, `categorize_model` premier/closed, `sig_org_map`). Supervisor auto-commits add to some and miss others. Audit A12 (ORG_REGISTRY) is still the right consolidation, blocked on golden tests for `is_noise_model` / `is_significant_release`.

## What not to do

- **Do not restore the claude.ai curator.** Coverage gaps are fetch/filter/memory problems; a second author does not fix casing or ephemeral pending files.
- **Do not copy ClawBytes' nine source classes.** ClawBytes is a coding-harness signal aggregator (RSS, Reddit, HN, Bluesky, leaderboards). ModelBytes should stay a model-release digest. Borrow *specific* high-precision surfaces (lab blogs, GitHub releases.atom for known orgs, HF Daily Papers, LiteLLM price diffs), not community firehoses.
- **Do not add Together / Fireworks / Groq / Replicate as fetchers.** All four returned 401 without a key (probed 2026-08-20). Auth-gated catalogs fail the source-growth access rubric.
- **Do not poll HF `sort=createdAt`.** Live sample was anonymous 0-like dumps.
- **Do not grow `monitor.py` past ~5–6 fetchers without a `sources/` package.** Already called out in the growth playbook.

## Sequenced plan

Quality first. New fetchers on top of a deaf fact-check and a filter that drops SPECIALIZED will add spam, not coverage.

### Phase 0 — close leaks in the current triangle

Small PRs, existing tests, no new APIs.

| Item | Why it is coverage *and* quality |
|---|---|
| Fix `MAJOR_HF_ORGS` casing (`inclusionAI`; drop leftover `minimaxai`) | Issues #24 / #28. Live API confirms the empty-result bug. |
| Re-key `sig_org_map` by display name **or** raw slug from `model.name` | Issue #22. Option B (slug from `name.split("/")[0]`) is robust to `PROVIDER_NAMES` edits. |
| Make `significant_orgs` derived from `KNOWN_ORGS` (or a single registry) | Issue #15. New orgs stop being second-class on busy days. |
| Persist published body; read fact-consistency + already-covered from it | Unblocks WATCH, repeats, and MiniMax-class drift. Prefer `posted_digests.body TEXT` over git write-back (Railway cron cannot push). |
| Stop dropping SPECIALIZED modalities in `is_noise_model` | Allow image/video/audio/TTS from `KNOWN_ORGS` and high-engagement unknowns; keep GGUF/LoRA/quant/SFT junk filters. Golden tests first. |
| OpenRouter: recency window (created ≤14d) + collapse `:free` / `:batch` | 19 recent IDs vs 416-catalog churn. Use `hugging_face_id` for card enrich. |
| Golden tests for `is_noise_model` / `is_significant_release` / `categorize_model` using **resolved** providers | Unblocks A12 and stops tests that pass on a path production never takes. |

### Phase 1 — make discovery a real source

Still no new vendor APIs.

- Log per-source `fetched / kept / error` (stdout now; `source_fetches` table next).
- Tighten Parallel queries: one generic + per-tier + **named labs already in `KNOWN_ORGS`**. Prefer vendor blogs, model cards, GitHub releases; down-rank aggregators in `_upgrade_http_url` / a host denylist.
- Promote Parallel hits that name a model + a primary URL into `ModelRelease` objects (`source="discovery"`, `confidence="low"` until card/pricing enrich). Then ranking, collapse, and the writer share one candidate set.
- Prompt: restore WATCH + lifecycle grammar from format v3. Emit WATCH only for announcement-only items with a primary URL and no weights/API yet.
- Cheap watchlist: rows in `posted_digests` (or a `watch_items` table) with `name + status=watch` so the next run can graduate.

### Phase 2 — add two or three high-precision sources

Candidates below were probed 2026-08-20. Implement one at a time; review the next 3–5 digests for source-specific noise (growth playbook).

**Do first**

1. **HuggingFace Daily Papers** (`GET https://huggingface.co/api/daily_papers`, 200, 50 items, org + githubRepo + upvotes). ClawBytes already ingests this. ModelBytes should keep only papers that map to a **concrete model a builder can watch or use** (linked repo, known org, or "we release weights" in the summary). Output: WATCH or SPECIALIZED, never a methods dump. Promotes the 2026-06-10 curator-only experiment to a fetcher now that the curator is gone.

2. **Lab primary feeds for orgs we already track** — GitHub `releases.atom` / vendor blogs (Anthropic news, DeepSeek, Qwen, xAI, Mistral, Cohere, Moonshot, Z.AI). ClawBytes `SOURCES.md` already maintains a clean list. ModelBytes should subscribe to the *model-lab* subset only, parse date + title + URL into `ModelRelease`, and let existing noise/significance gates decide. High precision, many formats — start with 5–8 feeds, not 54.

3. **LiteLLM pricing registry** (`model_prices_and_context_window.json`, public, ~3055 keys, sha-gated). ClawBytes uses key-diff for new SKUs. For ModelBytes the unique value is **lifecycle**: price cuts and new provider SKUs for models we already posted. That is the format-v3 "constant stream" on quiet days.

**Do second (if Phase 0+1 still miss CN / local)**

4. **ModelScope OpenAPI** (`https://www.modelscope.cn/openapi/v1/models`, 200, structured id/downloads/likes/license). Complements HF for labs that ship CN-first. Needs a noise profile probe (how many of the first page survive current filters) before a fetcher.

5. **Ollama** — replace HTML scrape only if a dated catalog exists. Do not treat `api/tags` as the library.

**Defer**

- LMArena / Artificial Analysis HTML leaderboards — no stable machine source (ClawBytes already passed LMArena for this reason). Revisit if they publish JSON.
- Auth-gated inference catalogs (Together, Fireworks, Groq, Replicate).
- Reddit / HN / Bluesky — wrong product.
- HF Spaces, HF createdAt firehose.

### Phase 3 — a recall audit, then stop adding sources

Once Phase 0–2 have a week in production:

1. Independently list ~30 notable releases from the last 14 days (OpenRouter recency + lab blogs + one human pass on the Telegram channel + Artificial Analysis "recently added" if usable).
2. Match against `posted_digests` bodies (after Phase 0 persist) and against `models` seen-set.
3. Classify each miss: source gap / filter false-negative / ranking overflow / writer stripped the entry / already correctly skipped.
4. Only then open a fetcher PR. Target: catch every KNOWN_ORG release and every OpenRouter 14-day add that is not a `:batch`/quant duplicate, without raising ALSO TRACKED spam.

This is the Grant Wire loop, sized for a daily digest of ~2–8 items rather than a 10k-row corpus.

## Suggested first PRs (implementation order)

Independent, reviewable, test-first:

1. **Memory:** `posted_digests.body` + fact-consistency / already-covered read from it. Unblocks every later quality check.
2. **Casing + significance:** #24 #28 #15. One focused list PR with live-API fixtures.
3. **`sig_org_map` + categorize golden tests using resolved providers.** #22.
4. **SPECIALIZED modality allowlist** in `is_noise_model` (known orgs only).
5. **OpenRouter recency + `:free`/`:batch` collapse + `hugging_face_id` enrich.**
6. **Per-source fetch counters** (log now; table when convenient).
7. **HF Daily Papers fetcher** behind the existing noise/significance gates.
8. **Parallel query rewrite + aggregator host denylist.**
9. **WATCH prompt + watchlist from persisted bodies.**

## Success metrics

- **Recall:** of OpenRouter models created in the last 7 days (minus `:batch`/`:free` duplicates and noise), ≥80% appear in that week's digests or are explainably skipped (quant, already covered, not a model).
- **Precision:** ALSO TRACKED stays empty or ≤2 on a typical day; no GGUF/LoRA/SFT leaks; no aggregator `href`.
- **Memory:** fact-consistency warnings fire against *last week's* post, not June 2026.
- **Tiers:** a TTS / image / video release from a KNOWN_ORG can land in SPECIALIZED; an announcement-only frontier lab drop can land in WATCH.
- **Ops:** `publish_runs` plus per-source counts explain a thin day without reading Railway logs.

## Probe log (2026-08-20)

| Source | Result |
|---|---|
| OpenRouter `/api/v1/models` | 200, 416 models, all dated; 8 ≤7d, 19 ≤14d; 167 with `hugging_face_id` |
| HF `api/daily_papers` | 200, 50 papers, org + githubRepo |
| HF `api/papers` | 200, 50; 17 with githubRepo |
| HF org casing | `inclusionai`/`minimaxai`/`bytedance` → 0; canonical slugs → 3 |
| HF `sort=createdAt` | 200, 0-engagement dumps |
| ModelScope `openapi/v1/models` | 200, structured catalog |
| Together / Fireworks / Groq / Replicate | 401 |
| `ollama.com/api/tags` | 200 but not a library catalog |
| LiteLLM pricing JSON | 200, ~3055 keys, public |
| Artificial Analysis / LMArena pages | HTML only |

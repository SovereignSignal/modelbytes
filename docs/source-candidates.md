# ModelBytes Source Candidates

This is the intake queue for possible new discovery sources. The supervisor routine can propose additions here. New fetchers, auth changes, schema changes, and threshold changes should still go through PR review.

Evaluated 2026-08-20 as part of the coverage/quality expansion spec:
[`docs/superpowers/specs/2026-08-20-coverage-and-quality-expansion.md`](./superpowers/specs/2026-08-20-coverage-and-quality-expansion.md).
Fix list/filter leaks (HF org casing, `sig_org_map`, digest-body persistence) before implementing any new fetcher.

## Intake Template

```markdown
### Source name

- Type:
- Candidate URL:
- Why it matters:
- Expected metadata:
- Noise risks:
- Access/auth:
- Suggested first test:
- Recommendation: investigate / reject / implement via PR
```

## Active Candidates

### HuggingFace Daily Papers

- Type: research-paper feed
- Candidate URL: https://huggingface.co/api/daily_papers (also https://huggingface.co/api/papers)
- Why it matters: model-release papers and technical reports surface here before or alongside weights — prime WATCH-tier material and early signal for SPECIALIZED releases. Suggested by Sov 2026-06-10 as a curator research surface; the curator is now retired, so this is orphaned. ClawBytes already ingests the same API. Live probe 2026-08-20: HTTP 200, 50 items, `organization` + `githubRepo` + `upvotes` + `ai_summary`.
- Expected metadata: paper title, authors/orgs, abstract, linked GitHub repo, upvotes, publish date.
- Noise risks: most papers are not model releases (methods, surveys, benchmarks). Gate: only items that map to a concrete model a builder can watch or use (linked repo, known org, or explicit weights/API language). Never benchmark-less experiments.
- Access/auth: public JSON, no secret.
- Suggested first test: parser fixture from a saved `daily_papers` payload; count how many of 50 survive a "maps to a model" heuristic; empty/error response test.
- Recommendation: implement via PR after Phase 0 (see expansion spec). Highest-leverage new fetcher.

### Lab primary feeds (GitHub releases.atom + vendor blogs)

- Type: lab/vendor release feeds
- Candidate URL: GitHub `releases.atom` for orgs already in `KNOWN_ORGS`; vendor blogs already curated in clawbytes `SOURCES.md` (Anthropic news, DeepSeek, Qwen, xAI, Mistral, Cohere, Moonshot, Z.AI)
- Why it matters: closed-lab and weights-pending launches often hit the blog/GitHub days before OpenRouter/HF. This is the WATCH + CLOSED FRONTIER coverage hole the retired curator used to fill. ClawBytes already maintains a clean feed list — ModelBytes should take the *model-lab* subset only, not harness/community feeds.
- Expected metadata: title, date, canonical URL, org.
- Noise risks: SDK/tooling releases, research posts with no model. Filter to model-release language; cap per-org per day at 1.
- Access/auth: public RSS/Atom. GitHub atom does not need a token at this volume.
- Suggested first test: fetch 5–8 feeds, count items in a 14-day window that look like model releases vs SDK bumps.
- Recommendation: implement via PR after HF Papers, starting with 5–8 feeds.

### LiteLLM pricing registry (lifecycle, not catalog)

- Type: inference-catalog diff
- Candidate URL: https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json
- Why it matters: format v3 treats price cuts and platform arrivals as first-class. LiteLLM is a public, sha-gateable JSON of ~3055 SKUs with `input_cost_per_token` / `max_tokens` / provider. ClawBytes already diffs it for new keys. ModelBytes should emit lifecycle entries for models already in recent digests (price move, new provider), not a firehose of new SKUs.
- Expected metadata: model key, provider, input/output price, context, deprecation date.
- Noise risks: 3055 keys; most diffs are long-tail proxies. Restrict to `KNOWN_ORGS` / recently posted names.
- Access/auth: public raw GitHub file, no secret.
- Suggested first test: sha-diff two snapshots; classify diffs as new-key vs price-change; see how many match names from `pending/*.txt`.
- Recommendation: implement via PR as a lifecycle source after Papers + lab feeds.

### ModelScope OpenAPI

- Type: regional model hub
- Candidate URL: https://www.modelscope.cn/openapi/v1/models
- Why it matters: CN labs sometimes ship on ModelScope a day before HF. Live probe 2026-08-20: HTTP 200, structured `id` / `downloads` / `likes` / `license` (e.g. `Qwen/Qwen3.8-27B`).
- Expected metadata: model id, downloads, likes, license, description.
- Noise risks: unknown until a "survive current noise filters" count is done on a full page. Overlap with HF for Qwen/DeepSeek/Z.AI will be high — treat as a miss-catcher, not a second catalog.
- Access/auth: public JSON, no secret. Language mix (zh/en).
- Suggested first test: fetch one page, run through `is_noise_model` / `is_stale_release`, report kept vs dropped. Do not implement until that probe exists.
- Recommendation: investigate (probe noise profile next).

### OpenRouter recency window (not a new source — fetcher change)

- Type: model catalog (existing)
- Candidate URL: https://openrouter.ai/api/v1/models
- Why it matters: live 2026-08-20 snapshot is 416 models, **19 created in 14 days**, 8 in 7 days, 167 with `hugging_face_id`. The news is in the tail, not the catalog. The fetcher currently takes every ID and relies on Postgres dedup. `:free` / `:batch` duplicates of the same model both appear (Gemini 3.7 Flash).
- Expected metadata: already have pricing, context, `created`, architecture.modality; unused: `hugging_face_id`, `canonical_slug`.
- Noise risks: low if gated on `created` ≤14 days and collapsing `:free`/`:batch`.
- Access/auth: public, already fetched.
- Suggested first test: unit test that a 30-day-old OpenRouter row is dropped even if unseen; `:batch` collapses into the base id.
- Recommendation: implement via PR in Phase 0 (no new source class).

## Accepted

### HuggingFace Papers (daily trending)

- Type: research-paper feed (curator research surface, not a monitor.py fetcher)
- Candidate URL: https://huggingface.co/papers
- Why it matters: model-release papers and technical reports surface here before or alongside weights — prime WATCH-tier material (announced/weights-pending) and early signal for SPECIALIZED releases. Suggested by Sov 2026-06-10.
- Expected metadata: paper title, authors/orgs, abstract, linked HF models/datasets, upvotes.
- Noise risks: most papers are not model releases (methods, surveys, benchmarks). The curator's existing bar applies: only items that map to a concrete model a builder can watch or use; never benchmark-less experiments.
- Access/auth: public page; the curator routine's HuggingFace MCP connector also exposes `paper_search` — no new auth.
- Suggested first test: curator checks it during daily research; if it sources a digest entry, note "via HF Papers" in the commit message.
- Recommendation: implemented 2026-06-10 as a curator research surface (prompt addition only, no fetcher code). **Reopened 2026-08-20** — curator is retired; see Active Candidates → HuggingFace Daily Papers.

## Rejected

### HF models `sort=createdAt` firehose

- Type: model catalog
- Candidate URL: https://huggingface.co/api/models?sort=createdAt&direction=-1
- Why it matters: would catch brand-new repos minutes after create.
- Expected metadata: id, createdAt, likes, downloads, tags.
- Noise risks: live probe 2026-08-20 returned anonymous 0-like LoRA/dump repos. Existing `is_noise_model` engagement gate would drop almost all of them; the rest would be spam.
- Access/auth: public.
- Suggested first test: n/a (rejected on probe).
- Recommendation: reject. Keep trending + per-org + top text-gen.

### Together / Fireworks / Groq / Replicate model lists

- Type: inference platform catalogs
- Candidate URL: respective `/v1/models` endpoints
- Why it matters: API-availability signal (⚡) for models we already track.
- Expected metadata: model ids, sometimes context.
- Noise risks: duplicate of OpenRouter for most frontier SKUs; would need secrets.
- Access/auth: all four returned HTTP 401 without a key (2026-08-20). Fails the source-growth access rubric.
- Suggested first test: n/a.
- Recommendation: reject as fetchers. OpenRouter already covers hosted availability.

### LMArena / Artificial Analysis HTML leaderboards

- Type: community trend / benchmark surface
- Candidate URL: https://artificialanalysis.ai/models , HF Space `lmarena-ai/arena-leaderboard`
- Why it matters: newly submitted model IDs sometimes appear here first.
- Expected metadata: model name, score, date (when present).
- Noise risks: HTML-only (2026-08-20); ClawBytes already passed LMArena ("no machine source since the HF space went stale").
- Access/auth: public HTML, unstable.
- Suggested first test: n/a until a JSON endpoint exists.
- Recommendation: reject until they publish a stable machine-readable board.

### `ollama.com/api/tags` as a library catalog

- Type: local-LLM registry
- Candidate URL: https://ollama.com/api/tags
- Why it matters: current Ollama fetcher is an HTML scrape with no dates.
- Expected metadata: name, modified_at, size.
- Noise risks: live 200 response looks like a node-local tag list (unstable set of ~19 models), not the public library.
- Access/auth: public but wrong resource.
- Suggested first test: n/a.
- Recommendation: reject this URL. Revisit Ollama only if a dated public library API appears.

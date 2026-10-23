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

### Z.ai Hugging Face org (`zai-org`)

- Type: existing HF org watch
- Candidate URL: `https://huggingface.co/api/models?author=zai-org`
- Why it matters: Z.ai specialist releases (GLM-Legal and the next one) land on this org when weights are published.
- Probe 2026-10-15: `author=zai-org` returns models (`zai-org/GLM-5.3` and the rest). `author=z-ai` returns `[]`. OpenRouter still uses the `z-ai` id; that slug stays in `KNOWN_ORGS` and is not an HF author. `MAJOR_HF_ORGS` watches `zai-org` only.
- GLM-Legal itself had no Hub repo under that name on 2026-10-15 (API access is application-only; the announcement is `https://z.ai/blog/glm-legal`). The org watch picks the repo up when it exists. Until then the AlphaSignal cross-check is what flags the name.
- Recommendation: implemented. No new secret.

### @ArtificialAnlys on X

- Type: ranking and launch posts
- Candidate URL: `GET https://api.x.com/2/users/1743487864934162432/tweets` (@ArtificialAnlys, id resolved 2026-10-15)
- Why it matters: closed and specialist models show up as arena rankings (HeyGen Voice at #1 on the voice arena) before they are a fresh row in a registry.
- Access/auth: `MODELBYTES_X_BEARER_TOKEN`. Unset skips the watch. There is no X client library in this repo; the call is one `requests` GET.
- Cost: X API v2 pay-per-use, Posts: Read at $0.005 per post returned (docs.x.com pricing, checked 2026-10). `max_results=10` (the API rejects values under 5; 10 covers a thread). A full page is $0.05 per run, about $1.50/month if every day returns 10 posts. The same post is not rebilled inside a UTC day. The user id is pinned, so there is no $0.010 username lookup and no expansions. No monthly minimum.
- Noise: only the first model name in a post that reads as a ranking or a launch. Retweets are dropped. Replies stay, because the ranking is often a thread reply.
- Recommendation: implemented, off until the token is set.

### fal.ai model catalog

- Type: hosted model catalog (launch signal for closed and hosted models)
- Candidate URL: `GET https://api.fal.ai/v1/models?limit=100[&cursor=<next_cursor>]`, and `&sort=recent` once the baseline exists
- Why it matters: closed and hosted launches (Krea 3, FLUX.3 Klein, OmniHuman V2, Mirelo SFX2.0, NVIDIA Nemotron 3.5 Super) land on fal the day they ship and never reach Hugging Face or the OpenRouter text list.
- Probe 2026-10-16: public, no key. About 1,500 endpoints over 16 pages (~8s). The default order is not by date, so the first run reads every page (cap 25) until `has_more` is false. `metadata.date` is the fal launch date; `license_type` is fal's commercial-use flag, not a weights license, so rows are not marked open or closed.
- Probe 2026-10-23: `sort=recent` orders by publication date (creation date when unpublished), before pagination, and echoes `ordering.sort == "recent"`. Cursors keep the sort. The response does not carry the publication date, and `metadata.date` is the creation date, so the order is only roughly by date: page 1 ran 2026-10-22 back to 2026-09-15 with rows created on 2026-09-15 and 2026-10-05 among the newest. Page 2 topped out at 2026-09-17. Once a `fal/` key is in the `models` table and a last good full catalog is stored, the fetcher reads this list until every row on a page is past the 14-day clock (cap 3 pages, 2 requests on most days). If fal does not echo the sort or returns nothing, it reads the full catalog. An HTTP failure goes straight to the last good fallback and skips the 16-request crawl to the same host. The recent list never replaces last good.
- Grouping: one candidate per launch. Endpoints join when they share `metadata.group.key` or a path key (endpoint id with trailing task segments such as `edit`, `realtime`, `text-to-image`, `*-to-*` stripped; `fal-ai/<vendor>/...` is the same family as `<vendor>/...`). Name is the shortest display name with task suffixes removed.
- Noise rule: skip `kind=training`, category `training`/`workflow`, non-active status, and ids or names that are background removal, upscaling / super-resolution, LoRA trainers, workflows, or routers. A launch posts only when its newest endpoint is inside the digest window (`DIGEST_FRESHNESS_DAYS`) and its oldest endpoint is inside the 14-day catalog clock, so a new endpoint on an old family (H3 Max, Meshy 6 retexture) is not a launch.
- Seen ids: `fal/<key>` rows in the existing `models` table, recorded like the Artificial Analysis baseline (catalog minus today's candidates; preview never writes). A candidate is marked seen only when the published digest includes it. A launch that matches a row from OpenRouter, Hugging Face, or Artificial Analysis fills that row's gaps, keeps its `fal-release` trait, and does not replace its URL.
- Recent names: a launch whose name is already in a recent digest is skipped, unless some of its endpoints name that model plus a variant or version word (`turbo`, `max`, `pro`, `mini`, `flash`, `lite`, `ultra`, `plus`, `nano`, `fast`, `hd`, or a token that starts with a digit). Those endpoints post as the variant. HeyGen Voice Turbo (2026-10-21) shares the `heygen-voice` key with HeyGen Voice (printed 2026-10-15) and was dropped before this rule.
- Recommendation: implemented. No new secret and no new flag.

### AlphaSignal public RSS

- Type: coverage cross-check, not a publisher
- Candidate URL: `https://alphasignal.ai/feed.xml` (also `https://alphasignal.ai/atom.xml`; the archive page at `https://alphasignal.ai/archive` links both)
- Probe 2026-10-15: HTTP 200, `application/rss+xml`, 18 items, newest dated 2026-10-15. This is a per-article feed, not one email issue. The daily issue is items from the digest day and the previous UTC day, so one midnight article does not hide the previous day's model notes. No key and no per-request cost.
- What it does: flags model names in that issue that are absent from the last 14 posted digests (ops alert, plus a log line). It does not add them to the digest. Preview does not alert. A dead feed is a source-health error and does not page about coverage.
- Substack (`alphasignal.substack.com/feed`, `alphasignalai.substack.com/feed`) still answers but is not the current site. Feeder has no first-party RSS of its own; the site does.
- Recommendation: implemented.

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

### LMArena HTML leaderboard

- Type: community trend / benchmark surface
- Candidate URL: HF Space `lmarena-ai/arena-leaderboard`
- Why it matters: newly submitted model IDs sometimes appear here first.
- Expected metadata: model name, score, date (when present).
- Noise risks: HTML-only (2026-08-20); ClawBytes already passed LMArena ("no machine source since the HF space went stale").
- Access/auth: public HTML, unstable.
- Suggested first test: n/a until a JSON endpoint exists.
- Recommendation: reject until they publish a stable machine-readable board.

### Artificial Analysis — implemented

The 2026-08-20 "reject, HTML-only" note is obsolete. The free Data API
(`GET /api/v2/language/models/free` and the five `/api/v2/media/*/models/free`
lanes) is wired as an optional source. Legacy `/api/v2/data/*` retires
2026-11-04. The key is `MODELBYTES_ARTIFICIAL_ANALYSIS_API_KEY`; unset skips
the source. Seen ids are `aa/<slug>` rows in the existing `models` table.
A row the AA source merges into keeps its `aa-release` trait, so an
unknown-org row stays significant (Inception Mercury 3, 2026-10-16).

A board debut of a model that is old elsewhere (inclusionAI Ming-3.0-tts:
Hub repo created 2026-09-22, top 3 on the AA TTS board 2026-10-22) is
visible only through this key. There is no keyless AA source; see
Rejected → Artificial Analysis public pages.

### Artificial Analysis public pages, sitemap, and leaderboard HTML

- Type: keyless leaderboard scrape (text, image, video, TTS, STT boards)
- Why it matters: would catch board debuts without the Data API key.
- Access/auth: public HTML, but the Website Terms of Use (v1.0, revised 2024-04-28, checked 2026-10-23) forbid it. §3.3(b) bars "software or automated agents or scripts ... to generate automated searches, requests, or queries to (or to strip, scrape, or mine data from) the Site", with an exception only for public search-engine spiders. §2.1 limits the license to personal, noncommercial use. No RSS or changelog feed exists.
- Recommendation: reject. The free Data API with a key is the permitted route.

### fal blog RSS and fal sitemap

- Type: launch announcements / page index
- Probe 2026-10-23: the blog feed has 15 items, months apart (newest 2026-10-20 "EAGLE-4 Technical Deepdive", next 2026-09-17). None of the 2026-10-16 to 10-22 launches (FLUX 3 Max, Kling 4.0, Fibo 2.0, HeyGen Video, HeyGen Voice Turbo, Ideogram Vector) has a post. The sitemap lists 1,729 URLs with no `lastmod`, so it cannot date a launch.
- Recommendation: reject. The catalog API with `sort=recent` dates every endpoint.

### `ollama.com/api/tags` as a library catalog

- Type: local-LLM registry
- Candidate URL: https://ollama.com/api/tags
- Why it matters: current Ollama fetcher is an HTML scrape with no dates.
- Expected metadata: name, modified_at, size.
- Noise risks: live 200 response looks like a node-local tag list (unstable set of ~19 models), not the public library.
- Access/auth: public but wrong resource.
- Suggested first test: n/a.
- Recommendation: reject this URL. Revisit Ollama only if a dated public library API appears.

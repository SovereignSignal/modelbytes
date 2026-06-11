# ModelBytes Source Candidates

This is the intake queue for possible new discovery sources. The supervisor routine can propose additions here. New fetchers, auth changes, schema changes, and threshold changes should still go through PR review.

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

No active candidates yet.

## Accepted

### HuggingFace Papers (daily trending)

- Type: research-paper feed (curator research surface, not a monitor.py fetcher)
- Candidate URL: https://huggingface.co/papers
- Why it matters: model-release papers and technical reports surface here before or alongside weights — prime WATCH-tier material (announced/weights-pending) and early signal for SPECIALIZED releases. Suggested by Sov 2026-06-10.
- Expected metadata: paper title, authors/orgs, abstract, linked HF models/datasets, upvotes.
- Noise risks: most papers are not model releases (methods, surveys, benchmarks). The curator's existing bar applies: only items that map to a concrete model a builder can watch or use; never benchmark-less experiments.
- Access/auth: public page; the curator routine's HuggingFace MCP connector also exposes `paper_search` — no new auth.
- Suggested first test: curator checks it during daily research; if it sources a digest entry, note "via HF Papers" in the commit message.
- Recommendation: implemented 2026-06-10 as a curator research surface (prompt addition only, no fetcher code). Promote to a monitor.py fetcher only if the curator consistently finds digest-worthy items here.

## Rejected

No rejected candidates yet.

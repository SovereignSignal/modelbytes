"""Post-QA model release forwarding for ModelBytes.

Pure qualification / identity / payload helpers, plus a Postgres outbox.
Nothing here runs during ``--preview`` or unless forwarding is explicitly
armed (``MODELBYTES_RELEASE_FORWARDING=1`` plus ``RELEASE_EVENTS_URL`` and
``RELEASE_EVENTS_TOKEN``). There is no scan of historical digests: the first
armed run only retries rows this process has queued, which is none.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

from release_events import EmitResult, emit_release_event

# Catalog prefixes that sometimes wrap an org/name id. Stripped only when a
# real org segment remains, so `huggingface/qwen/qwen3-32b` matches `qwen/qwen3-32b`.
_CATALOG_PREFIXES = frozenset({"huggingface", "openrouter", "ollama", "hf"})

# Quant / fine-tune tails removed from the identity base. Capability tiers
# (instruct, chat, it, base, …) stay — collapse_variants treats those as
# distinct models, and this module follows that policy.
_QUANT_SUFFIXES = (
    "gguf", "awq", "gptq", "onnx", "imatrix", "exl2",
    "fp8", "fp16", "bf16", "fp4", "int8", "int4", "int5",
    "w4a4", "w8a8", "w4a16", "w8a16", "nvfp4",
    "lora", "qlora",
)

_DERIVATIVE_MARKERS = (
    "-gguf", "_gguf", ".gguf", "-awq", "-gptq", "-onnx", "_onnx",
    "-lora", "_lora", "lora-", "-qlora",
    "-finetune", "-finetuned", "_finetuned",
    "-sft", "_sft", "-dpo", "_dpo", "-grpo", "_grpo",
    "-fp8", "-fp16", "-bf16", "-int8", "-int4", "-fp4",
    "-w4a4", "-bnb-", "abliterated", "obliterated", "-merged",
)

_PRICE_TRAITS = frozenset({
    "price_change", "pricing_change", "availability_change",
    "pricing_only", "availability_only",
})

# A release note that is itself a price or availability change. Ordinary
# "Pricing: $0.50/1M" spec lines do not match.
_PRICE_ONLY_RE = re.compile(
    r"(?i)(?:"
    r"\bprice(?:s|d|ing)?[- ]only\b"
    r"|\bavailability[- ]only\b"
    r"|\bprice change\b"
    r"|\bpricing update\b"
    r"|\bavailability change\b"
    r"|\bnow cheaper\b"
    r"|\bprice drop\b"
    r"|\bprice(?:s|d|ing)? (?:cut|reduced|increased|changed)\b"
    r")"
)

_TERMINAL = frozenset({"delivered", "duplicate", "rejected"})

OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS release_event_outbox (
    id TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    status TEXT NOT NULL,
    attempts INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

_INSERT_SQL = """
INSERT INTO release_event_outbox (id, payload, status, attempts, updated_at)
VALUES (%s, %s::jsonb, 'pending', 0, NOW())
ON CONFLICT (id) DO NOTHING
"""

_SELECT_STATUS_SQL = """
SELECT status, attempts FROM release_event_outbox WHERE id = %s
"""

_SELECT_PENDING_SQL = """
SELECT id, payload, attempts
FROM release_event_outbox
WHERE status = 'pending'
ORDER BY updated_at
"""

_UPDATE_SQL = """
UPDATE release_event_outbox
SET status = %s, attempts = %s, updated_at = NOW()
WHERE id = %s
"""


def forwarding_enabled() -> bool:
    """True only when the flag, URL, and token are all set.

    The flag must be the string ``1``. URL and token alone do nothing, so a
    deploy can ship with the endpoint configured and stay inert.
    """
    flag = os.environ.get("MODELBYTES_RELEASE_FORWARDING", "").strip()
    url = os.environ.get("RELEASE_EVENTS_URL", "").strip()
    token = os.environ.get("RELEASE_EVENTS_TOKEN", "").strip()
    return flag == "1" and bool(url) and bool(token)


def normalized_model_key(name: str) -> Tuple[str, str]:
    """Return ``(org, base)``, both lowercased.

    The base keeps version, size, and capability tiers (``-instruct``,
    ``-chat``). It drops a leading catalog prefix, OpenRouter ``:free`` /
    ``:batch`` suffixes, and quant/LoRA tails so the same weights seen on
    Hugging Face and OpenRouter share one id.
    """
    import monitor

    raw = (name or "").strip().lstrip("~")
    raw = monitor._openrouter_base_id(raw)
    parts = [p for p in raw.split("/") if p]
    if len(parts) >= 3 and parts[0].lower() in _CATALOG_PREFIXES:
        parts = parts[1:]
    if len(parts) >= 2:
        org, seg = parts[0], "/".join(parts[1:])
    elif parts:
        org, seg = "", parts[0]
    else:
        org, seg = "", ""
    return org.lower(), _normalize_base_segment(seg)


def _normalize_base_segment(seg: str) -> str:
    s = (seg or "").lower().replace("_", "-")
    s = re.sub(r":(free|batch|latest)$", "", s)
    s = re.sub(r"-latest$", "", s)
    changed = True
    while changed:
        changed = False
        for suf in _QUANT_SUFFIXES:
            for tail in (f"-{suf}", f".{suf}"):
                if s.endswith(tail):
                    s = s[: -len(tail)]
                    changed = True
    return re.sub(r"-{2,}", "-", s).strip("-.")


def event_id(model) -> str:
    """Stable observation id: ``model:<org>/<normalized-base>``.

    Does not include the catalog, the source URL, or the release date.
    """
    name = model if isinstance(model, str) else getattr(model, "name", "")
    org, base = normalized_model_key(name)
    ident = f"{org}/{base}" if org else base
    return f"model:{ident}"


def explicit_version(model) -> str:
    """Version/size drawn from the name, else the normalized model id.

    Never a calendar date and never the literal ``new``.
    """
    name = model if isinstance(model, str) else getattr(model, "name", "")
    org, base = normalized_model_key(name)
    norm_id = f"{org}/{base}" if org else base
    if not norm_id:
        return "unknown"
    import monitor

    size = monitor._param_size_from_name(name or base)
    size_l = size.lower() if size else ""
    seg = base
    if size_l and size_l in seg:
        seg = seg.replace(size_l, " ", 1)
    ver = ""
    dotted = re.search(r"(?<![a-z0-9])(\d+\.\d+(?:\.\d+)*)(?![a-z0-9])", seg)
    if dotted:
        ver = dotted.group(1)
    else:
        prefixed = re.search(r"(?<![a-z0-9])v(\d+(?:\.\d+)*)(?![a-z0-9])", seg)
        if prefixed:
            ver = prefixed.group(1)
        else:
            lone = re.search(r"(?<![a-z0-9])(\d+)(?![a-z0-9])", seg)
            if lone:
                ver = lone.group(1)
    if ver and size_l:
        version = f"{ver}-{size_l}"
    elif size_l:
        version = size_l
    elif ver:
        version = ver
    else:
        version = norm_id
    if version == "new" or re.fullmatch(r"\d{4}-\d{2}-\d{2}", version):
        version = norm_id
    release_date = getattr(model, "release_date", None) if not isinstance(model, str) else None
    if release_date and version == str(release_date)[:10]:
        version = norm_id
    return version


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _is_derivative(name: str) -> bool:
    import monitor

    if monitor._QUANT_NAME_RE.search(name or ""):
        return True
    low = (name or "").lower()
    return any(marker in low for marker in _DERIVATIVE_MARKERS)


def _is_price_only(model) -> bool:
    traits = {
        str(t).lower()
        for t in (getattr(model, "unique_traits", None) or [])
    }
    if traits & _PRICE_TRAITS:
        return True
    return bool(_PRICE_ONLY_RE.search(getattr(model, "description", "") or ""))


def _is_catalog_listing(model) -> bool:
    import monitor

    src = (getattr(model, "source", "") or "").lower()
    if src == "openrouter":
        return True
    host = monitor._url_host(getattr(model, "url", "") or "")
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def _primary_url(model) -> Optional[str]:
    """First non-catalog, non-aggregator http(s) URL on the model."""
    import monitor

    for candidate in (
        getattr(model, "canonical_url", None),
        getattr(model, "url", None),
    ):
        if not candidate or not isinstance(candidate, str):
            continue
        if monitor._host_is_aggregator(candidate):
            continue
        host = monitor._url_host(candidate)
        if not host:
            continue
        if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
            continue
        if candidate.lower().startswith(("http://", "https://")):
            return candidate
    return None


def _is_official_org(name: str) -> bool:
    import monitor

    org, _base = normalized_model_key(name)
    if not org:
        return False
    known = {o.lower() for o in monitor.KNOWN_ORGS}
    known.update(o.lower() for o in monitor.MAJOR_HF_ORGS)
    return org in known


def _is_web_discovery(model) -> bool:
    src = (getattr(model, "source", "") or "").lower()
    traits = {
        str(t).lower()
        for t in (getattr(model, "unique_traits", None) or [])
    }
    return src == "discovery" or "web_discovery" in traits


def _undated_exception(model) -> bool:
    """Undated models stay out unless a high-confidence primary source says so.

    Policy: official org + web-discovery hit + confidence ``high`` + a
    primary-source URL. A missing date on a Hugging Face catalog row is not
    enough.
    """
    if _parse_date(getattr(model, "release_date", None)) is not None:
        return False
    if (getattr(model, "confidence", "") or "").lower() != "high":
        return False
    if not _is_official_org(getattr(model, "name", "")):
        return False
    if not _is_web_discovery(model):
        return False
    return _primary_url(model) is not None


def _announces_primary(model, today: Optional[str]) -> bool:
    """True when this row is a primary-source announcement others can dedupe against."""
    import monitor

    name = getattr(model, "name", "")
    if _is_catalog_listing(model):
        return False
    if monitor.is_openrouter_serving_sku(name) or _is_derivative(name):
        return False
    if _is_price_only(model) or not _primary_url(model):
        return False
    if _parse_date(getattr(model, "release_date", None)) is not None:
        return not monitor.is_stale_release(model.release_date, today=today)
    return _undated_exception(model)


def qualification_reason(model, today: Optional[str] = None,
                         primary_keys=None) -> Optional[str]:
    """Why this model must not become a release event, or None if it qualifies.

    Dated releases need a parseable date inside the existing freshness window
    and a primary URL (Hugging Face, vendor, paper — not OpenRouter). Undated
    rows qualify only via :func:`_undated_exception`. Quantizations, GGUF/AWQ
    builds, fine-tunes, serving SKUs, and price/availability changes never
    qualify. An OpenRouter row whose normalized base was already announced by
    a primary source in the same batch is a catalog duplicate.
    """
    import monitor

    name = getattr(model, "name", "") or ""
    if monitor.is_openrouter_serving_sku(name):
        return "serving_sku"
    if _is_derivative(name):
        return "derivative"
    if _is_price_only(model):
        return "price_or_availability"
    parsed = _parse_date(getattr(model, "release_date", None))
    if parsed is not None:
        if monitor.is_stale_release(model.release_date, today=today):
            return "stale"
    elif not _undated_exception(model):
        return "undated"
    if not _primary_url(model):
        return "no_primary_url"
    if primary_keys is not None and _is_catalog_listing(model):
        if normalized_model_key(name) in primary_keys:
            return "catalog_duplicate"
    return None


def select_qualified(models: Sequence, today: Optional[str] = None) -> List:
    """Qualified models, one per event id, catalog duplicates removed."""
    models = list(models or [])
    primary_keys = {
        normalized_model_key(m.name)
        for m in models
        if _announces_primary(m, today)
    }
    out = []
    seen = set()
    for model in models:
        if qualification_reason(model, today=today, primary_keys=primary_keys):
            continue
        eid = event_id(model)
        if eid in seen:
            continue
        seen.add(eid)
        out.append(model)
    return out


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def is_surfaced(model, message: str) -> bool:
    """True when the published digest actually contains this model.

    Matches a primary or catalog URL inside the body, or a bold entry name
    against the normalized base. Models the writer dropped do not forward.
    """
    if not message:
        return False
    for candidate in (
        getattr(model, "canonical_url", None),
        getattr(model, "url", None),
        _primary_url(model),
    ):
        if candidate and candidate in message:
            return True
    import monitor

    _org, base = normalized_model_key(getattr(model, "name", ""))
    seg = (getattr(model, "name", "") or "").split("/")[-1]
    targets = {t for t in (_compact(base), _compact(seg)) if len(t) >= 4}
    if not targets:
        return False
    for entry in monitor._ENTRY_RE.findall(message):
        compacted = _compact(entry)
        if compacted in targets:
            return True
        for target in targets:
            if len(target) >= 8 and target in compacted:
                return True
    return False


def build_payload(model) -> dict:
    """Release Events v1 body. Pricing stays out; the date lives in metadata."""
    org, base = normalized_model_key(getattr(model, "name", ""))
    model_id = f"{org}/{base}" if org else base
    release_raw = getattr(model, "release_date", None)
    release_date = str(release_raw)[:10] if _parse_date(release_raw) else None
    urls = []
    for candidate in (
        getattr(model, "canonical_url", None),
        getattr(model, "url", None),
    ):
        if candidate and candidate not in urls:
            urls.append(candidate)
    primary = _primary_url(model) or (urls[0] if urls else "")
    version = explicit_version(model)
    if version in ("new", release_date):
        version = model_id or "unknown"
    name = (getattr(model, "name", "") or "").split("/")[-1] or model_id
    return {
        "id": f"model:{model_id}" if model_id else event_id(model),
        "kind": "model",
        "name": name,
        "version": version,
        "source": "modelbytes",
        "source_type": getattr(model, "source", None) or "model",
        "url": primary,
        "published_at": release_date,
        "summary": getattr(model, "description", None) or "",
        "metadata": {
            "provider": getattr(model, "provider", None),
            "model_id": model_id,
            "release_date": release_date,
            "catalog": getattr(model, "source", None),
            "urls": urls,
            "confidence": getattr(model, "confidence", None) or "medium",
        },
    }


def _coerce_payload(payload):
    if isinstance(payload, str):
        return json.loads(payload)
    if isinstance(payload, dict):
        return payload
    return json.loads(str(payload))


def _with_outbox(fn):
    """Run `fn(cursor)` inside a committed transaction. None without DATABASE_URL."""
    import monitor

    if not monitor.DATABASE_URL:
        return None
    conn = monitor._db_connect(connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute(OUTBOX_DDL)
            result = fn(cur)
        conn.commit()
        return result
    finally:
        conn.close()


def _enqueue(payload) -> Tuple[str, int]:
    """Insert a pending row. Returns ``(state, attempts_so_far)``.

    ``state`` is ``new``, ``pending`` (already queued), or ``terminal``.
    Without DATABASE_URL the event is not stored and ``state`` is ``new``
    so the caller can still try one best-effort POST.
    """
    import monitor

    if not monitor.DATABASE_URL:
        return "new", 0

    def op(cur):
        cur.execute(_INSERT_SQL, (payload["id"], json.dumps(payload)))
        if cur.rowcount == 1:
            return "new", 0
        cur.execute(_SELECT_STATUS_SQL, (payload["id"],))
        row = cur.fetchone()
        if not row:
            return "new", 0
        status, attempts = row[0], int(row[1] or 0)
        if status in _TERMINAL:
            return "terminal", attempts
        return "pending", attempts

    return _with_outbox(op)


def _load_pending() -> List[tuple]:
    def op(cur):
        cur.execute(_SELECT_PENDING_SQL)
        return list(cur.fetchall())

    rows = _with_outbox(op)
    return rows or []


def _status_for(outcome: str) -> str:
    if outcome == "delivered":
        return "delivered"
    if outcome == "duplicate":
        return "duplicate"
    if outcome == "rejected":
        return "rejected"
    return "pending"


def _record(event_id: str, result: EmitResult, attempts_before: int) -> None:
    import monitor

    if not monitor.DATABASE_URL:
        return
    status = _status_for(result.outcome)
    attempts = int(attempts_before or 0) + 1

    def op(cur):
        cur.execute(_UPDATE_SQL, (status, attempts, event_id))

    _with_outbox(op)


def _log_result(event_id: str, result: EmitResult) -> None:
    extra = f" status={result.status}" if result.status else ""
    print(
        f"release forwarding: {result.outcome}{extra} {event_id}",
        file=sys.stderr,
    )


def retry_pending_release_events() -> int:
    """POST each pending outbox row once. No-op when forwarding is off.

    Safe on seed, quiet-day, and no-models returns: callers invoke this at
    the start of ``main`` before those branches. Does not read
    ``posted_digests`` or the seen-model table, so arming the flag replays
    nothing that was not queued by an earlier armed run.
    """
    if not forwarding_enabled():
        return 0
    import monitor

    if not monitor.DATABASE_URL:
        print(
            "release forwarding: enabled but DATABASE_URL unset — outbox retry skipped",
            file=sys.stderr,
        )
        return 0
    try:
        rows = _load_pending()
    except Exception as exc:
        print(
            f"[WARN] release outbox retry failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 0
    if not rows:
        print("release forwarding: no pending events", file=sys.stderr)
        return 0
    print(
        f"release forwarding: retrying {len(rows)} pending event(s)",
        file=sys.stderr,
    )
    n = 0
    for event_id_value, payload, attempts in rows:
        try:
            body = _coerce_payload(payload)
            result = emit_release_event(body)
        except Exception as exc:
            result = EmitResult("retryable", detail=type(exc).__name__)
            body = {"id": event_id_value}
        try:
            _record(body.get("id") or event_id_value, result, int(attempts or 0))
        except Exception as exc:
            print(
                f"[WARN] release outbox update failed: {type(exc).__name__}",
                file=sys.stderr,
            )
        _log_result(body.get("id") or event_id_value, result)
        n += 1
    return n


def forward_release_events(models, message: str = "", *, preview: bool = False,
                           today: Optional[str] = None,
                           qa_errors: Optional[Sequence[str]] = None) -> List[EmitResult]:
    """Forward models that survived QA, were published, and qualify.

    Returns immediately on preview, on any qa error, and when forwarding is
    disabled — no HTTP and no outbox writes. Never raises.
    """
    if preview or qa_errors or not forwarding_enabled():
        return []
    try:
        qualified = [
            model for model in select_qualified(models, today=today)
            if is_surfaced(model, message or "")
        ]
        if not qualified:
            print("release forwarding: nothing qualified", file=sys.stderr)
            return []
        noun = "event" if len(qualified) == 1 else "events"
        print(
            f"release forwarding: sending {len(qualified)} {noun}",
            file=sys.stderr,
        )
        results: List[EmitResult] = []
        for model in qualified:
            payload = build_payload(model)
            state, attempts = "new", 0
            try:
                state, attempts = _enqueue(payload)
            except Exception as exc:
                print(
                    f"[WARN] release outbox enqueue failed: {type(exc).__name__}",
                    file=sys.stderr,
                )
                state, attempts = "new", 0
            if state == "terminal":
                print(
                    f"release forwarding: skip {payload['id']} (already recorded)",
                    file=sys.stderr,
                )
                continue
            result = emit_release_event(payload)
            try:
                _record(payload["id"], result, attempts)
            except Exception as exc:
                print(
                    f"[WARN] release outbox update failed: {type(exc).__name__}",
                    file=sys.stderr,
                )
            _log_result(payload["id"], result)
            results.append(result)
        return results
    except Exception as exc:
        print(
            f"[WARN] release forwarding failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return []

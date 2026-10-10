"""Best-effort push of a published ModelBytes digest into the AI Wire registry.

Inert unless ``AI_WIRE_ENABLED`` is exactly ``1`` and both ``AI_WIRE_URL`` and
``AI_WIRE_INGEST_TOKEN`` are set. The daily publisher calls
:func:`push_published_digest` only after Telegram has accepted the post.
A receiver outage is logged and swallowed.

``python monitor.py --ai-wire-backfill`` reprints recent digests from
``posted_digests`` (dry-run). ``--apply`` POSTs them. There is no new table:
the published HTML and ``publish_runs.telegram_message_id`` are the history.
"""
from __future__ import annotations

import html
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime
from typing import List, Optional, Sequence

TIMEOUT_SECONDS = 5
MAX_ATTEMPTS = 2  # one try plus one retry
BATCH_LIMIT = 100

_HF_NON_MODEL = frozenset({
    "datasets", "spaces", "papers", "collections", "models", "docs",
})
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MODALITY = {
    "text": "text",
    "language": "text",
    "llm": "text",
    "multimodal": "multimodal",
    "image": "image",
    "text-to-image": "image",
    "image-editing": "image",
    "image-to-image": "image",
    "video": "video",
    "text-to-video": "video",
    "image-to-video": "video",
    "audio": "audio",
    "text-to-speech": "audio",
    "speech-to-text": "audio",
    "speech-to-speech": "audio",
    "text-to-audio": "audio",
    "music": "audio",
}

_HEADER_RE = re.compile(r"━━━\s*<b>([^<]+)</b>")
_ENTRY_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:•[ \t]*)?<b>(?P<name>[^<]+)</b>(?P<rest>.*)$",
)
_BULLET_RE = re.compile(
    r'^[ \t]*•\s*<a href="([^"]+)">([^<]+)</a>',
    re.I,
)
_HREF_RE = re.compile(r'<a href="([^"]+)"', re.I)
_ITALIC_RE = re.compile(r"(?is)<i>(.*?)</i>")
_RELEASED_RE = re.compile(
    r"(?i)\bReleased:?\s+([A-Za-z]{3})\.?\s+(\d{1,2})(?:\s*,?\s*(\d{4}))?"
)
_DATELINE_RE = re.compile(
    r"<i>[A-Z][a-z]+day,\s+([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})</i>"
)

_HISTORY_SQL = """
SELECT d.post_date, d.body, d.posted_at, r.telegram_message_id
FROM posted_digests d
LEFT JOIN LATERAL (
    SELECT telegram_message_id
    FROM publish_runs
    WHERE post_date = d.post_date
      AND status = 'posted'
      AND telegram_message_id IS NOT NULL
    ORDER BY run_at DESC
    LIMIT 1
) r ON TRUE
WHERE d.body IS NOT NULL AND btrim(d.body) <> ''
ORDER BY d.post_date DESC
LIMIT %s
"""


def enabled() -> bool:
    """True only when the flag, origin, and bearer token are all set.

    The flag must be the string ``1``. URL and token alone do nothing, so a
    deploy can ship with the endpoint configured and stay inert.
    """
    flag = os.environ.get("AI_WIRE_ENABLED", "").strip()
    url = os.environ.get("AI_WIRE_URL", "").strip()
    token = os.environ.get("AI_WIRE_INGEST_TOKEN", "").strip()
    return flag == "1" and bool(url) and bool(token)


def wire_modality(value) -> Optional[str]:
    """Map a digest modality onto text|image|video|audio|multimodal."""
    key = (value or "").strip().lower().replace("_", "-")
    if not key:
        return None
    if key in _MODALITY:
        return _MODALITY[key]
    if "video" in key:
        return "video"
    if "speech" in key or "audio" in key or key == "tts":
        return "audio"
    if "image" in key:
        return "image"
    return None


def canonical_key(model=None, url: str = "", title: str = "") -> str:
    """``model:<org>/<name>`` when an org is visible, else ``model:<name>``.

    Hugging Face, OpenRouter, and a lab-blog row that carries the same
    org/name collapse. Quant tails and ``:free`` / ``:batch`` suffixes are
    stripped the same way release forwarding already strips them. A bare
    display title does not invent an org.
    """
    import release_forwarding

    name = ""
    if isinstance(model, str):
        name = model
    elif model is not None:
        name = getattr(model, "name", "") or ""
        if not url:
            url = (
                getattr(model, "canonical_url", None)
                or getattr(model, "url", None)
                or ""
            )
    ident = _identity(name, url or "", title or "", release_forwarding)
    return f"model:{ident}" if ident else ""


def _slug(text: str) -> str:
    text = (text or "").lower().replace("_", "-")
    text = re.sub(r"[^a-z0-9.+-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-.")
    return text


def _parts(raw: str, release_forwarding) -> tuple:
    org, base = release_forwarding.normalized_model_key(raw or "")
    return _slug(org), _slug(base)


def _catalog_name(url: str) -> str:
    import monitor

    match = monitor._HF_REPO_RE.search(url or "")
    if match and match.group(1).lower() not in _HF_NON_MODEL:
        return f"{match.group(1)}/{match.group(2)}"
    match = monitor._OR_MODEL_RE.search(url or "")
    if match:
        return match.group(1).strip("/")
    match = monitor._OLLAMA_LIB_RE.search(url or "")
    if match:
        return match.group(1)
    return ""


def _identity(name: str, url: str, title: str, release_forwarding) -> str:
    candidates = []
    if name:
        candidates.append(name)
    catalog = _catalog_name(url)
    if catalog:
        candidates.append(catalog)
    if title:
        candidates.append(title)
    fallback = ""
    for cand in candidates:
        org, base = _parts(cand, release_forwarding)
        if org and base:
            return f"{org}/{base}"
        if base and not fallback:
            fallback = f"{org}/{base}" if org else base
    return fallback


def _plain(value: str) -> str:
    text = re.sub(r"(?is)<[^>]+>", " ", value or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _context_day(message: str, today) -> str:
    if today:
        text = str(today)[:10]
        try:
            datetime.strptime(text, "%Y-%m-%d")
            return text
        except ValueError:
            pass
    match = _DATELINE_RE.search(message or "")
    if not match:
        return ""
    try:
        parsed = datetime.strptime(
            f"{match.group(1)} {int(match.group(2))} {match.group(3)}",
            "%B %d %Y",
        )
    except ValueError:
        return ""
    return parsed.date().isoformat()


def _parse_date(value) -> str:
    if not value:
        return ""
    head = str(value).strip()[:10]
    try:
        datetime.strptime(head, "%Y-%m-%d")
    except ValueError:
        return ""
    return head


def _published_at(model, line: str, today: str) -> str:
    if model is not None:
        parsed = _parse_date(getattr(model, "release_date", None))
        if parsed:
            return parsed
    match = _RELEASED_RE.search(line or "")
    if not match:
        return ""
    month = _MONTHS.get(match.group(1).lower()[:3])
    if not month:
        return ""
    day = int(match.group(2))
    if match.group(3):
        year = int(match.group(3))
    elif today:
        year = int(today[:4])
        digest_month = int(today[5:7])
        if month > digest_month:
            year -= 1
    else:
        return ""
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return ""


def _channel_post_url(message_id) -> str:
    if message_id is None:
        return ""
    text = str(message_id).strip()
    if not text.isdigit() or int(text) <= 0:
        return ""
    return f"https://t.me/ModelBytes/{int(text)}"


def _iso_timestamp(value) -> str:
    if value is None or value == "":
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _best_href(rest: str) -> str:
    """First non-aggregator link in an entry, else the first link.

    A digest line can cite a tracker and a model page. The registry wants
    the model page. An entry whose only link is an aggregator still ships.
    """
    import monitor

    hrefs = []
    for raw in _HREF_RE.findall(rest or ""):
        url = _https(html.unescape(raw.strip()))
        if url.lower().startswith("https://"):
            hrefs.append(url)
    for url in hrefs:
        if not monitor._host_is_aggregator(url):
            return url
    return hrefs[0] if hrefs else ""


def _parse_entries(message: str) -> List[dict]:
    entries = []
    lane = ""
    for line in (message or "").splitlines():
        if "━━━" in line:
            header = _HEADER_RE.search(line)
            if header:
                lane = header.group(1).strip()
                continue
        if "<b>" not in line:
            bullet = _BULLET_RE.search(line)
            if bullet:
                entries.append({
                    "lane": lane,
                    "title": html.unescape(bullet.group(2).strip()),
                    "url": bullet.group(1).strip(),
                    "summary": "",
                    "line": line,
                })
            continue
        match = _ENTRY_RE.match(line)
        if not match:
            continue
        rest = match.group("rest") or ""
        italics = _ITALIC_RE.findall(rest)
        entries.append({
            "lane": lane,
            "title": html.unescape(match.group("name").strip()),
            "url": _best_href(rest),
            "summary": _plain(italics[0]) if italics else "",
            "line": line,
        })
    return entries


def _model_for(title: str, url: str, models):
    if not models:
        return None
    import monitor

    hit = monitor._match_model(title, models)
    if hit is not None:
        return hit
    url_key = (url or "").rstrip("/")
    if not url_key:
        return None
    for model in models:
        for cand in (
            getattr(model, "canonical_url", None),
            getattr(model, "url", None),
        ):
            if cand and str(cand).rstrip("/") == url_key:
                return model
    return None


def _https(url: str) -> str:
    url = (url or "").strip()
    if url.lower().startswith("http://"):
        return "https://" + url.split("://", 1)[1]
    return url


def _item_from_entry(entry: dict, model, *, message_id, posted_at, today: str):
    import monitor

    url = _https(entry.get("url") or "")
    if not url.lower().startswith("https://") and model is not None:
        url = _https(monitor._entry_url(model))
    if not url.lower().startswith("https://"):
        return None
    key = canonical_key(model, url=url, title=entry.get("title") or "")
    if not key or key == "model:":
        return None
    title = (entry.get("title") or "").strip()
    if not title and model is not None:
        title = monitor.clean_display_name(getattr(model, "name", "") or "")
    if not title:
        return None
    item = {
        "source_bot": "modelbytes",
        "kind": "model",
        "canonical_key": key,
        "title": title,
        "url": url,
        "channel": "ModelBytes",
    }
    if entry.get("lane"):
        item["lane"] = entry["lane"]
    rest = key[len("model:"):]
    if "/" in rest:
        item["org"] = rest.split("/", 1)[0]
    summary = entry.get("summary") or ""
    if not summary and model is not None:
        summary = _plain(getattr(model, "description", "") or "")
    summary = summary[:600].strip()
    if summary:
        item["summary"] = summary
    if model is not None:
        modality = wire_modality(getattr(model, "modality", None))
        if modality:
            item["modality"] = modality
    published = _published_at(model, entry.get("line") or "", today)
    if published:
        item["published_at"] = published
    posted = _iso_timestamp(posted_at)
    if posted:
        item["posted_at"] = posted
    post_url = _channel_post_url(message_id)
    if post_url:
        item["channel_post_url"] = post_url
    return item


def _item_score(item: dict) -> tuple:
    import monitor

    host = monitor._url_host(item.get("url") or "")
    primary = 0 if host == "openrouter.ai" or host.endswith(".openrouter.ai") else 1
    return (
        primary,
        1 if item.get("published_at") else 0,
        1 if item.get("summary") else 0,
        1 if item.get("modality") else 0,
        1 if item.get("org") else 0,
    )


def _collapse(items: Sequence[dict]) -> List[dict]:
    order = []
    by_key = {}
    for item in items:
        key = item.get("canonical_key")
        if not key:
            continue
        if key not in by_key:
            order.append(key)
            by_key[key] = dict(item)
            continue
        current = by_key[key]
        if _item_score(item) > _item_score(current):
            winner, extra = dict(item), current
        else:
            winner, extra = dict(current), item
        for field, value in extra.items():
            if winner.get(field) in (None, ""):
                winner[field] = value
        by_key[key] = winner
    return [by_key[key] for key in order]


def items_from_digest(models, message: str, *, message_id=None,
                      posted_at=None, today=None) -> List[dict]:
    """One registry item per model that actually appears in the sent HTML.

    Candidates the writer left out are omitted. The body is clipped with the
    same 4096-character cut Telegram send uses, so a tail that never reached
    the channel is not pushed. Two sections that name the same normalized
    model become one item. The lane is the tier header above the entry that
    wins (a non-OpenRouter URL beats a catalog link).
    """
    import monitor

    message = monitor._truncate_for_telegram(message or "")
    day = _context_day(message or "", today)
    built = []
    for entry in _parse_entries(message or ""):
        model = _model_for(entry["title"], entry["url"], models)
        item = _item_from_entry(
            entry, model,
            message_id=message_id, posted_at=posted_at, today=day,
        )
        if item:
            built.append(item)
    return _collapse(built)


def _endpoint() -> str:
    base = os.environ.get("AI_WIRE_URL", "").strip().rstrip("/")
    suffix = "/api/ingest/items"
    if base.endswith(suffix):
        return base
    return base + suffix


def _upserted(raw: bytes, fallback: int) -> int:
    try:
        data = json.loads((raw or b"").decode("utf-8", errors="replace") or "{}")
    except Exception:
        return fallback
    if isinstance(data, dict) and isinstance(data.get("upserted"), int):
        return data["upserted"]
    return fallback


def _post(url: str, token: str, data: bytes):
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        status = getattr(resp, "status", None)
        if status is None:
            status = resp.getcode()
        return int(status), resp.read()


def push_items(items) -> bool:
    """POST one ``{"items": [...]}`` batch. Never raises.

    Flag-off and an empty list do no I/O. Timeout is 5 seconds. A timeout,
    connection error, 408, 429, or 5xx is tried once more. Other 4xx are not
    retried. Logs ``ai_wire push ok n=`` or ``ai_wire push failed:``.
    """
    try:
        return _push_items(items)
    except Exception as exc:
        print(f"ai_wire push failed: {type(exc).__name__}", file=sys.stderr)
        return False


def _push_items(items) -> bool:
    if not enabled():
        return False
    batch = [item for item in (items or []) if item][:BATCH_LIMIT]
    if not batch:
        return False
    url = _endpoint()
    token = os.environ.get("AI_WIRE_INGEST_TOKEN", "").strip()
    data = json.dumps({"items": batch}).encode("utf-8")
    last = "error"
    for attempt in range(MAX_ATTEMPTS):
        try:
            status, raw = _post(url, token, data)
        except urllib.error.HTTPError as exc:
            if exc.code in (408, 429) or exc.code >= 500:
                last = f"http {exc.code}"
                if attempt == 0:
                    continue
            else:
                print(f"ai_wire push failed: http {exc.code}", file=sys.stderr)
                return False
            print(f"ai_wire push failed: {last}", file=sys.stderr)
            return False
        except (TimeoutError, socket.timeout, urllib.error.URLError, OSError) as exc:
            last = type(exc).__name__
            if attempt == 0:
                continue
            print(f"ai_wire push failed: {last}", file=sys.stderr)
            return False
        if 200 <= status < 300:
            print(f"ai_wire push ok n={_upserted(raw, len(batch))}", file=sys.stderr)
            return True
        last = f"http {status}"
        if attempt == 0 and (status in (408, 429) or status >= 500):
            continue
        print(f"ai_wire push failed: {last}", file=sys.stderr)
        return False
    print(f"ai_wire push failed: {last}", file=sys.stderr)
    return False


def push_published_digest(models, message: str, *, message_id=None,
                          posted_at=None, today=None):
    """Push models included in a digest Telegram already accepted.

    No-op when the flag is off. Never raises.
    """
    try:
        if not enabled():
            return None
        items = items_from_digest(
            models, message or "",
            message_id=message_id, posted_at=posted_at, today=today,
        )
        if not items:
            if (message or "").strip():
                print("ai_wire push skipped: no items", file=sys.stderr)
            return None
        push_items(items)
    except Exception as exc:
        print(f"ai_wire push failed: {type(exc).__name__}", file=sys.stderr)
    return None


def load_history(days: int):
    """Recent posted digest rows, newest first.

    ``None`` when ``DATABASE_URL`` is unset (nothing to read). ``[]`` when
    the query succeeds and no body is stored. Connection and query errors
    propagate so the caller does not describe a failure as missing history.
    """
    import monitor

    if not getattr(monitor, "DATABASE_URL", ""):
        return None
    conn = monitor._db_connect(connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute(_HISTORY_SQL, (int(days),))
            return list(cur.fetchall())
    finally:
        conn.close()


def backfill(days: int = 14, apply: bool = False) -> int:
    """Reprint recent posted digests. POST only when ``apply`` is true.

    Dry-run is the default. ``--apply`` still refuses to POST while the
    flag is off, and prints the same plan. Returns 0 on success, 1 when an
    apply push failed. Missing history is a no-op, not an error.
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 14
    days = max(1, min(days, 90))
    try:
        rows = load_history(days)
    except Exception as exc:
        print(f"ai_wire backfill failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if not rows:
        print("ai_wire backfill: no posted digest history")
        return 0
    posting = bool(apply and enabled())
    if apply and not enabled():
        print(
            "ai_wire backfill: disabled "
            "(set AI_WIRE_ENABLED=1, AI_WIRE_URL, AI_WIRE_INGEST_TOKEN)"
        )
    mode = "apply" if posting else "dry-run"
    print(f"ai_wire backfill {mode} days={days} digests={len(rows)}")
    failures = 0
    for post_date, body, posted_at, message_id in rows:
        day = post_date.isoformat()[:10] if hasattr(post_date, "isoformat") else str(post_date)[:10]
        try:
            items = items_from_digest(
                [], body or "",
                message_id=message_id, posted_at=posted_at, today=day,
            )
        except Exception as exc:
            print(f"ai_wire push failed: {type(exc).__name__}", file=sys.stderr)
            failures += 1
            continue
        post_url = ""
        if items and items[0].get("channel_post_url"):
            post_url = " " + items[0]["channel_post_url"]
        print(f"{day} items={len(items)}{post_url}")
        for item in items:
            print(
                f"  {item['canonical_key']} lane={item.get('lane', '')} "
                f"url={item['url']}"
            )
        if posting and items:
            if not push_items(items):
                failures += 1
    return 1 if failures else 0


def main_backfill(argv: Sequence[str]) -> int:
    """``monitor.py --ai-wire-backfill [--days N] [--apply]``."""
    apply = "--apply" in argv
    days = 14
    if "--days" in argv:
        idx = list(argv).index("--days")
        if idx + 1 >= len(argv):
            print("ai_wire backfill: --days needs an integer", file=sys.stderr)
            return 2
        try:
            days = int(argv[idx + 1])
        except ValueError:
            print("ai_wire backfill: --days needs an integer", file=sys.stderr)
            return 2
    return backfill(days=days, apply=apply)

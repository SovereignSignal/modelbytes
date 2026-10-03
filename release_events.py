"""Best-effort Release Events v1 producer shared by Sov's signal services.

Returns a typed outcome so callers can retry timeouts and 5xx, treat a
duplicate as done, and keep a rejected payload for inspection. The bearer
token is never logged.
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

SCHEMA = "release-event/v1"

DELIVERED = "delivered"
DUPLICATE = "duplicate"
RETRYABLE = "retryable"
REJECTED = "rejected"

_DUPLICATE_MARKERS = frozenset({
    "duplicate",
    "already_seen",
    "already-seen",
    "owned_by_poller",
})


@dataclass(frozen=True)
class EmitResult:
    """Outcome of one POST. `status` is None when no HTTP response arrived."""

    outcome: str
    status: Optional[int] = None
    detail: str = ""


def emit_release_event(event: dict, timeout: int = 5) -> EmitResult:
    """POST one event. Never raises.

    Missing URL/token or a payload missing required fields is `rejected`
    (nothing to retry until configuration or the payload changes). Timeouts,
    connection errors, 408, 429, and 5xx are `retryable`. 409 and a 2xx body
    that says duplicate / owned_by_poller are `duplicate`. Other 2xx are
    `delivered`. Other 4xx are `rejected`.
    """
    url = os.environ.get("RELEASE_EVENTS_URL", "").strip()
    token = os.environ.get("RELEASE_EVENTS_TOKEN", "").strip()
    if not url or not token:
        return EmitResult(REJECTED, detail="not_configured")
    payload = dict(event or {})
    payload["schema"] = SCHEMA
    required = ("id", "kind", "name", "version", "source", "url")
    if any(not payload.get(k) for k in required):
        return EmitResult(REJECTED, detail="invalid_payload")
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
            return _from_status(int(status), resp.read())
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        return _from_status(int(exc.code), raw or b"")
    except (TimeoutError, socket.timeout, urllib.error.URLError, OSError) as exc:
        # Exception type only: the message can echo the request URL.
        print(f"[WARN] release-event forward failed: {type(exc).__name__}")
        return EmitResult(RETRYABLE, detail=type(exc).__name__)


def _from_status(status: int, body: bytes) -> EmitResult:
    if status == 409 or (200 <= status < 300 and _body_says_duplicate(body)):
        return EmitResult(DUPLICATE, status=status)
    if 200 <= status < 300:
        return EmitResult(DELIVERED, status=status)
    if status in (408, 429) or 500 <= status <= 599:
        return EmitResult(RETRYABLE, status=status)
    if 400 <= status <= 499:
        return EmitResult(REJECTED, status=status)
    return EmitResult(RETRYABLE, status=status)


def _body_says_duplicate(body: bytes) -> bool:
    if not body:
        return False
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    for key in ("status", "result", "outcome", "reason"):
        val = data.get(key)
        if isinstance(val, str) and val.strip().lower() in _DUPLICATE_MARKERS:
            return True
    return False

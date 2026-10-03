#!/usr/bin/env python3
"""ModelBytes runner with best-effort immediate Release Events forwarding."""
from __future__ import annotations
import hashlib
import monitor
from release_events import emit_release_event

_original=monitor.summarize_models

def _event_id(m):
    raw=f"{m.provider}|{m.name}|{m.release_date or ''}|{m.url}".lower()
    return "model:"+hashlib.sha256(raw.encode()).hexdigest()[:32]

def summarize_and_forward(models, *args, **kwargs):
    for m in models or []:
        try:
            if not monitor.is_stale_release(m.release_date) and m.url:
                emit_release_event({
                    "id":_event_id(m),"kind":"model",
                    "name":m.name.split("/")[-1],"version":m.release_date or "new",
                    "source":"modelbytes","source_type":m.source,"url":m.canonical_url or m.url,
                    "published_at":m.release_date,
                    "summary":m.description or "",
                    "metadata":{"provider":m.provider,"model_id":m.name},
                })
        except Exception as exc:
            print(f"[WARN] release-event model forward failed: {type(exc).__name__}")
    return _original(models,*args,**kwargs)

monitor.summarize_models=summarize_and_forward
raise SystemExit(monitor.main())

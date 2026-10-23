"""Ops alert for a publisher that dies before main() runs.

monitor.py's __main__ handler (_handle_crash) only sees exceptions raised
inside main(). A failure while monitor.py imports (a broken dependency, a
bad MODELBYTES_* number at module level) exited 1 with no ops alert, no
heartbeat /fail and no publish_runs row. This hook covers that window with
the same destinations and env vars as send_ops_alert: the admin Telegram DM,
then the Slack ops channel, then MODELBYTES_HEARTBEAT_URL/fail.

Standard library only: the failure may be requests or ss_publish itself.
"""
import json
import os
import socket
import sys
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BANNER = "🚨 ModelBytes ops:"
_SECRET_NAME_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "DATABASE", "_URL")
_state = {"handled": False, "preview": False, "previous": None}


def install_for(module_name: str, argv) -> None:
    """Install the hook when monitor.py runs as a script. An import is a no-op."""
    if module_name != "__main__" or _state["previous"] is not None:
        return
    _state["preview"] = "--preview" in (argv or [])
    _state["previous"] = sys.excepthook
    sys.excepthook = _hook


def mark_handled() -> None:
    """_handle_crash has reported this crash; the hook stays quiet."""
    _state["handled"] = True


def _hook(exc_type, exc, tb) -> None:
    try:
        (_state["previous"] or sys.__excepthook__)(exc_type, exc, tb)
    except Exception:
        pass
    if _state["handled"] or _state["preview"]:
        return
    if issubclass(exc_type, KeyboardInterrupt):
        return
    try:
        notify(format_alert(exc_type, exc, tb))
    except Exception:
        pass


def _redact(text: str, env) -> str:
    out = str(text)
    for name, value in env.items():
        if len(value or "") >= 8 and any(m in name.upper() for m in _SECRET_NAME_MARKERS):
            out = out.replace(value, "<redacted>")
    return out


def _site(tb) -> str:
    frames = traceback.extract_tb(tb) if tb is not None else []
    if not frames:
        return ""
    chosen = frames[-1]
    for frame in reversed(frames):
        if Path(frame.filename).name == "monitor.py":
            chosen = frame
            break
    return f"{Path(chosen.filename).name}:{chosen.lineno} in {chosen.name}"


def _where(env) -> str:
    parts = [f"host={socket.gethostname()}"]
    loc = "/".join(p for p in (
        env.get("RAILWAY_ENVIRONMENT_NAME") or env.get("RAILWAY_ENVIRONMENT") or "",
        env.get("RAILWAY_SERVICE_NAME") or "") if p)
    if loc:
        parts.append(f"railway={loc}")
    deploy = env.get("RAILWAY_DEPLOYMENT_ID") or ""
    if deploy:
        parts.append(f"deploy={deploy[:8]}")
    return " ".join(parts)


def format_alert(exc_type, exc, tb, env=None, argv=None, now=None) -> str:
    """Operator-facing body. No source lines and no env dump."""
    env = os.environ if env is None else env
    now = now or datetime.now(timezone.utc)
    err = f"{exc_type.__name__}: {exc}".strip()
    if len(err) > 400:
        err = err[:400] + "…"
    lines = [f"Publisher CRASHED at startup — {now:%Y-%m-%d %H:%M} UTC", err]
    site = _site(tb)
    if site:
        lines.append(f"at: {site}")
    run = " ".join(a if a.startswith("-") else Path(a).name
                   for a in (sys.argv if argv is None else argv)) or "monitor.py"
    lines.append(f"run: {run}")
    lines.append(f"where: {_where(env)}")
    lines.append("main() never ran: no digest was attempted and there is no "
                 "publish_runs row for this run.")
    return _redact("\n".join(lines), env)


def _post(url: str, data: bytes, headers: dict) -> bytes:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read()


def _post_json(url: str, payload: dict, headers: dict = None) -> bool:
    try:
        raw = _post(url, json.dumps(payload).encode("utf-8"),
                    {"Content-Type": "application/json", **(headers or {})})
        return bool(json.loads(raw or b"{}").get("ok"))
    except Exception:
        return False


def notify(text: str, env=None) -> bool:
    """Telegram admin DM, else Slack ops; then heartbeat /fail. Never raises.
    True when the Telegram or Slack message landed."""
    env = os.environ if env is None else env
    text = _redact(text, env)
    body = f"{BANNER} {text}"[:3900]
    sent = False
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat = env.get("MODELBYTES_ADMIN_CHAT_ID", "")
    if token and chat:
        sent = _post_json(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": chat, "text": body, "disable_web_page_preview": True})
    slack = env.get("SLACK_BOT_TOKEN", "")
    channel = env.get("MODELBYTES_OPS_SLACK_CHANNEL_ID", "")
    if not sent and slack and channel:
        sent = _post_json(
            "https://slack.com/api/chat.postMessage",
            {"channel": channel, "text": body, "unfurl_links": False},
            headers={"Authorization": f"Bearer {slack}"})
    heartbeat = env.get("MODELBYTES_HEARTBEAT_URL", "").rstrip("/")
    if heartbeat:
        try:
            _post(f"{heartbeat}/fail", text[:1000].encode("utf-8"), {})
        except Exception:
            pass
    return sent

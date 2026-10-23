"""Startup crash alert: a failure before main() must still page the operator.

monitor.py's __main__ handler only catches exceptions raised inside main().
An import or module-level failure exited 1 with no ops alert, no heartbeat
/fail and no publish_runs row. startup_alert covers that window.

The subprocess tests run monitor.py for real with a scrubbed environment
(placeholder token and chat id, example.com heartbeat) and a stubbed
urllib.request.urlopen, so nothing leaves the machine.
"""
import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import monitor
import startup_alert

TOKEN = "123456:placeholder-bot-token"
ENV = {
    "TELEGRAM_BOT_TOKEN": TOKEN,
    "MODELBYTES_ADMIN_CHAT_ID": "XXXXXXXXX",
    "SLACK_BOT_TOKEN": "xoxb-placeholder-slack-token",
    "MODELBYTES_OPS_SLACK_CHANNEL_ID": "C0XXXXXXXXX",
    "MODELBYTES_HEARTBEAT_URL": "https://hc.example.com/ping/placeholder/",
}


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(startup_alert, "_state",
                        {"handled": False, "preview": False, "previous": None})
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)


def _raise_in_monitor(code="int('not-a-number')"):
    source = f"def load():\n    {code}\nload()\n"
    try:
        exec(compile(source, "/app/monitor.py", "exec"), {})
    except BaseException as exc:  # noqa: BLE001 - the test wants any type
        return type(exc), exc, exc.__traceback__
    raise AssertionError("did not raise")


def _recorder(monkeypatch, responses=None):
    sent = []
    responses = dict(responses or {})

    def fake_post(url, data, headers):
        sent.append({"url": url, "data": data.decode("utf-8"), "headers": headers})
        for marker, reply in responses.items():
            if marker in url:
                if isinstance(reply, Exception):
                    raise reply
                return reply
        return b'{"ok": true}'

    monkeypatch.setattr(startup_alert, "_post", fake_post)
    return sent


def test_alert_names_the_error_the_site_and_that_main_never_ran():
    exc_type, exc, tb = _raise_in_monitor()
    now = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)
    body = startup_alert.format_alert(
        exc_type, exc, tb, env={"RAILWAY_DEPLOYMENT_ID": "abcdef123456"},
        argv=["/app/monitor.py"], now=now)
    assert body.startswith("Publisher CRASHED at startup — 2026-10-21 16:00 UTC")
    assert "ValueError: invalid literal for int()" in body
    assert "at: monitor.py:2 in load" in body
    assert "run: monitor.py" in body
    assert "deploy=abcdef12" in body
    assert "main() never ran" in body
    assert "publish_runs" in body


def test_alert_redacts_secret_env_values():
    exc_type, exc, tb = _raise_in_monitor(f"raise RuntimeError('bad {TOKEN}')")
    body = startup_alert.format_alert(exc_type, exc, tb, env=ENV)
    assert TOKEN not in body
    assert "<redacted>" in body


def test_notify_sends_telegram_then_heartbeat_fail(monkeypatch):
    sent = _recorder(monkeypatch)
    assert startup_alert.notify(f"boom {TOKEN}", env=ENV) is True
    assert [s["url"].split("/bot")[0] for s in sent] == [
        "https://api.telegram.org", "https://hc.example.com/ping/placeholder/fail"]
    payload = json.loads(sent[0]["data"])
    assert payload["chat_id"] == "XXXXXXXXX"
    assert payload["text"].startswith("🚨 ModelBytes ops: boom")
    assert TOKEN not in payload["text"]
    assert TOKEN not in sent[1]["data"]


def test_notify_falls_back_to_slack_when_telegram_fails(monkeypatch):
    sent = _recorder(monkeypatch, {"api.telegram.org": OSError("telegram down")})
    assert startup_alert.notify("boom", env=ENV) is True
    urls = [s["url"] for s in sent]
    assert urls[1] == "https://slack.com/api/chat.postMessage"
    assert sent[1]["headers"]["Authorization"] == "Bearer xoxb-placeholder-slack-token"
    assert json.loads(sent[1]["data"])["channel"] == "C0XXXXXXXXX"
    assert urls[2].endswith("/fail")


def test_notify_never_raises(monkeypatch):
    def broken(url, data, headers):
        raise RuntimeError("network gone")

    monkeypatch.setattr(startup_alert, "_post", broken)
    assert startup_alert.notify("boom", env=ENV) is False


def test_notify_without_destinations_sends_nothing(monkeypatch):
    sent = _recorder(monkeypatch)
    assert startup_alert.notify("boom", env={}) is False
    assert sent == []


def test_install_is_a_no_op_on_import():
    before = sys.excepthook
    startup_alert.install_for("monitor", ["monitor.py"])
    assert sys.excepthook is before


def test_hook_alerts_once_for_an_unhandled_startup_crash(monkeypatch):
    bodies = []
    monkeypatch.setattr(startup_alert, "notify", lambda body: bodies.append(body))
    monkeypatch.setattr(sys, "excepthook", lambda *a: None)
    startup_alert.install_for("__main__", ["monitor.py"])
    sys.excepthook(*_raise_in_monitor())
    assert len(bodies) == 1 and "ValueError" in bodies[0]


@pytest.mark.parametrize("argv,handled,exc", [
    (["monitor.py", "--preview"], False, None),
    (["monitor.py"], True, None),
    (["monitor.py"], False, KeyboardInterrupt),
])
def test_hook_stays_quiet(monkeypatch, argv, handled, exc):
    bodies = []
    monkeypatch.setattr(startup_alert, "notify", lambda body: bodies.append(body))
    monkeypatch.setattr(sys, "excepthook", lambda *a: None)
    startup_alert.install_for("__main__", argv)
    if handled:
        startup_alert.mark_handled()
    info = _raise_in_monitor() if exc is None else (exc, exc(), None)
    sys.excepthook(*info)
    assert bodies == []


def test_handle_crash_marks_the_crash_reported(monkeypatch):
    monkeypatch.setattr(monitor, "send_ops_alert", lambda text: True)
    monkeypatch.setattr(monitor, "ping_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "record_publish_run", lambda *a, **k: True)
    monitor._handle_crash(RuntimeError("boom"), preview=False, argv=["monitor.py"])
    assert startup_alert._state["handled"] is True


_DRIVER = textwrap.dedent("""
    import json, runpy, sys, urllib.request

    log_path, monitor_path = sys.argv[1], sys.argv[2]

    class _Reply:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "url": req.full_url,
                "data": (req.data or b"").decode("utf-8"),
            }) + "\\n")
        return _Reply(b'{"ok": true}')

    urllib.request.urlopen = fake_urlopen
    sys.argv = [monitor_path] + sys.argv[3:]
    runpy.run_path(monitor_path, run_name="__main__")
""")


def _run_monitor(tmp_path, extra_env=None, args=(), pythonpath=()):
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER)
    log = tmp_path / "sent.jsonl"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTHONPATH": os.pathsep.join([*map(str, pythonpath), str(ROOT)]),
        "TELEGRAM_BOT_TOKEN": TOKEN,
        "MODELBYTES_ADMIN_CHAT_ID": "XXXXXXXXX",
        "MODELBYTES_HEARTBEAT_URL": "https://hc.example.com/ping/placeholder",
    }
    env.update(extra_env or {})
    proc = subprocess.run(
        [sys.executable, str(driver), str(log), str(ROOT / "monitor.py"), *args],
        env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=120)
    sent = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return proc, sent


def test_module_level_crash_alerts_from_a_real_process(tmp_path):
    proc, sent = _run_monitor(tmp_path, {"MODELBYTES_HTTP_RETRIES": "not-a-number"})
    assert proc.returncode == 1
    assert "ValueError" in proc.stderr  # the traceback still reaches the log
    assert [s["url"].split("/bot")[0] for s in sent] == [
        "https://api.telegram.org", "https://hc.example.com/ping/placeholder/fail"]
    text = json.loads(sent[0]["data"])["text"]
    assert "CRASHED at startup" in text
    assert "ValueError" in text
    assert "at: monitor.py:" in text
    assert TOKEN not in text


def test_broken_dependency_alerts_from_a_real_process(tmp_path):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "requests.py").write_text("raise ImportError('requests is broken (test)')\n")
    proc, sent = _run_monitor(tmp_path, pythonpath=[broken])
    assert proc.returncode == 1
    text = json.loads(sent[0]["data"])["text"]
    assert "ImportError: requests is broken (test)" in text
    assert sent[-1]["url"].endswith("/fail")


def test_preview_startup_crash_does_not_alert(tmp_path):
    proc, sent = _run_monitor(
        tmp_path, {"MODELBYTES_HTTP_RETRIES": "not-a-number"}, args=["--preview"])
    assert proc.returncode == 1
    assert sent == []

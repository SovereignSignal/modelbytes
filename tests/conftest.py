"""Shared test guards.

The publisher now talks to the network (GitHub raw pending fetch) and can wait
minutes for a late curator (grace window). Tests must never sleep or hit the
real network: the grace window is zeroed and the raw fetch stubbed to "absent"
by default — tests that exercise those paths monkeypatch them explicitly.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


@pytest.fixture(autouse=True)
def _no_network_no_sleep(monkeypatch):
    monkeypatch.setattr(monitor, "PENDING_GRACE_SECONDS", 0)
    monkeypatch.setattr(monitor, "_fetch_pending_from_github",
                        lambda today, attempts=3: None)
    monkeypatch.setattr(monitor.time, "sleep", lambda s: None)
    # Blank every ops destination so a dev shell with prod env vars exported
    # can never send real alerts, heartbeats, or DB writes from the suite.
    monkeypatch.setattr(monitor, "ADMIN_CHAT_ID", "")
    monkeypatch.setattr(monitor, "OPS_SLACK_CHANNEL_ID", "")
    monkeypatch.setattr(monitor, "HEARTBEAT_URL", "")
    monkeypatch.setattr(monitor, "DATABASE_URL", "")
    monkeypatch.setattr(monitor, "ALLOW_SEED", False)

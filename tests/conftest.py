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
def _no_network_no_sleep(monkeypatch, tmp_path):
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
    # Release-event forwarding must stay inert in the suite even if a dev
    # shell exported the production endpoint. Tests that exercise it opt in.
    monkeypatch.delenv("MODELBYTES_RELEASE_FORWARDING", raising=False)
    monkeypatch.delenv("RELEASE_EVENTS_URL", raising=False)
    monkeypatch.delenv("RELEASE_EVENTS_TOKEN", raising=False)
    # Source-health state must not leak across tests or into the repo tree.
    monkeypatch.setattr(monitor, "SOURCE_HEALTH_PATH", tmp_path / "source_health.json")
    monkeypatch.setattr(monitor, "_SOURCE_ERRORS", {})
    monkeypatch.setattr(monitor, "_DISCOVERY_DISABLED", False)

    class _EmptyLabFeed:
        text = ""
        content = b""

        def raise_for_status(self):
            return None

    # Lab feeds and HF commit lookups are network. Tests that exercise them
    # monkeypatch these back. Empty feeds keep discover_recent_releases quiet.
    monkeypatch.setattr(
        monitor, "_lab_feed_get", lambda url, source_name: _EmptyLabFeed())
    monkeypatch.setattr(monitor, "_fetch_hf_commits", lambda model_id: [])

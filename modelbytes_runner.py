"""Start shim.

Production runs ``python monitor.py`` (railway.toml ``startCommand``) so
monitor's ``__main__`` block can ops-alert, ping the heartbeat ``/fail``,
and write a ``publish_runs`` crash row.

This module remains so an old ``python modelbytes_runner.py`` command does
the same thing. It must not wrap the digest writer or emit release events.
That old path forwarded during ``--preview``, before QA.
"""
from __future__ import annotations

import runpy

if __name__ == "__main__":
    runpy.run_module("monitor", run_name="__main__")

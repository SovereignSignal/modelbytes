"""Production cron must enter monitor.py as __main__.

startCommand is `python monitor.py` so the `__main__` block runs
_handle_crash (ops alert, heartbeat /fail, publish_runs crash row).
The cron string stays `0 16 * * *`.
"""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_railway_start_command_runs_monitor_py_with_unchanged_cron():
    cfg = tomllib.loads((ROOT / "railway.toml").read_text())
    deploy = cfg["deploy"]
    assert deploy["startCommand"] == "python monitor.py"
    assert deploy["cronSchedule"] == "0 16 * * *"

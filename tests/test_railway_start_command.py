"""Production cron must enter monitor.py as __main__.

modelbytes_runner.py monkeypatches summarize_models (forwards release events
before digest QA, including --preview) and calls monitor.main() directly,
which skips the `if __name__ == "__main__"` crash handler (_handle_crash:
ops alert, heartbeat /fail, publish_runs crash row).
"""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_railway_start_command_runs_monitor_py_with_unchanged_cron():
    cfg = tomllib.loads((ROOT / "railway.toml").read_text())
    deploy = cfg["deploy"]
    assert deploy["startCommand"] == "python monitor.py"
    assert deploy["cronSchedule"] == "0 16 * * *"

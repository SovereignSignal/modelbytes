"""@ArtificialAnlys watch on X. Off unless MODELBYTES_X_BEARER_TOKEN is set.

Posts are read with the official X API v2 user timeline. One page of 10
posts is $0.005 each ($0.05 when the page is full). No user lookup: the
id 1743487864934162432 was resolved on 2026-10-15.
"""
import ast
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


TODAY = "2026-10-15"
TOKEN = "x-bearer-test"
USER_ID = "1743487864934162432"


def _post(text, *, created="2026-10-14T22:37:30.000Z", tweet_id="100",
          expanded=None, retweet=False):
    row = {
        "id": tweet_id,
        "text": text,
        "created_at": created,
        "entities": {"urls": []},
    }
    if expanded:
        row["entities"]["urls"].append({
            "expanded_url": expanded,
            "unwound_url": expanded,
        })
    if retweet:
        row["referenced_tweets"] = [{"type": "retweeted", "id": "9"}]
        row["text"] = "RT @someone: " + text
    return row


def _resp(payload):
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.json.return_value = payload
    resp.status_code = 200
    return resp


def test_x_watch_skipped_when_token_unset(monkeypatch, capsys):
    monkeypatch.setattr(monitor, "X_BEARER_TOKEN", "")
    called = []
    monkeypatch.setattr(
        monitor, "_http_get",
        lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
            AssertionError("network")),
    )
    alerts = []
    monkeypatch.setattr(
        monitor, "send_ops_alert", lambda text: alerts.append(text) or True)

    assert monitor.fetch_artificial_analysis_x_models(seen=set(), today=TODAY) == []
    err = capsys.readouterr().err
    assert "MODELBYTES_X_BEARER_TOKEN" in err
    assert called == []
    assert alerts == []
    assert monitor._consume_source_error("X-ArtificialAnalysis") == ""


def test_x_watch_keeps_a_ranking_and_a_launch(monkeypatch):
    monkeypatch.setattr(monitor, "X_BEARER_TOKEN", TOKEN)
    calls = []

    def fake_get(url, source_name, timeout=30, **kwargs):
        calls.append((url, source_name, kwargs))
        return _resp({"data": [
            _post(
                "HeyGen Voice is now #1 on the Artificial Analysis voice arena",
                tweet_id="501",
                expanded="https://artificialanalysis.ai/text-to-speech/models/heygen-voice",
            ),
            _post(
                "Microsoft-Decision-1 is now available as an API-only model",
                tweet_id="502",
            ),
            _post(
                "GPT-6 Astra tops the leaderboard today\n\n"
                "ahead of GPT-6.1 Sol at 65 and Gemini 4 Argon at 55",
                tweet_id="503",
            ),
            _post(
                "We are launching AA-Robotics v0.1, our research preview",
                tweet_id="504",
            ),
            _post(
                "How the voice arena sample was recorded",
                tweet_id="505",
            ),
            _post(
                "HeyGen Voice is now #1",
                tweet_id="506",
                created="2026-10-01T00:00:00.000Z",
            ),
            _post("HeyGen Voice is now #1", tweet_id="507", retweet=True),
        ]})

    monkeypatch.setattr(monitor, "_http_get", fake_get)
    models = monitor.fetch_artificial_analysis_x_models(seen=set(), today=TODAY)
    assert [m.name for m in models] == [
        "HeyGen Voice",
        "Microsoft-Decision-1",
        "GPT-6 Astra",
    ]
    # Seven posts came back. A page that is not all launches is still live.
    assert monitor.LAST_X_POSTS == 7
    heygen, decision, gpt = models
    assert heygen.url == "https://artificialanalysis.ai/text-to-speech/models/heygen-voice"
    assert heygen.credit_url == "https://x.com/ArtificialAnlys/status/501"
    assert heygen.modality == "text-to-speech"
    assert heygen.source == "x-artificial-analysis"
    assert "x-release" in heygen.unique_traits
    assert heygen.release_date == "2026-10-14"
    assert monitor.categorize_model(heygen) == "specialized"
    assert monitor.is_significant_release(
        heygen.name, "", heygen.unique_traits, downloads=0)
    assert decision.url == "https://x.com/ArtificialAnlys/status/502"
    assert decision.is_open_source is False
    assert monitor.categorize_model(decision) == "closed_frontier"
    assert gpt.name == "GPT-6 Astra"

    url, source, kwargs = calls[0]
    assert url == f"https://api.x.com/2/users/{USER_ID}/tweets"
    assert source == "X-ArtificialAnalysis"
    assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in url
    assert kwargs["params"]["max_results"] == 10
    assert "retweets" in kwargs["params"]["exclude"]
    assert "replies" not in kwargs["params"]["exclude"]
    assert "expansions" not in kwargs["params"]
    assert kwargs["params"]["start_time"].startswith("2026-10-12")


def test_x_watch_failure_returns_empty_and_redacts_the_token(monkeypatch):
    monkeypatch.setattr(monitor, "X_BEARER_TOKEN", TOKEN)

    def boom(url, source_name, timeout=30, **kwargs):
        raise RuntimeError("boom " + TOKEN)

    monkeypatch.setattr(monitor, "_http_get", boom)
    assert monitor.fetch_artificial_analysis_x_models(seen=set(), today=TODAY) == []
    reason = monitor._consume_source_error("X-ArtificialAnalysis")
    assert "boom" in reason
    assert TOKEN not in reason


def test_main_skips_x_watch_without_a_token(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["monitor.py", "--preview"])
    monkeypatch.setattr(monitor, "try_post_pending_curated", lambda: False)
    monkeypatch.setattr(monitor, "init_database", lambda: None)
    monkeypatch.setattr(monitor, "load_seen_models", lambda: {"seed/already"})
    monkeypatch.setattr(monitor, "save_seen_models", lambda _s: None)
    monkeypatch.setattr(monitor, "X_BEARER_TOKEN", "")
    monkeypatch.setattr(monitor, "DISCOVERY_ENABLED", False)
    monkeypatch.setattr(monitor, "PARALLEL_API_KEY", "")
    for name in (
        "fetch_openrouter_models",
        "fetch_ollama_models",
        "fetch_huggingface_trending",
        "fetch_major_orgs",
        "fetch_hf_text_generation",
        "fetch_testingcatalog_models",
        "fetch_artificial_analysis_models",
    ):
        monkeypatch.setattr(monitor, name, lambda *args, **kwargs: [])
    alerts = []
    monkeypatch.setattr(
        monitor, "send_ops_alert", lambda text: alerts.append(text) or True)

    assert monitor.main() == 0
    err = capsys.readouterr().err
    assert (
        "source_health source=X-ArtificialAnalysis status=ok items=0 error=disabled"
        in err
    )
    assert err.count("MODELBYTES_X_BEARER_TOKEN") == 1
    assert alerts == []


def test_main_references_the_x_fetcher():
    tree = ast.parse(Path(monitor.__file__).read_text())
    # main() is a thin wrapper; the pipeline body lives in _run_digest.
    fns = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in ("main", "_run_digest")
    ]
    assert len(fns) == 2
    referenced = {
        node.id for fn in fns for node in ast.walk(fn)
        if isinstance(node, ast.Name)
    }
    assert "fetch_artificial_analysis_x_models" in referenced
    assert "run_alphasignal_crosscheck" in referenced

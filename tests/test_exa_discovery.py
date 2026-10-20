"""Exa discovery trial — LOG ONLY, flag-gated (2-week trial). Mocked HTTP only."""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


def _resp(payload):
    r = MagicMock(); r.raise_for_status = lambda: None
    r.json.return_value = payload
    return r


def _cand(name="acme/x-2", url="https://example.com/x2"):
    return monitor.ModelRelease(name=name, provider="Acme", source="test",
                                url=url, description="d")


@pytest.fixture
def _exa_on(monkeypatch):
    monkeypatch.setattr(monitor, "EXA_API_KEY", "placeholder")
    monkeypatch.setattr(monitor, "EXA_DISCOVERY_ENABLED", True)


def _no_http(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network call")
    monkeypatch.setattr(monitor.requests, "post", boom)


def test_off_by_default(monkeypatch, capsys):
    monkeypatch.setattr(monitor, "EXA_API_KEY", "placeholder")
    monkeypatch.setattr(monitor, "EXA_DISCOVERY_ENABLED", False)
    _no_http(monkeypatch)
    assert monitor.exa_discovery_trial([_cand()]) == []
    assert capsys.readouterr().err == ""


def test_off_without_key(monkeypatch, capsys):
    monkeypatch.setattr(monitor, "EXA_API_KEY", "")
    monkeypatch.setattr(monitor, "EXA_DISCOVERY_ENABLED", True)
    _no_http(monkeypatch)
    assert monitor.exa_discovery_trial([_cand()]) == []
    assert capsys.readouterr().err == ""


def test_request_shape(monkeypatch, _exa_on):
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers, timeout))
        return _resp({"results": []})
    monkeypatch.setattr(monitor.requests, "post", post)
    monitor.exa_discovery_trial([_cand()])
    assert 3 <= len(calls) <= 5
    for url, body, headers, timeout in calls:
        assert url == "https://api.exa.ai/search"
        assert headers["x-api-key"] == "placeholder"
        assert headers["Content-Type"] == "application/json"
        assert headers["Exa-Beta"] == "dynamic-highlights-2026-08-28"
        assert body["type"] == "auto" and body["includeDomains"]
        assert body["startPublishedDate"]
        assert body["contents"]["highlights"] == {"dynamic": True,
                                                  "verbosity": "low"}
        assert timeout


def test_flags_new_vs_known(monkeypatch, _exa_on, capsys):
    payload = {"results": [
        {"title": "Known by url", "url": "https://www.example.com/x2/?a=1",
         "publishedDate": "2026-10-18", "highlights": ["h1"]},
        {"title": "Acme X-2 launches", "url": "https://example.com/other",
         "highlights": ["h2"]},
        {"title": "Brand New Model", "url": "https://example.com/new",
         "highlights": ["fresh highlight"]},
    ]}
    monkeypatch.setattr(monitor.requests, "post",
                        lambda *a, **k: _resp(payload))
    found = monitor.exa_discovery_trial([_cand()])
    by_title = {f["title"]: f["known"] for f in found}
    assert by_title["Known by url"] is True
    assert by_title["Acme X-2 launches"] is True
    assert by_title["Brand New Model"] is False
    err = capsys.readouterr().err
    assert "NEW" in err and "fresh highlight" in err


def test_errors_swallowed_no_alert(monkeypatch, _exa_on, capsys):
    alert = MagicMock()
    monkeypatch.setattr(monitor, "send_ops_alert", alert)
    monkeypatch.setattr(monitor.requests, "post",
                        MagicMock(side_effect=TimeoutError("slow")))
    assert monitor.exa_discovery_trial([_cand()]) == []
    assert "Exa discovery query failed" in capsys.readouterr().err

    bad = MagicMock()
    bad.raise_for_status.side_effect = RuntimeError("HTTP 500")
    monkeypatch.setattr(monitor.requests, "post", lambda *a, **k: bad)
    assert monitor.exa_discovery_trial([_cand()]) == []
    alert.assert_not_called()


def test_does_not_mutate_candidates(monkeypatch, _exa_on):
    monkeypatch.setattr(monitor.requests, "post", lambda *a, **k: _resp(
        {"results": [{"title": "Brand New", "url": "https://example.com/n",
                      "highlights": ["h"]}]}))
    cands = [_cand()]
    before = list(cands)
    monitor.exa_discovery_trial(cands)
    assert cands == before and len(cands) == 1

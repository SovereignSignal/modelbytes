"""Tests for source HTTP retry behavior."""
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


class FakeResponse:
    def __init__(self, status_code: int, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


def test_http_get_retries_retryable_status(monkeypatch):
    responses = [FakeResponse(503), FakeResponse(200, {"ok": True})]
    calls = []
    sleeps = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(monitor.requests, "get", fake_get)
    monkeypatch.setattr(monitor.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(monitor, "HTTP_RETRIES", 2)

    resp = monitor._http_get("https://example.test/models", "Example")

    assert resp.json() == {"ok": True}
    assert len(calls) == 2
    assert sleeps == [monitor.HTTP_BACKOFF_SECONDS]
    assert calls[0][1]["headers"]["User-Agent"].startswith("ModelBytes/")


def test_http_get_does_not_retry_non_retryable_status(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(404)

    monkeypatch.setattr(monitor.requests, "get", fake_get)
    monkeypatch.setattr(monitor, "HTTP_RETRIES", 3)

    with pytest.raises(requests.HTTPError):
        monitor._http_get("https://example.test/missing", "Example")

    assert len(calls) == 1

"""Tests for the Gemini web-grounding fetcher (mocked HTTP — no network)."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from strategy_app.brain.oversight import gemini_web


def _fake_resp(payload: dict):
    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(payload).encode()
    return _R()


def test_no_key_returns_empty():
    assert gemini_web.fetch_web_context(api_key="") == ""


def test_parses_grounded_text(monkeypatch):
    payload = {"candidates": [{"content": {"parts": [{"text": "RBI repo 6.0%; FII -1200cr; calm."}]}}]}
    monkeypatch.setattr(gemini_web.urllib.request, "urlopen", lambda req, timeout=0: _fake_resp(payload))
    out = gemini_web.fetch_web_context(api_key="k")
    assert "RBI repo" in out and "FII" in out


def test_http_error_returns_empty(monkeypatch):
    def _raise(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url if hasattr(req, "full_url") else "u", 429, "Too Many Requests", {}, io.BytesIO(b"quota"))
    monkeypatch.setattr(gemini_web.urllib.request, "urlopen", _raise)
    assert gemini_web.fetch_web_context(api_key="k") == ""


def test_malformed_response_returns_empty(monkeypatch):
    monkeypatch.setattr(gemini_web.urllib.request, "urlopen", lambda req, timeout=0: _fake_resp({"nope": 1}))
    assert gemini_web.fetch_web_context(api_key="k") == ""

"""LLM-резюме: выключено по умолчанию; при отказе модели — None; текст берётся только из text-блоков."""

from __future__ import annotations

from types import SimpleNamespace

from quant.analysis.llm import MODEL, summarize


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def fake_client(response):
    msgs = FakeMessages(response)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def test_disabled_by_default(tmp_settings):
    client, msgs = fake_client(None)
    assert summarize({"a": 1}, client=client) is None
    assert msgs.kwargs is None


def test_enabled_uses_facts_and_fallback(tmp_settings, monkeypatch):
    from quant import config

    monkeypatch.setenv("QUANT_LLM_SUMMARY_ENABLED", "1")
    config.get_settings.cache_clear()
    resp = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="thinking", thinking=""),
                                                          SimpleNamespace(type="text", text=" Лонг поддержан трендом. ")])
    client, msgs = fake_client(resp)
    assert summarize({"symbol": "BTCUSDT"}, client=client) == "Лонг поддержан трендом."
    assert msgs.kwargs["model"] == MODEL
    assert msgs.kwargs["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in msgs.kwargs["betas"]
    assert "BTCUSDT" in msgs.kwargs["messages"][0]["content"]
    refused = SimpleNamespace(stop_reason="refusal", content=[])
    client2, _ = fake_client(refused)
    assert summarize({"symbol": "BTCUSDT"}, client=client2) is None

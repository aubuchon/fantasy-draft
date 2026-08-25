from __future__ import annotations

from fantasy_draft.settings import AppSettings


def test_live_openai_defaults_fit_thirty_second_decision_budget(monkeypatch):
    for name in (
        "OPENAI_MODEL",
        "OPENAI_LIVE_TIMEOUT_SECONDS",
        "OPENAI_TIMEOUT_SECONDS",
        "OPENAI_DIAGNOSTIC_TIMEOUT_SECONDS",
        "OPENAI_REASONING_EFFORT",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = AppSettings.from_environment()
    assert settings.openai_model == "gpt-5.6-terra"
    assert settings.openai_live_timeout_seconds == 25
    assert settings.openai_diagnostic_timeout_seconds == 30
    assert settings.openai_reasoning_effort == "low"


def test_openai_live_and_diagnostic_timeouts_are_independent(monkeypatch):
    monkeypatch.setenv("OPENAI_LIVE_TIMEOUT_SECONDS", "6")
    monkeypatch.setenv("OPENAI_DIAGNOSTIC_TIMEOUT_SECONDS", "31")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "low")
    settings = AppSettings.from_environment()
    assert settings.openai_live_timeout_seconds == 6
    assert settings.openai_diagnostic_timeout_seconds == 31
    assert settings.openai_reasoning_effort == "low"


def test_legacy_timeout_only_applies_to_live_path(monkeypatch):
    monkeypatch.delenv("OPENAI_LIVE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("OPENAI_DIAGNOSTIC_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "9")
    settings = AppSettings.from_environment()
    assert settings.openai_live_timeout_seconds == 9
    assert settings.openai_diagnostic_timeout_seconds == 30

"""Settings tab backend: /api/settings and /api/env-keys.

Calls the endpoint functions directly (FastAPI's route decorators return the
plain function, so no ASGI/TestClient/lifespan machinery is needed) against
config paths monkeypatched into tmp_path, so these tests never touch the
real ~/.applypilot/settings.json or .env.
"""

import pytest
from fastapi import HTTPException

from applypilot import config
from applypilot.web import server


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    return tmp_path


def test_get_settings_returns_defaults_when_unconfigured(isolated_config):
    s = server.api_get_settings()
    assert s["apply_backend"] == "goose"
    assert s["max_daily_spend_usd"] is None


def test_update_settings_persists_and_returns_merged(isolated_config):
    server.api_update_settings({"max_daily_spend_usd": 25.0, "max_apply_attempts": 5})
    s = server.api_get_settings()
    assert s["max_daily_spend_usd"] == 25.0
    assert s["max_apply_attempts"] == 5
    # untouched keys keep their default
    assert s["apply_backend"] == "goose"


def test_update_settings_merges_nested_dicts_instead_of_replacing(isolated_config):
    """cost_defaults has two backends; changing one must not drop the other."""
    server.api_update_settings({"cost_defaults": {"goose": 0.10}})
    s = server.api_get_settings()
    assert s["cost_defaults"]["goose"] == 0.10
    assert s["cost_defaults"]["claude"] == pytest.approx(1.20)


def test_env_keys_report_unset_when_no_env_file(isolated_config):
    status = server.api_get_env_keys()
    assert status == {"GEMINI_API_KEY": False, "OPENAI_API_KEY": False,
                       "OPENROUTER_API_KEY": False, "LLM_URL": False,
                       "APPLYPILOT_JOB_PASSWORD": False}


def test_set_env_keys_writes_and_reports_set(isolated_config):
    server.api_set_env_keys({"OPENROUTER_API_KEY": "sk-test-123"})
    status = server.api_get_env_keys()
    assert status["OPENROUTER_API_KEY"] is True
    assert status["GEMINI_API_KEY"] is False
    # the raw value is never echoed back by either endpoint
    assert "sk-test-123" not in str(status)


def test_set_env_keys_blank_value_leaves_existing_key_alone(isolated_config):
    server.api_set_env_keys({"OPENROUTER_API_KEY": "sk-real"})
    result = server.api_set_env_keys({"OPENROUTER_API_KEY": "", "GEMINI_API_KEY": "  "})
    assert result["updated"] == []
    assert server.api_get_env_keys()["OPENROUTER_API_KEY"] is True


def test_set_env_keys_rejects_unknown_key(isolated_config):
    with pytest.raises(HTTPException) as exc:
        server.api_set_env_keys({"SOME_RANDOM_SECRET": "x"})
    assert exc.value.status_code == 400

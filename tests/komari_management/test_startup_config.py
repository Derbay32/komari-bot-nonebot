"""Management startup configuration uses only namespaced dotenv settings."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import nonebot
import nonebot.plugin
import pytest
from nonebot.config import Config
from pydantic import ValidationError
from sqlmodel import SQLModel

from komari_bot.config.typed_config import ensure_typed_config_model
from komari_bot.plugins.komari_management.config_schema import ManagementConfig

if TYPE_CHECKING:
    from pathlib import Path

TOKEN = "test-only-startup-token-0123456789"
CREDENTIALS = [{"credential_id": "dashboard", "token": TOKEN, "permissions": ["*"]}]


def test_nonebot_loads_dotenv_and_environment_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "KOMARI_MANAGEMENT_PLUGIN_ENABLE=true\n"
        f"KOMARI_MANAGEMENT_API_CREDENTIALS='{json.dumps(CREDENTIALS)}'\n"
        'KOMARI_MANAGEMENT_API_ALLOWED_ORIGINS=["https://ui.example.com"]\n'
        "KOMARI_MANAGEMENT_ANNOUNCE_STATUS_PAGE_URL=https://status.example.com\n"
        "KOMARI_MANAGEMENT_ANNOUNCE_MAX_GROUP_COUNT=7\n"
        "KOMARI_MANAGEMENT_ANNOUNCE_SEND_INTERVAL_SECONDS=2.5\n"
        "KOMARI_MANAGEMENT_ANNOUNCE_REQUEST_COOLDOWN_SECONDS=45\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KOMARI_MANAGEMENT_ANNOUNCE_MAX_GROUP_COUNT", "9")
    driver = SimpleNamespace(config=Config(_env_file=dotenv))
    monkeypatch.setattr(nonebot.plugin, "get_driver", lambda: driver)

    config = nonebot.get_plugin_config(ManagementConfig)

    assert config.komari_management_plugin_enable is True
    assert config.komari_management_api_credentials[0].token == TOKEN
    assert config.komari_management_api_allowed_origins == ["https://ui.example.com"]
    assert (
        config.komari_management_announce_status_page_url
        == "https://status.example.com"
    )
    assert config.komari_management_announce_max_group_count == 9
    assert config.komari_management_announce_send_interval_seconds == 2.5
    assert config.komari_management_announce_request_cooldown_seconds == 45
    assert TOKEN not in repr(config)
    assert TOKEN not in repr(config.komari_management_api_credentials[0])


def test_management_is_not_a_typed_database_resource() -> None:
    assert ensure_typed_config_model("komari_management") is None
    assert "komari_management_config" not in SQLModel.metadata.tables
    assert not {"id", "revision", "updated_at"} & ManagementConfig.model_fields.keys()


def test_unprefixed_settings_do_not_enable_management(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ManagementConfig.model_validate(
        {
            "plugin_enable": True,
            "api_credentials": CREDENTIALS,
            "api_allowed_origins": ["https://wrong.example.com"],
        }
    )
    assert config.komari_management_plugin_enable is False
    assert config.komari_management_api_credentials == []
    assert config.komari_management_api_allowed_origins == []

    monkeypatch.setenv("PLUGIN_ENABLE", "true")
    monkeypatch.setenv("API_CREDENTIALS", json.dumps(CREDENTIALS))
    driver = SimpleNamespace(config=Config(_env_file=None))
    monkeypatch.setattr(nonebot.plugin, "get_driver", lambda: driver)
    startup = nonebot.get_plugin_config(ManagementConfig)
    assert startup.komari_management_plugin_enable is False
    assert startup.komari_management_api_credentials == []


@pytest.mark.parametrize("field", ["api_credentials", "api_allowed_origins"])
@pytest.mark.parametrize(
    "value", ['{"wrong": []}', '"not-an-array"', "not-json", "null"]
)
def test_startup_arrays_reject_non_arrays(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        ManagementConfig.model_validate({f"komari_management_{field}": value})


@pytest.mark.parametrize(
    "token", ["tiny123", "aaaaaaaaaaaaaaaa", "contains whitespace 12345"]
)
def test_startup_credentials_reject_weak_tokens_without_echoing(token: str) -> None:
    with pytest.raises(ValidationError) as error:
        ManagementConfig.model_validate(
            {
                "komari_management_api_credentials": [
                    {"credential_id": "dashboard", "token": token, "permissions": ["*"]}
                ]
            }
        )
    assert token not in str(error.value)


def test_invalid_dotenv_credentials_fail_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "KOMARI_MANAGEMENT_PLUGIN_ENABLE=true\n"
        'KOMARI_MANAGEMENT_API_CREDENTIALS=[{"credential_id":"dashboard","token":"short","permissions":["*"]}]\n',
        encoding="utf-8",
    )
    driver = SimpleNamespace(config=Config(_env_file=dotenv))
    monkeypatch.setattr(nonebot.plugin, "get_driver", lambda: driver)
    with pytest.raises(ValidationError):
        nonebot.get_plugin_config(ManagementConfig)

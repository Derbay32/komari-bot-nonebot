"""配置字段原子变换测试。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast

import pytest
from pydantic import BaseModel, Field

from komari_bot.plugins.config_manager import manager as manager_module
from komari_bot.plugins.config_manager.manager import ConfigManager
from komari_bot.plugins.config_manager.storage import StoredConfig


class _ListConfig(BaseModel):
    version: str = "1.0"
    last_updated: str = "2026-07-16T00:00:00+08:00"
    sr_list: list[str] = Field(default_factory=list)


def _stored(items: list[str], revision: int) -> StoredConfig:
    return StoredConfig(
        plugin_name="sr-mutation-test",
        schema_name="_ListConfig",
        config_data={
            "version": "1.0",
            "last_updated": "2026-07-16T00:00:00+08:00",
            "sr_list": items,
        },
        version="1.0",
        revision=revision,
        updated_at=datetime.now().astimezone() + timedelta(seconds=revision),
    )


class _ConflictOnceStorage:
    def __init__(self) -> None:
        self.fetch_results = [_stored(["甲"], 1), _stored(["甲", "乙"], 2)]
        self.update_patches: list[dict[str, Any]] = []

    async def fetch_async(self, _plugin_name: str) -> StoredConfig:
        return self.fetch_results.pop(0)

    async def update_fields_if_revision_async(
        self,
        **kwargs: Any,
    ) -> StoredConfig | None:
        patch = dict(kwargs["config_patch"])
        self.update_patches.append(patch)
        if len(self.update_patches) == 1:
            return None
        return _stored(list(patch["sr_list"]), 3)


@pytest.mark.asyncio
async def test_mutate_field_reapplies_transform_to_latest_value_after_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _ConflictOnceStorage()
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    manager = ConfigManager("sr-mutation-test", _ListConfig)
    observed_values: list[list[str]] = []

    def _append_item(current_value: Any) -> list[str]:
        items = [str(item) for item in current_value]
        observed_values.append(items)
        return [*items, "丙"]

    result = await manager.mutate_field_async("sr_list", _append_item)

    assert observed_values == [["甲"], ["甲", "乙"]]
    assert storage.update_patches[0]["sr_list"] == ["甲", "丙"]
    assert storage.update_patches[1]["sr_list"] == ["甲", "乙", "丙"]
    assert cast("_ListConfig", result).sr_list == ["甲", "乙", "丙"]


@pytest.mark.asyncio
async def test_mutate_field_skips_revision_write_when_value_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoWriteStorage:
        async def fetch_async(self, _plugin_name: str) -> StoredConfig:
            return _stored(["甲"], 1)

        async def update_fields_if_revision_async(self, **_kwargs: Any) -> None:
            raise AssertionError("无变化的字段不应写入新修订")

    monkeypatch.setattr(
        manager_module,
        "get_config_storage",
        lambda: _NoWriteStorage(),
    )
    manager = ConfigManager("sr-mutation-test", _ListConfig)

    result = await manager.mutate_field_async("sr_list", lambda value: value)

    assert cast("_ListConfig", result).sr_list == ["甲"]

"""PostgreSQL 场景模板加载器的可观察损坏检测测试。"""

from __future__ import annotations

from typing import Any, cast

import pytest

from komari_bot.plugins.komari_decision.services.scene_template_loader import (
    PostgresSceneTemplateLoader,
)

_REQUIRED_FIXED_SCENE_KEYS = (
    "NOISE",
    "MEANINGFUL",
    "CALL_DIRECT",
    "CALL_MENTION",
)


def _valid_rows() -> list[dict[str, Any]]:
    rows = [
        {
            "id": index,
            "scene_key": scene_key,
            "scene_type": "fixed",
            "content_text": f"{scene_key} 内容",
            "enabled": True,
            "order_index": index,
            "content_hash": f"hash-{scene_key}",
        }
        for index, scene_key in enumerate(_REQUIRED_FIXED_SCENE_KEYS, start=1)
    ]
    rows.append(
        {
            "id": 5,
            "scene_key": "GREETING",
            "scene_type": "general",
            "content_text": "问候场景",
            "enabled": True,
            "order_index": 5,
            "content_hash": "hash-GREETING",
        }
    )
    return rows


class FakeSceneRepository:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def list_scenes(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        assert enabled_only is True
        return [dict(row) for row in self.rows]

    @staticmethod
    def compute_scene_source_hash(rows: list[dict[str, Any]]) -> str:
        del rows
        return "source-hash"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_key", _REQUIRED_FIXED_SCENE_KEYS)
async def test_loader_rejects_each_missing_required_fixed_scene(
    missing_key: str,
) -> None:
    rows = [row for row in _valid_rows() if row["scene_key"] != missing_key]
    loader = PostgresSceneTemplateLoader(
        cast("Any", FakeSceneRepository(rows)),
    )

    with pytest.raises(ValueError) as exc_info:
        await loader.load_scene_template()

    message = str(exc_info.value)
    assert missing_key in message
    assert "fixed" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_key", _REQUIRED_FIXED_SCENE_KEYS)
async def test_loader_rejects_required_scene_declared_as_general(
    invalid_key: str,
) -> None:
    rows = _valid_rows()
    for row in rows:
        if row["scene_key"] == invalid_key:
            row["scene_type"] = "general"
            break
    loader = PostgresSceneTemplateLoader(
        cast("Any", FakeSceneRepository(rows)),
    )

    with pytest.raises(ValueError) as exc_info:
        await loader.load_scene_template()

    message = str(exc_info.value)
    assert invalid_key in message
    assert "fixed" in message


@pytest.mark.asyncio
async def test_loader_returns_valid_fixed_and_general_scenes() -> None:
    loader = PostgresSceneTemplateLoader(
        cast("Any", FakeSceneRepository(_valid_rows())),
    )

    payload = await loader.load_scene_template()

    assert set(payload.fixed_candidates) == set(_REQUIRED_FIXED_SCENE_KEYS)
    assert payload.general_scenes == [{"id": "GREETING", "text": "问候场景"}]

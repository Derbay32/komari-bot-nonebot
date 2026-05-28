"""单表拆表迁移脚本测试。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts/split_komari_memory_entity_tables.py"


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "tests.scripts.split_komari_memory_entity_tables_script",
        SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError

    module = importlib.util.module_from_spec(spec)
    original_module = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        if original_module is not None:
            sys.modules[spec.name] = original_module
        else:
            sys.modules.pop(spec.name, None)


def test_build_profile_payload_supports_columnized_profile_row() -> None:
    module = _load_script_module()
    row = module.LegacyProfileRow(
        user_id="10001",
        group_id="821560570",
        value=None,
        profile_version=1,
        profile_display_name="用户10001",
        profile_traits={"喜欢的食物": {"value": "布丁"}},
        profile_updated_at="2026-04-10T12:00:00+00:00",
        importance=4,
        access_count=2,
        last_accessed=None,
    )

    payload = module._build_profile_payload(row)

    assert payload == {
        "version": 1,
        "user_id": "10001",
        "display_name": "用户10001",
        "traits": {"喜欢的食物": {"value": "布丁"}},
        "updated_at": "2026-04-10T12:00:00+00:00",
    }


def test_build_interaction_payload_uses_fallback_display_name() -> None:
    module = _load_script_module()
    row = module.LegacyInteractionRow(
        user_id="10001",
        group_id="821560570",
        value=json.dumps(
            {
                "summary": "最近常聊天",
                "records": [{"event": "投喂布丁", "result": "吃掉了", "emotion": "开心"}],
            },
            ensure_ascii=False,
        ),
        importance=5,
        access_count=3,
        last_accessed=None,
    )

    payload = module._build_interaction_payload(
        row,
        fallback_display_name="用户10001",
    )

    assert payload is not None
    assert payload["display_name"] == "用户10001"
    assert payload["summary"] == "最近常聊天"
    assert payload["records"][0]["event"] == "投喂布丁"

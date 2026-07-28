"""用户画像瘦身脚本测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts/compact_komari_memory_profiles.py"


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "tests.scripts.compact_komari_memory_profiles_script",
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


def _make_profile_row(
    module: Any,
    *,
    user_id: str,
    trait_count: int,
) -> Any:
    return module.ProfileRow(
        user_id=user_id,
        group_id="821560570",
        value=json.dumps(
            {
                "version": 1,
                "user_id": user_id,
                "display_name": f"用户{user_id}",
                "traits": {
                    f"特征{i:02d}": {
                        "value": f"长期描述{i}",
                        "category": "general",
                        "importance": 4,
                        "updated_at": f"2026-03-21T00:00:{i % 60:02d}+08:00",
                    }
                    for i in range(trait_count)
                },
            },
            ensure_ascii=False,
        ),
        importance=4,
    )


def _make_memory_config() -> Any:
    return SimpleNamespace(
        llm_model_summary="summary-model",
        llm_temperature_summary=0.3,
        llm_max_tokens_summary=2048,
        summary_chunk_token_limit=3000,
        profile_trait_limit=20,
    )


def test_process_profile_rows_dry_run_only_reports_changes() -> None:
    module = _load_script_module()
    updated_rows: list[dict[str, Any]] = []

    async def _fake_generate_text(**kwargs: Any) -> str:
        del kwargs
        return json.dumps(
            {
                "user_id": "10001",
                "display_name": "用户10001",
                "traits": [
                    {
                        "key": f"压缩特征{i}",
                        "value": f"稳定信息{i}",
                        "category": "general",
                        "importance": 4,
                    }
                    for i in range(20)
                ],
            },
            ensure_ascii=False,
        )

    async def _fake_update_profile(**kwargs: Any) -> None:
        updated_rows.append(dict(kwargs))

    stats = asyncio.run(
        module._process_profile_rows(
            [
                _make_profile_row(module, user_id="10001", trait_count=25),
                _make_profile_row(module, user_id="10002", trait_count=5),
            ],
            memory_config=_make_memory_config(),
            llm_generate_text=_fake_generate_text,
            apply=False,
            update_profile=_fake_update_profile,
        )
    )

    assert stats == {
        "scanned": 2,
        "updated": 0,
        "would_update": 1,
        "skipped": 1,
        "failed": 0,
    }
    assert updated_rows == []


def test_process_profile_rows_apply_updates_database_callback() -> None:
    module = _load_script_module()
    updated_rows: list[dict[str, Any]] = []

    async def _fake_generate_text(**kwargs: Any) -> str:
        del kwargs
        return json.dumps(
            {
                "user_id": "10001",
                "display_name": "用户10001",
                "traits": [
                    {
                        "key": f"压缩特征{i}",
                        "value": f"稳定信息{i}",
                        "category": "general",
                        "importance": 4,
                    }
                    for i in range(18)
                ],
            },
            ensure_ascii=False,
        )

    async def _fake_update_profile(**kwargs: Any) -> None:
        updated_rows.append(dict(kwargs))

    stats = asyncio.run(
        module._process_profile_rows(
            [_make_profile_row(module, user_id="10001", trait_count=25)],
            memory_config=_make_memory_config(),
            llm_generate_text=_fake_generate_text,
            apply=True,
            update_profile=_fake_update_profile,
        )
    )

    assert stats == {
        "scanned": 1,
        "updated": 1,
        "would_update": 0,
        "skipped": 0,
        "failed": 0,
    }
    assert len(updated_rows) == 1

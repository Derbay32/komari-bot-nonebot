"""用户画像按绑定表合并脚本测试。"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts/merge_user_profiles_by_binding.py"


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "tests.scripts.merge_user_profiles_by_binding_script",
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


def test_build_merge_plans_merges_same_display_name_into_binding_uid() -> None:
    module = _load_script_module()
    rows = [
        module.ProfileRow(
            user_id="104719527",
            group_id="821560570",
            version=1,
            display_name="Derbay32",
            traits={
                "身份": {
                    "value": "开发者",
                    "category": "fact",
                    "importance": 4,
                    "updated_at": "2026-04-10T00:00:00+00:00",
                }
            },
            updated_at=datetime(2026, 4, 10, 0, 0, tzinfo=UTC),
            importance=4,
            access_count=1,
            last_accessed=datetime(2026, 4, 10, 0, 0),
        ),
        module.ProfileRow(
            user_id="1047195267",
            group_id="821560570",
            version=1,
            display_name="Derbay32",
            traits={
                "身份": {
                    "value": "最靠谱的开发者",
                    "category": "fact",
                    "importance": 5,
                    "updated_at": "2026-04-11T00:00:00+00:00",
                },
                "关系": {
                    "value": "Master",
                    "category": "relation",
                    "importance": 5,
                    "updated_at": "2026-04-11T00:00:00+00:00",
                },
            },
            updated_at=datetime(2026, 4, 11, 0, 0, tzinfo=UTC),
            importance=4,
            access_count=2,
            last_accessed=datetime(2026, 4, 11, 0, 0),
        ),
    ]

    plans = module._build_merge_plans(
        rows,
        bindings={"Derbay32": "1047195267"},
    )

    assert len(plans) == 1
    plan = plans[0]
    assert plan.target_uid == "1047195267"
    assert plan.delete_user_ids == ("104719527",)
    assert plan.merged_payload["traits"]["身份"]["value"] == "最靠谱的开发者"
    assert plan.merged_payload["traits"]["关系"]["value"] == "Master"
    assert plan.merged_access_count == 3


def test_build_merge_plans_skips_unbound_display_name() -> None:
    module = _load_script_module()
    rows = [
        module.ProfileRow(
            user_id="198291314",
            group_id="821560570",
            version=1,
            display_name="Yanami",
            traits={},
            updated_at=datetime(2026, 4, 11, 0, 0, tzinfo=UTC),
            importance=4,
            access_count=1,
            last_accessed=None,
        ),
        module.ProfileRow(
            user_id="1982919314",
            group_id="821560570",
            version=1,
            display_name="Yanami",
            traits={},
            updated_at=datetime(2026, 4, 11, 1, 0, tzinfo=UTC),
            importance=4,
            access_count=1,
            last_accessed=None,
        ),
    ]

    plans = module._build_merge_plans(rows, bindings={})

    assert plans == []


def test_coerce_timestamp_returns_naive_utc_datetime() -> None:
    module = _load_script_module()

    result = module._coerce_timestamp("2026-04-12T02:20:36+08:00")

    assert result == datetime(2026, 4, 11, 18, 20, 36)
    assert result.tzinfo is None

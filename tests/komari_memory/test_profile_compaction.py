"""用户画像压缩服务测试。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from komari_bot.common import profile_compaction as profile_compaction_module


class _FakeGenerateText:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        if not self._responses:
            raise AssertionError("缺少预置响应")
        return json.dumps(self._responses.pop(0), ensure_ascii=False)


def _make_config(**overrides: Any) -> Any:
    payload = {
        "llm_model_summary": "summary-model",
        "llm_temperature_summary": 0.3,
        "llm_max_tokens_summary": 2048,
        "summary_chunk_token_limit": 3000,
        "profile_trait_limit": 20,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _make_profile(trait_count: int, *, uniform: bool = False) -> dict[str, Any]:
    return {
        "version": 1,
        "user_id": "10001",
        "display_name": "阿明",
        "traits": {
            f"特征{i:02d}": {
                "value": f"长期描述{i}",
                "category": "general",
                "importance": 3 if uniform else 5 - (i % 3),
                "updated_at": (
                    "2026-03-21T00:00:00+08:00"
                    if uniform
                    else f"2026-03-21T00:00:{i % 60:02d}+08:00"
                ),
            }
            for i in range(trait_count)
        },
    }


def test_compact_profile_with_llm_single_request_caps_traits() -> None:
    """单次压缩请求：28 traits → LLM 输出 delete 操作删掉 8 条 → 保留 20 条。"""
    fake_generate_text = _FakeGenerateText(
        [
            {
                "operations": [
                    {"op": "delete", "field": "trait", "key": f"特征{i:02d}"}
                    for i in range(20, 28)
                ],
            }
        ]
    )

    result = asyncio.run(
        profile_compaction_module.compact_profile_with_llm(
            profile=_make_profile(28),
            config=_make_config(summary_chunk_token_limit=8000),
            llm_generate_text=fake_generate_text,
            trace_id="profilecap-test01",
            source="unit_test",
        )
    )

    assert profile_compaction_module.count_profile_traits(result) == 20
    assert len(fake_generate_text.calls) == 1
    assert fake_generate_text.calls[0]["request_phase"] == "profile_compaction_single"


def test_compact_profile_with_llm_chunks_then_finalizes(monkeypatch: Any) -> None:
    """分批压缩：30 traits → 分两批各删掉一批 → 合并 15 条 → 最终删至 5 条。

    uniform=True 使所有 trait 的 importance 和 updated_at 相同，
    排序后按 key 降序：特征29, 28, ..., 00。
    分批：[特征29-15] 和 [特征14-00]。
    """
    # chunk 1 (特征29-15)：delete 特征21-15 (7条) → 保留 特征29-22 (8条)
    # chunk 2 (特征14-00)：delete 特征07-00 (8条) → 保留 特征14-08 (7条)
    # 合并后 15 条，进入下一轮 → 单次请求
    # final：delete 特征24,23,22,14,13,12,11,10,09,08 (10条) → 保留 特征29-25 (5条)
    fake_generate_text = _FakeGenerateText(
        [
            {
                "operations": [
                    {"op": "delete", "field": "trait", "key": f"特征{i:02d}"}
                    for i in range(15, 22)
                ],
            },
            {
                "operations": [
                    {"op": "delete", "field": "trait", "key": f"特征{i:02d}"}
                    for i in range(8)
                ],
            },
            {
                "operations": [
                    {"op": "delete", "field": "trait", "key": f"特征{i:02d}"}
                    for i in [8, 9, 10, 11, 12, 13, 14, 22, 23, 24]
                ],
            },
        ]
    )

    monkeypatch.setattr(
        profile_compaction_module,
        "_estimate_prompt_tokens",
        lambda **kwargs: 9999 if len(kwargs["traits"]) > 20 else 200,
    )
    monkeypatch.setattr(
        profile_compaction_module,
        "_chunk_traits_for_prompt",
        lambda **kwargs: [kwargs["traits"][:15], kwargs["traits"][15:]],
    )

    result = asyncio.run(
        profile_compaction_module.compact_profile_with_llm(
            profile=_make_profile(30, uniform=True),
            config=_make_config(summary_chunk_token_limit=1000),
            llm_generate_text=fake_generate_text,
            trace_id="profilecap-test02",
            source="unit_test",
        )
    )

    assert profile_compaction_module.count_profile_traits(result) == 5
    assert [call["request_phase"] for call in fake_generate_text.calls] == [
        "profile_compaction_chunk",
        "profile_compaction_chunk",
        "profile_compaction_final",
    ]


def test_compact_profile_preserves_program_fields() -> None:
    """压缩后 user_id 和 display_name 必须与原始画像一致，不受 LLM 输出影响。"""
    fake_generate_text = _FakeGenerateText(
        [
            {
                "operations": [
                    {"op": "delete", "field": "trait", "key": f"特征{i:02d}"}
                    for i in range(20, 28)
                ],
            }
        ]
    )

    result = asyncio.run(
        profile_compaction_module.compact_profile_with_llm(
            profile=_make_profile(28),
            config=_make_config(summary_chunk_token_limit=8000),
            llm_generate_text=fake_generate_text,
            trace_id="profilecap-test03",
            source="unit_test",
        )
    )

    assert result["user_id"] == "10001"
    assert result["display_name"] == "阿明"


def test_parse_compaction_operations_rejects_invalid_ops() -> None:
    """_parse_compaction_operations 应过滤无效操作并拒绝错误格式。"""
    # 正常操作
    valid = profile_compaction_module._parse_compaction_operations(
        {
            "operations": [
                {"op": "delete", "field": "trait", "key": "a"},
                {
                    "op": "replace",
                    "field": "trait",
                    "key": "b",
                    "value": "v",
                    "category": "general",
                    "importance": 3,
                },
            ]
        }
    )
    assert len(valid) == 2

    # 混入无效操作（op 不合法、field 不合法、key 缺失）
    filtered = profile_compaction_module._parse_compaction_operations(
        {
            "operations": [
                {"op": "delete", "field": "trait", "key": "valid"},
                {"op": "invalid", "field": "trait", "key": "x"},
                {"op": "delete", "field": "invalid", "key": "y"},
                {"op": "delete", "field": "trait"},
                "not_a_dict",
            ]
        }
    )
    assert len(filtered) == 1
    assert filtered[0]["key"] == "valid"

    # 非对象 → 报错
    raised = False
    try:
        profile_compaction_module._parse_compaction_operations([])
    except TypeError:
        raised = True
    assert raised

    # 缺少 operations 数组 → 报错
    raised = False
    try:
        profile_compaction_module._parse_compaction_operations({"data": []})
    except TypeError:
        raised = True
    assert raised


def test_apply_compaction_operations() -> None:
    """_apply_compaction_operations 正确处理 add/replace/delete 操作。"""
    current = [
        {
            "key": "a",
            "value": "old_a",
            "category": "general",
            "importance": 3,
            "updated_at": "t1",
        },
        {
            "key": "b",
            "value": "old_b",
            "category": "fact",
            "importance": 4,
            "updated_at": "t2",
        },
        {
            "key": "c",
            "value": "old_c",
            "category": "preference",
            "importance": 5,
            "updated_at": "t3",
        },
    ]

    operations = [
        {"op": "delete", "field": "trait", "key": "c"},
        {
            "op": "replace",
            "field": "trait",
            "key": "a",
            "value": "new_a",
            "category": "relation",
            "importance": 2,
        },
        {
            "op": "add",
            "field": "trait",
            "key": "d",
            "value": "new_d",
            "category": "general",
            "importance": 3,
        },
        # add 已存在的 key 应被忽略
        {
            "op": "add",
            "field": "trait",
            "key": "b",
            "value": "ignored",
            "category": "general",
            "importance": 1,
        },
    ]

    result = profile_compaction_module._apply_compaction_operations(current, operations)
    result_by_key = {t["key"]: t for t in result}

    assert len(result) == 3
    assert "c" not in result_by_key
    assert result_by_key["a"]["value"] == "new_a"
    assert result_by_key["a"]["category"] == "relation"
    assert result_by_key["a"]["importance"] == 2
    assert result_by_key["b"]["value"] == "old_b"  # add 被忽略，保持原值
    assert result_by_key["d"]["value"] == "new_d"

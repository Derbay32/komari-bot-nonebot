"""用户画像压缩服务测试。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema
from komari_bot.plugins.komari_memory.services import (
    profile_compaction as profile_compaction_module,
)


class _FakeGenerateText:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        if not self._responses:
            raise AssertionError("缺少预置响应")
        return json.dumps(self._responses.pop(0), ensure_ascii=False)


def _make_config(**overrides: Any) -> KomariMemoryConfigSchema:
    payload = {
        "llm_model_summary": "summary-model",
        "llm_temperature_summary": 0.3,
        "llm_max_tokens_summary": 2048,
        "summary_chunk_token_limit": 3000,
        "profile_trait_limit": 20,
    }
    payload.update(overrides)
    return KomariMemoryConfigSchema(**payload)


def _make_profile(trait_count: int) -> dict[str, Any]:
    return {
        "version": 1,
        "user_id": "10001",
        "display_name": "阿明",
        "traits": {
            f"特征{i:02d}": {
                "value": f"长期描述{i}",
                "category": "general",
                "importance": 5 - (i % 3),
                "updated_at": f"2026-03-21T00:00:{i % 60:02d}+08:00",
            }
            for i in range(trait_count)
        },
    }


def test_compact_profile_with_llm_single_request_caps_traits() -> None:
    fake_generate_text = _FakeGenerateText(
        [
            {
                "user_id": "10001",
                "display_name": "阿明",
                "traits": [
                    {
                        "key": f"压缩后特征{i:02d}",
                        "value": f"长期信息{i}",
                        "category": "general",
                        "importance": 4,
                    }
                    for i in range(20)
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
    fake_generate_text = _FakeGenerateText(
        [
            {
                "user_id": "10001",
                "display_name": "阿明",
                "traits": [
                    {
                        "key": f"分段A{i}",
                        "value": f"信息A{i}",
                        "category": "general",
                        "importance": 4,
                    }
                    for i in range(8)
                ],
            },
            {
                "user_id": "10001",
                "display_name": "阿明",
                "traits": [
                    {
                        "key": f"分段B{i}",
                        "value": f"信息B{i}",
                        "category": "general",
                        "importance": 4,
                    }
                    for i in range(7)
                ],
            },
            {
                "user_id": "10001",
                "display_name": "阿明",
                "traits": [
                    {
                        "key": f"最终特征{i}",
                        "value": f"稳定总结{i}",
                        "category": "general",
                        "importance": 5,
                    }
                    for i in range(5)
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
            profile=_make_profile(30),
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

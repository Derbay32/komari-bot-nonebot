"""TSK-190 判定配置历史行夹具（test-support，不收集为用例）。

0025 退役了 38 个判定配置字段，生产 Schema 不再声明它们；但历史迁移边界
测试（0021/0022/0024）仍必须在**旧表结构**上写入完整一行才能越过
``NOT NULL`` 列。本模块把这份历史行写成显式测试数据：

- 不导入已删除的生产字段（期望值不来自被测 Schema）；
- 不解析 0025 迁移源文本推导删除集合（期望值不来自被测迁移）；
- 只服务 ``tests/db`` 中明确需要历史列集合的迁移边界用例。

列集合与 ``/tmp/komari-TSK-180/190-manifest.json``（TSK-159 冻结清单）逐项
一致：38 个退役字段 + 13 个保留字段 + 3 个存储字段 = 0024 时代的 54 列。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

#: 存储字段（0025 之后仍存在，迁移不得改写）。
STORAGE_COLUMNS: tuple[str, ...] = ("id", "revision", "updated_at")

#: 0025 之后仍有列的保留字段（与冻结清单 preserve_config_fields 一致）。
RETAINED_DECISION_CONFIG_FIELDS: tuple[str, ...] = (
    "plugin_enable",
    "bot_aliases",
    "jev_base_url",
    "jev_api_key",
    "jev_model",
    "jev_http_timeout_seconds",
    "jev_request_timeout_seconds",
    "max_concurrent_decisions",
    "max_queued_messages_per_group",
    "queue_timeout_seconds",
    "reference_timeout_seconds",
    "optional_reply_probability",
    "decision_log_retention_days",
)

#: 0025 删除的 38 个判定配置字段（冻结清单顺序）。
RETIRED_DECISION_CONFIG_FIELDS: tuple[str, ...] = (
    "filter_min_length",
    "filter_history_check_size",
    "message_buffer_size",
    "scene_top_k",
    "reply_threshold",
    "timing_weight",
    "noise_conf_threshold",
    "noise_margin_threshold",
    "call_margin_threshold",
    "social_window_activity_seconds",
    "social_window_dialogue_seconds",
    "social_silence_seconds",
    "social_bot_cooldown_seconds",
    "social_timing_mention_bonus",
    "social_timing_silence_bonus",
    "social_timing_activity_max_penalty",
    "social_timing_dialogue_penalty",
    "social_timing_cooldown_max_penalty",
    "social_timing_activity_threshold",
    "social_timing_activity_slope_denominator",
    "embedding_instruction_query",
    "embedding_instruction_scene",
    "rerank_instruction",
    "scene_persist_enabled",
    "scene_sync_poll_seconds",
    "scene_embedding_lease_seconds",
    "scene_embedding_max_attempts",
    "scene_embedding_retry_base_seconds",
    "scene_keep_versions",
    "summary_embedding_instruction_query",
    "summary_rerank_instruction",
    "summary_scene_top_k",
    "summary_rerank_enabled",
    "summary_rerank_threshold",
    "summary_similarity_threshold",
    "summary_rerank_fallback_enabled",
    "summary_rerank_failure_threshold",
    "summary_rerank_failure_window_seconds",
)

#: 38 个退役字段在 0024 时代表结构上的历史值。
#: 数值/文本取自 ``/tmp/komari-TSK-180/190-legacy-config-defaults.json``
#: （TSK-190 前的 Pydantic 声明默认；历史行必须写入具体值，因为旧表列
#: 全部 NOT NULL 且无 server default）。
RETIRED_HISTORICAL_VALUES: dict[str, Any] = {
    "filter_min_length": 3,
    "filter_history_check_size": 50,
    "message_buffer_size": 200,
    "scene_top_k": 4,
    "reply_threshold": 0.72,
    "timing_weight": 0.3,
    "noise_conf_threshold": 0.76,
    "noise_margin_threshold": 0.1,
    "call_margin_threshold": 0.08,
    "social_window_activity_seconds": 10,
    "social_window_dialogue_seconds": 30,
    "social_silence_seconds": 60,
    "social_bot_cooldown_seconds": 10,
    "social_timing_mention_bonus": 0.2,
    "social_timing_silence_bonus": 0.2,
    "social_timing_activity_max_penalty": 0.25,
    "social_timing_dialogue_penalty": 0.2,
    "social_timing_cooldown_max_penalty": 0.25,
    "social_timing_activity_threshold": 5,
    "social_timing_activity_slope_denominator": 10,
    "embedding_instruction_query": (
        "任务：将群聊消息编码为机器人回复场景检索向量。"
        "重点保留是否提问、是否请求机器人、情绪强度、信息密度和话题意图；"
        "忽略口头禅、语气词、无意义重复字符。"
    ),
    "embedding_instruction_scene": (
        "任务：将候选场景编码为可与用户消息匹配的语义原型向量。"
        "突出场景核心意图、适用边界和区分点。"
    ),
    "rerank_instruction": (
        "你在做群聊机器人决策精排。按语义匹配强度给候选打分："
        "1) 记忆价值（MEANINGFUL vs NOISE）；"
        "2) 呼叫意图（CALL_DIRECT vs CALL_MENTION）；"
        "3) 场景匹配（SCENE_*）。优先语义，不因礼貌措辞或语气强弱偏置。"
    ),
    "scene_persist_enabled": False,
    "scene_sync_poll_seconds": 30,
    "scene_embedding_lease_seconds": 120,
    "scene_embedding_max_attempts": 3,
    "scene_embedding_retry_base_seconds": 30,
    "scene_keep_versions": 3,
    "summary_embedding_instruction_query": (
        "任务：将群聊消息编码为群总结场景归类检索向量。"
        "重点保留消息的对话意图、话题归属、事件类型与信息价值；"
        "忽略口头禅、语气词、无意义重复字符。"
    ),
    "summary_rerank_instruction": (
        "你在做群聊总结候选场景归类精排。按语义匹配强度给候选打分："
        "1) 消息与场景的归属度；"
        "2) 场景区分度；"
        "3) 信息价值。优先语义，不因礼貌措辞或语气强弱偏置。"
    ),
    "summary_scene_top_k": 4,
    "summary_rerank_enabled": True,
    "summary_rerank_threshold": 0.6,
    "summary_similarity_threshold": None,
    "summary_rerank_fallback_enabled": False,
    "summary_rerank_failure_threshold": 3,
    "summary_rerank_failure_window_seconds": 3600,
}

#: 13 个保留字段在 0024 时代的历史值（plugin_enable 用 0024 的默认 false，
#: 需要 true 的用例显式覆盖）。
RETAINED_HISTORICAL_VALUES: dict[str, Any] = {
    "plugin_enable": False,
    "bot_aliases": ["小鞠", "小鞠知花", "komari"],
    "jev_base_url": "",
    "jev_api_key": "",
    "jev_model": "jev-1.14.0",
    "jev_http_timeout_seconds": 3.0,
    "jev_request_timeout_seconds": 3.0,
    "max_concurrent_decisions": 4,
    "max_queued_messages_per_group": 5,
    "queue_timeout_seconds": 3.0,
    "reference_timeout_seconds": 3.0,
    "optional_reply_probability": 0.5,
    "decision_log_retention_days": 7,
}

#: 0024 时代 ``komari_decision_config`` 的完整列集合（除存储字段）。
LEGACY_CONFIG_VALUE_COLUMNS: tuple[str, ...] = (
    *RETAINED_DECISION_CONFIG_FIELDS,
    *RETIRED_DECISION_CONFIG_FIELDS,
)

#: 0024 时代单行的固定写入时间（迁移不得改写 updated_at）。
LEGACY_UPDATED_AT = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)

# 列集合与值字典结构自洽，防止后续改动时两处漂移。
assert set(RETIRED_HISTORICAL_VALUES) == set(RETIRED_DECISION_CONFIG_FIELDS)
assert set(RETAINED_HISTORICAL_VALUES) == set(RETAINED_DECISION_CONFIG_FIELDS)
assert len(RETIRED_DECISION_CONFIG_FIELDS) == 38
assert len(RETAINED_DECISION_CONFIG_FIELDS) == 13
assert not set(RETIRED_DECISION_CONFIG_FIELDS) & set(RETAINED_DECISION_CONFIG_FIELDS)


def historical_decision_config_row(**overrides: Any) -> dict[str, Any]:
    """构造 0024 时代 ``komari_decision_config`` 一行的完整 JSON 值。

    旧表列全部 ``NOT NULL`` 且无 server default，因此历史边界用例必须给全
    54 列；返回的字典可直接作为 ``json_populate_record`` 的 JSON 参数
    （早期 revision 上多余的键由 PostgreSQL 忽略）。

    Args:
        **overrides: 只允许覆盖历史列集合内的列（含 ``id`` / ``revision`` /
            ``updated_at``），未知列立即失败，避免夹具静默漂移。

    Returns:
        列名 → 可 JSON 序列化值的完整字典。
    """
    allowed = {
        *STORAGE_COLUMNS,
        *LEGACY_CONFIG_VALUE_COLUMNS,
    }
    unknown = sorted(set(overrides) - allowed)
    if unknown:
        msg = f"历史判定配置行不允许覆盖未知列: {unknown}"
        raise KeyError(msg)
    row: dict[str, Any] = {
        "id": 1,
        "revision": 1,
        "updated_at": LEGACY_UPDATED_AT.isoformat(),
        **RETAINED_HISTORICAL_VALUES,
        **RETIRED_HISTORICAL_VALUES,
        **overrides,
    }
    return row

"""旧版 JSONB 配置离线迁移到强类型配置表（ticket 05 / ADR-0004）。

把 legacy 通用 JSONB KV 表 ``komari_plugin_configs``（15 个配置资源）
与 ``komari_prompt_configs``（3 个 prompt 资源）的值一次性搬运到 v2.0.0
引入的强类型单行表（``komari_<插件>_config`` / ``komari_prompt_<资源>``，
主键 ``id=1``，内部列 ``revision`` / ``updated_at``）。

本脚本是「用完即弃」的离线工具，刻意保持独立：

- 不 import 任何 ``komari_bot`` 运行时代码与新模型定义；
- 键→列映射在下方 ``_RESOURCE_SPECS`` 中静态写死（与
  ``migrations/versions/0002_typed_plugin_config_tables.py``、
  ``0003_typed_prompt_tables.py`` 逐列一致），运行时仅通过
  information_schema 校验列存在性，不读取任何模型元数据；
- 数据库直连只使用项目依赖里已有的 asyncpg；
- 连接串来自命令行 ``--dsn`` 或环境变量 ``SQLALCHEMY_DATABASE_URL``
  （``postgresql://`` 与 ``postgresql+asyncpg://`` 均可），不读取
  仓库 ``.env`` 文件。

语义约定：

- 对目标表执行 ``INSERT ... ON CONFLICT (id) DO UPDATE`` upsert，
  可重复执行且幂等；
- 只写入旧 JSONB 中存在的键对应的列；缺失的 NOT NULL 列写入类型
  中性默认值（bool→false、int→0、float→0.0、str→''、JSONB→{}，
  JSONB 列默认容器按 ``jsonb_defaults`` 静态声明），缺失的可空列
  不写入（保持 NULL）；
- ``revision`` 继承 legacy 行 revision（无值或非正数时置 1），
  ``updated_at`` 继承 legacy 行 updated_at（无值时取当前 UTC 时间），
  由脚本显式赋值；
- 决算报告输出到 stdout，按资源列出「已迁移键 / 丢弃弃用键 /
  落回默认值列」三类清单，末尾给出汇总与可重复执行提示。

用法::

    python scripts/migrate_legacy_config_to_typed_tables.py \
        --dsn postgresql://user:pass@host:5432/komari_bot
    SQLALCHEMY_DATABASE_URL=postgresql+asyncpg://... \
        python scripts/migrate_legacy_config_to_typed_tables.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

import asyncpg

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# ---------------------------------------------------------------------------
# 静态资源清单：键→列映射写死，与 migrations/0002、0003 的 DDL 逐列一致
# ---------------------------------------------------------------------------

_JSONValue = bool | int | float | str | list[Any] | dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    """一个待迁移资源的静态声明（脚本内唯一真源，不读任何模型定义）。"""

    legacy_table: str
    legacy_key_column: str
    legacy_data_column: str
    key_value: str
    target_table: str
    columns: tuple[str, ...]
    deprecated_keys: frozenset[str]
    jsonb_defaults: dict[str, list[Any] | dict[str, Any]] = field(
        default_factory=dict
    )


#: legacy 行里出现的、新表没有对应列的弃用键（进入决算报告「丢弃」清单）。
_DEPRECATED_PLUGIN_KEYS = frozenset({"version", "last_updated", "schema_name"})
_DEPRECATED_PROMPT_KEYS = frozenset({"version", "last_updated", "display_name"})

_RESOURCE_SPECS: tuple[ResourceSpec, ...] = (
    ResourceSpec(
        legacy_table="komari_plugin_configs",
        legacy_key_column="plugin_name",
        legacy_data_column="config_data",
        key_value="agent_run_logger",
        target_table="komari_agent_run_logger_config",
        columns=("log_enabled", "retention_days"),
        deprecated_keys=_DEPRECATED_PLUGIN_KEYS,
    ),
    ResourceSpec(
        legacy_table="komari_plugin_configs",
        legacy_key_column="plugin_name",
        legacy_data_column="config_data",
        key_value="embedding_provider",
        target_table="komari_embedding_provider_config",
        columns=(
            "embedding_model",
            "embedding_api_url",
            "embedding_api_key",
            "embedding_dimension",
            "request_connect_timeout_seconds",
            "request_read_timeout_seconds",
            "request_total_timeout_seconds",
            "request_retry_attempts",
            "request_retry_backoff_seconds",
            "response_max_bytes",
            "rerank_enabled",
            "rerank_model",
            "rerank_api_url",
            "rerank_api_key",
            "rerank_top_n",
        ),
        deprecated_keys=_DEPRECATED_PLUGIN_KEYS,
    ),
    ResourceSpec(
        legacy_table="komari_plugin_configs",
        legacy_key_column="plugin_name",
        legacy_data_column="config_data",
        key_value="group_history_summary",
        target_table="komari_group_history_summary_config",
        columns=(
            "plugin_enable",
            "user_whitelist",
            "group_whitelist",
            "redis_db",
            "summary_lock_ttl_seconds",
            "history_min_coverage_ratio",
            "min_summary_count",
            "max_summary_count",
            "fetch_batch_size",
            "summary_default_count",
            "summary_planning_model",
            "summary_planning_max_tokens",
            "summary_planning_round_limit",
            "summary_planning_request_api",
            "summary_planning_stream_enabled",
            "summary_planning_thinking_mode",
            "summary_planning_reasoning_effort",
            "summary_tool_scan_limit",
            "summary_model",
            "summary_temperature",
            "summary_max_tokens",
            "summary_request_api",
            "summary_stream_enabled",
            "summary_thinking_mode",
            "summary_reasoning_effort",
            "assistant_prefill_enabled",
            "layout_params",
        ),
        deprecated_keys=_DEPRECATED_PLUGIN_KEYS,
        jsonb_defaults={
            "user_whitelist": [],
            "group_whitelist": [],
            "layout_params": {},
        },
    ),
    ResourceSpec(
        legacy_table="komari_plugin_configs",
        legacy_key_column="plugin_name",
        legacy_data_column="config_data",
        key_value="komari_custom",
        target_table="komari_custom_config",
        columns=(
            "plugin_enable",
            "user_whitelist",
            "group_whitelist",
            "required_votes",
            "vote_emoji_id",
            "proposal_expire_hours",
            "list_chunk_size",
            "max_proposals_per_user",
            "redis_db",
        ),
        deprecated_keys=_DEPRECATED_PLUGIN_KEYS,
        jsonb_defaults={"user_whitelist": [], "group_whitelist": []},
    ),
    ResourceSpec(
        legacy_table="komari_plugin_configs",
        legacy_key_column="plugin_name",
        legacy_data_column="config_data",
        key_value="komari_decision",
        target_table="komari_decision_config",
        columns=(
            "plugin_enable",
            "user_whitelist",
            "group_whitelist",
            "filter_min_length",
            "filter_history_check_size",
            "message_buffer_size",
            "bot_aliases",
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
        ),
        deprecated_keys=_DEPRECATED_PLUGIN_KEYS,
        jsonb_defaults={
            "user_whitelist": [],
            "group_whitelist": [],
            "bot_aliases": [],
        },
    ),
    ResourceSpec(
        legacy_table="komari_plugin_configs",
        legacy_key_column="plugin_name",
        legacy_data_column="config_data",
        key_value="komari_help",
        target_table="komari_help_config",
        columns=(
            "plugin_enable",
            "user_whitelist",
            "group_whitelist",
            "similarity_threshold",
            "layer1_limit",
            "layer2_limit",
            "total_limit",
            "default_result_limit",
            "max_reply_result_count",
            "query_rewrite_rules",
            "auto_scan_on_startup",
            "disabled_auto_help_plugins",
            "show_category_emoji",
            "max_content_preview_length",
        ),
        deprecated_keys=_DEPRECATED_PLUGIN_KEYS,
        jsonb_defaults={
            "user_whitelist": [],
            "group_whitelist": [],
            "query_rewrite_rules": {},
            "disabled_auto_help_plugins": [],
        },
    ),
    ResourceSpec(
        legacy_table="komari_plugin_configs",
        legacy_key_column="plugin_name",
        legacy_data_column="config_data",
        key_value="komari_knowledge",
        target_table="komari_knowledge_config",
        columns=(
            "plugin_enable",
            "user_whitelist",
            "group_whitelist",
            "similarity_threshold",
            "query_rewrite_rules",
            "layer1_limit",
            "layer2_limit",
            "total_limit",
        ),
        deprecated_keys=_DEPRECATED_PLUGIN_KEYS,
        jsonb_defaults={
            "user_whitelist": [],
            "group_whitelist": [],
            "query_rewrite_rules": {},
        },
    ),
    ResourceSpec(
        legacy_table="komari_plugin_configs",
        legacy_key_column="plugin_name",
        legacy_data_column="config_data",
        key_value="komari_management",
        target_table="komari_management_config",
        columns=(
            "plugin_enable",
            "api_credentials",
            "api_allowed_origins",
            "announce_status_page_url",
            "announce_max_group_count",
            "announce_send_interval_seconds",
            "announce_request_cooldown_seconds",
        ),
        deprecated_keys=_DEPRECATED_PLUGIN_KEYS,
        jsonb_defaults={
            "api_credentials": [],
            "api_allowed_origins": [],
        },
    ),
    ResourceSpec(
        legacy_table="komari_plugin_configs",
        legacy_key_column="plugin_name",
        legacy_data_column="config_data",
        key_value="komari_memory",
        target_table="komari_memory_config",
        columns=(
            "plugin_enable",
            "user_whitelist",
            "group_whitelist",
            "redis_db",
            "llm_model_chat",
            "llm_temperature_chat",
            "llm_max_tokens_chat",
            "llm_request_api_chat",
            "llm_stream_enabled_chat",
            "llm_thinking_mode_chat",
            "llm_reasoning_effort_chat",
            "assistant_prefill_enabled",
            "dsv4_roleplay_instruct_mode",
            "vision_tool_enabled",
            "vision_image_download_max_count",
            "vision_image_download_max_bytes",
            "vision_image_download_total_max_bytes",
            "vision_image_download_max_pixels",
            "vision_image_download_concurrency",
            "vision_image_download_connect_timeout_seconds",
            "vision_image_download_read_timeout_seconds",
            "vision_image_download_total_timeout_seconds",
            "llm_model_summary",
            "llm_temperature_summary",
            "llm_max_tokens_summary",
            "llm_request_api_summary",
            "llm_stream_enabled_summary",
            "llm_thinking_mode_summary",
            "llm_reasoning_effort_summary",
            "knowledge_enabled",
            "knowledge_limit",
            "summary_idle_timeout",
            "summary_min_messages",
            "summary_max_buffer_size",
            "conversation_snapshot_ttl_seconds",
            "conversation_processing_lease_seconds",
            "profile_snapshot_ttl_seconds",
            "profile_snapshot_enable",
            "profile_trait_limit",
            "memory_agent_max_rounds",
            "memory_agent_staging_ttl_seconds",
            "memory_agent_max_tool_calls",
            "memory_agent_max_read_profiles",
            "memory_agent_max_write_operations",
            "memory_agent_lock_timeout_seconds",
            "memory_search_limit",
            "context_messages_limit",
            "context_max_utf8_bytes",
            "context_max_estimated_tokens",
            "global_interaction_enabled",
            "global_interaction_trigger_size",
            "global_interaction_summary_interval_minutes",
            "global_interaction_processing_lease_seconds",
            "bot_nickname",
            "response_tag",
            "forgetting_enabled",
            "forgetting_importance_threshold",
            "forgetting_decay_factor",
            "forgetting_access_boost",
            "forgetting_min_age_days",
            "forgetting_fuzzify_concurrency",
            "forgetting_job_lease_seconds",
            "query_rewrite_history_limit",
            "bot_aliases",
            "face_reaction_enabled",
            "face_reaction_id",
            "error_notify_enabled",
        ),
        deprecated_keys=_DEPRECATED_PLUGIN_KEYS,
        jsonb_defaults={
            "user_whitelist": [],
            "group_whitelist": [],
            "bot_aliases": [],
        },
    ),
    # KOMARIBOT-7：komari_chat 拥有自有配置表后，legacy komari_memory 行里的
    # 主动回复频控 / outbox 字段改投 komari_chat_config（同一 legacy 行，
    # key_value 与 komari_memory 一致；proactive_score_threshold 死字段不迁移，
    # 由 komari_memory 资源的 unknown 键丢弃逻辑一并清理）。
    ResourceSpec(
        legacy_table="komari_plugin_configs",
        legacy_key_column="plugin_name",
        legacy_data_column="config_data",
        key_value="komari_memory",
        target_table="komari_chat_config",
        columns=(
            "proactive_enabled",
            "proactive_cooldown",
            "proactive_max_per_hour",
            "proactive_reservation_ttl_seconds",
            "reply_commit_worker_interval_seconds",
            "reply_commit_batch_size",
            "reply_commit_lease_seconds",
            "reply_commit_max_attempts",
            "reply_commit_retry_base_seconds",
            "reply_commit_tombstone_retention_days",
        ),
        deprecated_keys=_DEPRECATED_PLUGIN_KEYS,
    ),
    ResourceSpec(
        legacy_table="komari_plugin_configs",
        legacy_key_column="plugin_name",
        legacy_data_column="config_data",
        key_value="komari_search",
        target_table="komari_search_config",
        columns=(
            "plugin_enable",
            "user_whitelist",
            "group_whitelist",
            "search_provider",
            "search_api_key",
            "search_enabled",
            "max_results",
            "result_content_limit",
            "search_timeout_seconds",
            "fetch_enabled",
            "fetch_max_urls",
            "fetch_content_limit",
            "fetch_timeout_seconds",
            "tavily_search_depth",
            "tavily_include_answer",
            "exa_search_type",
            "exa_fetch_format",
            "circuit_breaker_failure_threshold",
            "circuit_breaker_recovery_seconds",
        ),
        deprecated_keys=_DEPRECATED_PLUGIN_KEYS,
        jsonb_defaults={"user_whitelist": [], "group_whitelist": []},
    ),
    ResourceSpec(
        legacy_table="komari_plugin_configs",
        legacy_key_column="plugin_name",
        legacy_data_column="config_data",
        key_value="komari_sentry",
        target_table="komari_sentry_config",
        columns=(
            "plugin_enable",
            "dsn",
            "environment",
            "release",
            "debug",
            "error_sample_rate",
            "traces_sample_rate",
            "profiles_sample_rate",
            "attach_stacktrace",
            "send_default_pii",
            "max_breadcrumbs",
            "shutdown_timeout",
            "breadcrumb_level",
            "sentry_logs_level",
            "event_level",
        ),
        deprecated_keys=_DEPRECATED_PLUGIN_KEYS,
    ),
    ResourceSpec(
        legacy_table="komari_plugin_configs",
        legacy_key_column="plugin_name",
        legacy_data_column="config_data",
        key_value="llm_provider",
        target_table="komari_llm_provider_config",
        columns=(
            "api_token",
            "api_base",
            "model",
            "request_api",
            "stream_enabled",
            "temperature",
            "max_tokens",
            "timeout_seconds",
            "summary_task_rpm_limit",
            "chat_rpm_limit",
            "frequency_penalty",
            "extra_params",
            "vision_model",
            "vision_request_api",
            "vision_stream_enabled",
            "vision_temperature",
            "vision_max_tokens",
            "vision_thinking_mode",
            "vision_reasoning_effort",
        ),
        deprecated_keys=_DEPRECATED_PLUGIN_KEYS,
        jsonb_defaults={"extra_params": {}},
    ),
    ResourceSpec(
        legacy_table="komari_plugin_configs",
        legacy_key_column="plugin_name",
        legacy_data_column="config_data",
        key_value="sr",
        target_table="komari_sr_config",
        columns=(
            "plugin_enable",
            "user_whitelist",
            "group_whitelist",
            "sr_list",
            "list_chunk_size",
            "redis_db",
        ),
        deprecated_keys=_DEPRECATED_PLUGIN_KEYS,
        jsonb_defaults={
            "user_whitelist": [],
            "group_whitelist": [],
            "sr_list": [],
        },
    ),
    ResourceSpec(
        legacy_table="komari_plugin_configs",
        legacy_key_column="plugin_name",
        legacy_data_column="config_data",
        key_value="user_data",
        target_table="komari_user_data_config",
        columns=(
            "plugin_enable",
            "initial_favorability",
            "max_favorability_delta_per_reply",
        ),
        deprecated_keys=_DEPRECATED_PLUGIN_KEYS,
    ),
    ResourceSpec(
        legacy_table="komari_prompt_configs",
        legacy_key_column="resource_id",
        legacy_data_column="prompt_data",
        key_value="komari_chat",
        target_table="komari_prompt_komari_chat",
        columns=(
            "system_prompt",
            "memory_ack",
            "memory_ack_role",
            "output_instruction",
            "cot_prefix",
            "cot_prefix_role",
        ),
        deprecated_keys=_DEPRECATED_PROMPT_KEYS,
    ),
    ResourceSpec(
        legacy_table="komari_prompt_configs",
        legacy_key_column="resource_id",
        legacy_data_column="prompt_data",
        key_value="komari_memory_summary",
        target_table="komari_prompt_memory_summary",
        columns=(
            "memory_summary_common_system",
            "profile_agent_workflow_system",
            "summary_workflow_system",
            "json_response_example",
        ),
        deprecated_keys=_DEPRECATED_PROMPT_KEYS,
    ),
    ResourceSpec(
        legacy_table="komari_prompt_configs",
        legacy_key_column="resource_id",
        legacy_data_column="prompt_data",
        key_value="group_history_summary",
        target_table="komari_prompt_group_history_summary",
        columns=(
            "system_prompt",
            "planning_system_prompt",
            "memory_ack",
            "memory_ack_role",
            "output_instruction",
            "cot_prefix",
            "cot_prefix_role",
        ),
        deprecated_keys=_DEPRECATED_PROMPT_KEYS,
    ),
)

#: 仅用于生成 SQL 与报告的关键字白名单，防止任何外部输入拼入标识符。
_IDENTIFIER_PATTERN = "^[a-z_][a-z0-9_]*$"


def parse_dsn(dsn: str) -> dict[str, Any]:
    """解析 postgresql:// 与 postgresql+asyncpg:// 形式连接串。

    返回可直接传给 ``asyncpg.connect`` 的关键字参数。
    """
    if not dsn:
        msg = "DSN 不能为空，请通过 --dsn 或 SQLALCHEMY_DATABASE_URL 提供"
        raise ValueError(msg)

    parts = urlsplit(dsn)
    scheme = parts.scheme.lower()
    if scheme not in {"postgresql", "postgresql+asyncpg"}:
        msg = f"不支持的数据库连接串 scheme: {scheme!r}（只支持 postgresql）"
        raise ValueError(msg)
    if parts.hostname is None:
        msg = f"DSN 缺少主机名: {dsn!r}"
        raise ValueError(msg)

    return {
        "host": parts.hostname,
        "port": parts.port or 5432,
        "database": parts.path.lstrip("/") or "",
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
    }


def convert_value(raw: Any, data_type: str, column_name: str) -> Any:
    """把 JSONB 里的一个值转换为目标列类型（直连转换，不做智能猜测）。"""
    if data_type == "BOOLEAN":
        if isinstance(raw, bool):
            return raw
    elif data_type in ("SMALLINT", "INTEGER", "BIGINT"):
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
    elif data_type in ("REAL", "DOUBLE PRECISION", "NUMERIC"):
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
    elif data_type == "JSONB":
        return raw
    elif data_type in ("CHARACTER VARYING", "TEXT"):
        if isinstance(raw, str):
            return raw
    else:
        msg = f"不支持的列类型 {data_type!r}（列 {column_name}）"
        raise ValueError(msg)

    msg = (
        f"列 {column_name} 需要 {data_type} 类型，"
        f"但旧 JSONB 中的值是 {type(raw).__name__}: {raw!r}"
    )
    raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class PlannedRow:
    """一行 legacy JSONB 的迁移计划与决算元数据。

    - ``values``：INSERT 路径写入的全部值（含缺失 NOT NULL 列的类型默认值）；
    - ``update_keys``：旧 JSONB 实际存在的键，ON CONFLICT 时只覆盖这些列，
      缺失列在已播种的新表行上保持原值。
    """

    values: dict[str, Any]
    update_keys: tuple[str, ...]
    migrated_keys: list[str]
    dropped_keys: list[str]
    defaulted_keys: list[str]


def _neutral_default(
    spec: ResourceSpec,
    data_type: str,
    column_name: str,
) -> Any:
    """缺失 NOT NULL 列时使用的类型中性默认值。"""
    if data_type == "BOOLEAN":
        return False
    if data_type in ("SMALLINT", "INTEGER", "BIGINT"):
        return 0
    if data_type in ("REAL", "DOUBLE PRECISION", "NUMERIC"):
        return 0.0
    if data_type == "JSONB":
        return spec.jsonb_defaults.get(column_name, {})
    if data_type in ("CHARACTER VARYING", "TEXT"):
        return ""
    msg = f"不支持为目标列类型生成中性默认值: {data_type}（列 {column_name}）"
    raise ValueError(msg)


def plan_row_values(
    spec: ResourceSpec,
    data: Mapping[str, Any],
    column_info: Mapping[str, tuple[str, bool]],
) -> PlannedRow:
    """把一行旧 JSONB 数据映射为写入值并产出决算元数据。

    ``column_info`` 为 ``{列名: (data_type, is_nullable)}``（来自
    information_schema）。规则见模块 docstring「语义约定」。
    """
    declared = set(spec.columns)
    missing_from_db = declared - set(column_info)
    if missing_from_db:
        names = ", ".join(sorted(missing_from_db))
        msg = (
            f"资源 {spec.key_value} 静态声明的列在数据库中不存在，"
            f"请核对脚本声明与迁移版本: {names}"
        )
        raise RuntimeError(msg)

    values: dict[str, Any] = {}
    update_keys: list[str] = []
    migrated_keys: list[str] = []
    dropped_keys: list[str] = []
    defaulted_keys: list[str] = []

    for column in spec.columns:
        raw = data.get(column)
        data_type, is_nullable = column_info[column]
        if raw is None:
            # JSONB null / 缺失键：可空列不写入（保持 NULL / 播种值），
            # NOT NULL 列仅在 INSERT 路径回退类型默认值（不进入 update_keys，
            # 已播种行上的原值不会被覆盖）
            defaulted_keys.append(column)
            if not is_nullable:
                values[column] = _neutral_default(spec, data_type, column)
            continue
        values[column] = convert_value(raw, data_type, column)
        update_keys.append(column)
        migrated_keys.append(column)

    unknown_found = sorted(
        set(data).difference(declared).difference(spec.deprecated_keys)
    )
    deprecated_found = sorted(spec.deprecated_keys.intersection(data))
    dropped_keys = [*unknown_found, *deprecated_found]

    return PlannedRow(
        values=values,
        update_keys=tuple(update_keys),
        migrated_keys=migrated_keys,
        dropped_keys=dropped_keys,
        defaulted_keys=defaulted_keys,
    )


def main() -> None:
    """命令行入口（详见模块 docstring）。"""
    parser = argparse.ArgumentParser(
        description="把 legacy komari_plugin_configs / komari_prompt_configs"
        " 的 JSONB 值离线迁移到强类型配置表",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="PostgreSQL 连接串（postgresql:// 或 postgresql+asyncpg://）；"
        "缺省时读取 SQLALCHEMY_DATABASE_URL 环境变量",
    )
    args = parser.parse_args()
    dsn = args.dsn or os.environ.get("SQLALCHEMY_DATABASE_URL", "")
    kwargs = parse_dsn(dsn)

    async def _run() -> int:
        conn = await asyncpg.connect(**kwargs)
        try:
            return await run_migration(conn)
        finally:
            await conn.close()

    sys.exit(asyncio.run(_run()))


# ---------------------------------------------------------------------------
# 迁移执行
# ---------------------------------------------------------------------------

_INFO_SCHEMA_QUERY = """
SELECT table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = ANY($1)
"""


@dataclass(frozen=True, slots=True)
class ResourceReport:
    """单个资源的迁移决算。"""

    spec: ResourceSpec
    migrated: bool
    revision: int | None
    updated_at: datetime | None
    migrated_keys: list[str]
    dropped_keys: list[str]
    defaulted_keys: list[str]
    error: str | None


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """一次迁移的整体结果。"""

    reports: list[ResourceReport]
    unknown_keys: list[str]


def _check_identifier(value: str, kind: str) -> str:
    """校验标识符只含白名单字符，防注入（标识符均来自静态声明）。"""
    import re

    if re.fullmatch(_IDENTIFIER_PATTERN, value) is None:
        msg = f"非法 {kind} 标识符: {value!r}"
        raise ValueError(msg)
    return value


#: information_schema data_type → SQL 字面量类型名（用于显式占位符 cast，
#: 避免 asyncpg 对 JSONB 等参数做隐式文本推断）。
_SQL_TYPE_NAMES: dict[str, str] = {
    "BOOLEAN": "boolean",
    "SMALLINT": "smallint",
    "INTEGER": "integer",
    "BIGINT": "bigint",
    "REAL": "real",
    "DOUBLE PRECISION": "double precision",
    "NUMERIC": "numeric",
    "JSONB": "jsonb",
    "CHARACTER VARYING": "character varying",
    "TEXT": "text",
}


def build_upsert_sql(
    spec: ResourceSpec,
    values: Mapping[str, Any],
    column_types: Mapping[str, str],
    update_keys: Sequence[str],
) -> str:
    """构建单行 upsert；占位符按列类型显式 cast。

    INSERT 路径写入 ``values`` 全部列（保证空表可建行）；ON CONFLICT
    只覆盖 ``update_keys`` 中的列（旧 JSONB 实际存在的键），缺失列在
    已播种的新表行上保持原值。
    """
    table = _check_identifier(spec.target_table, "表名")
    column_names = [f'"{_check_identifier(c, '列名')}"' for c in values]
    placeholders = [
        f"${index}::{_SQL_TYPE_NAMES[column_types[column]]}"
        for index, column in enumerate(values, start=3)
    ]
    update_columns = [
        f'"{_check_identifier(c, '列名')}" = EXCLUDED."{_check_identifier(c, '列名')}"'
        for c in update_keys
    ]
    return (
        f"INSERT INTO {table} (id, revision, updated_at, "
        f"{', '.join(column_names)}) "
        f"VALUES (1, $1, $2, {', '.join(placeholders)}) "
        f"ON CONFLICT (id) DO UPDATE SET "
        f"revision = EXCLUDED.revision, updated_at = EXCLUDED.updated_at"
        f"{', ' + ', '.join(update_columns) if update_columns else ''}"
    )


async def _fetch_column_info(
    conn: asyncpg.Connection, specs: Sequence[ResourceSpec]
) -> dict[str, dict[str, tuple[str, bool]]]:
    """查询目标表列元数据: {表名: {列名: (data_type, is_nullable)}}。"""
    tables = sorted({spec.target_table for spec in specs})
    rows = await conn.fetch(_INFO_SCHEMA_QUERY, tables)
    info: dict[str, dict[str, tuple[str, bool]]] = {
        table: {} for table in tables
    }
    for row in rows:
        table = str(row["table_name"])
        if table in info:
            info[table][str(row["column_name"])] = (
                str(row["data_type"]).upper(),
                str(row["is_nullable"]).upper() == "YES",
            )
    return info


async def _fetch_legacy_rows(
    conn: asyncpg.Connection,
    legacy_table: str,
    key_column: str,
    data_column: str,
) -> list[dict[str, Any]]:
    """读取某张 legacy 表的全部行（列名统一别名，便于共用处理逻辑）。

    asyncpg 默认把 json/jsonb 解码为字符串，这里统一解析为 Python 对象。
    """
    table = _check_identifier(legacy_table, "表名")
    key = _check_identifier(key_column, "列名")
    data = _check_identifier(data_column, "列名")
    query = (
        f"SELECT {key} AS key_value, {data} AS data, revision, updated_at "
        f"FROM {table}"
    )
    rows: list[dict[str, Any]] = []
    for raw_row in await conn.fetch(query):
        row = dict(raw_row)
        raw_data = row.get("data")
        if isinstance(raw_data, str):
            row["data"] = json.loads(raw_data)
        rows.append(row)
    return rows


def _resolve_revision(raw: Any) -> int:
    """revision 决策：继承 legacy 行 revision，缺失或非正数时置 1。"""
    if isinstance(raw, int) and raw >= 1:
        return raw
    return 1


def _resolve_updated_at(raw: Any) -> datetime:
    """updated_at 决策：继承 legacy 行值，缺失时取当前 UTC 时间。"""
    if isinstance(raw, datetime):
        return raw
    return datetime.now(UTC)


async def migrate_legacy_configs(
    conn: asyncpg.Connection,
    *,
    specs: Sequence[ResourceSpec] = _RESOURCE_SPECS,
) -> MigrationResult:
    """执行全部资源的离线迁移，返回逐资源决算。"""
    # asyncpg 的 json/jsonb codec 默认只接受字符串：注册标准库编解码器，
    # 让 dict/list 参数与结果都能直接工作（重复注册无副作用）。
    for type_name in ("jsonb", "json"):
        await conn.set_type_codec(
            type_name,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    column_info = await _fetch_column_info(conn, specs)
    reports: list[ResourceReport] = []
    unknown_keys: list[str] = []

    # 按 legacy 表分组：每张表只读取一次，未识别的行按表收集
    grouped: dict[str, list[ResourceSpec]] = {}
    for spec in specs:
        grouped.setdefault(spec.legacy_table, []).append(spec)

    rows_by_table: dict[str, list[dict[str, Any]]] = {}
    for legacy_table, table_specs in grouped.items():
        rows = await _fetch_legacy_rows(
            conn,
            legacy_table,
            table_specs[0].legacy_key_column,
            table_specs[0].legacy_data_column,
        )
        rows_by_table[legacy_table] = rows
        known_keys = {spec.key_value for spec in table_specs}
        unknown_keys.extend(
            key
            for key in sorted({str(row["key_value"]) for row in rows} - known_keys)
        )

    for spec in specs:
        rows = rows_by_table[spec.legacy_table]
        row = next(
            (r for r in rows if str(r["key_value"]) == spec.key_value),
            None,
        )
        if row is None:
            reports.append(
                ResourceReport(
                    spec=spec,
                    migrated=False,
                    revision=None,
                    updated_at=None,
                    migrated_keys=[],
                    dropped_keys=[],
                    defaulted_keys=[],
                    error=None,
                )
            )
            continue

        revision = _resolve_revision(row.get("revision"))
        updated_at = _resolve_updated_at(row.get("updated_at"))
        try:
            planned = plan_row_values(
                spec,
                row["data"],
                column_info[spec.target_table],
            )
        except (ValueError, RuntimeError) as error:
            reports.append(
                ResourceReport(
                    spec=spec,
                    migrated=False,
                    revision=revision,
                    updated_at=updated_at,
                    migrated_keys=[],
                    dropped_keys=[],
                    defaulted_keys=[],
                    error=str(error),
                )
            )
            continue

        sql = build_upsert_sql(
            spec,
            planned.values,
            {
                column: column_info[spec.target_table][column][0]
                for column in planned.values
            },
            planned.update_keys,
        )
        await conn.execute(
            sql,
            revision,
            updated_at,
            *(planned.values[column] for column in planned.values),
        )
        reports.append(
            ResourceReport(
                spec=spec,
                migrated=True,
                revision=revision,
                updated_at=updated_at,
                migrated_keys=planned.migrated_keys,
                dropped_keys=planned.dropped_keys,
                defaulted_keys=planned.defaulted_keys,
                error=None,
            )
        )

    return MigrationResult(reports=reports, unknown_keys=unknown_keys)


def render_report(result: MigrationResult) -> str:
    """渲染人类可读的决算报告（stdout 直接输出）。"""
    lines: list[str] = []
    for report in result.reports:
        spec = report.spec
        source = (
            f"{spec.legacy_table}.{spec.legacy_key_column}={spec.key_value}"
        )
        lines.append(f"=== {source} -> {spec.target_table} ===")
        if not report.migrated:
            if report.error is not None:
                lines.append(f"  迁移失败: {report.error}")
            else:
                lines.append("  legacy 无行（跳过，不影响新表）")
            continue
        assert report.updated_at is not None
        lines.append(
            f"  revision={report.revision} "
            f"updated_at={report.updated_at.isoformat()}"
        )
        lines.append(
            "  已迁移键 ({}): {}".format(
                len(report.migrated_keys), ", ".join(report.migrated_keys)
            )
        )
        lines.append(
            "  丢弃弃用键 ({}): {}".format(
                len(report.dropped_keys), ", ".join(report.dropped_keys)
            )
        )
        lines.append(
            "  落回默认值列 ({}): {}".format(
                len(report.defaulted_keys),
                ", ".join(report.defaulted_keys),
            )
        )

    migrated = sum(1 for r in result.reports if r.migrated)
    failed = sum(1 for r in result.reports if r.error is not None)
    lines.append("=== 汇总 ===")
    lines.append(
        f"资源总数: {len(result.reports)} | 已迁移: {migrated} "
        f"| 跳过（legacy 无行）: {len(result.reports) - migrated - failed} "
        f"| 失败: {failed}"
    )
    if result.unknown_keys:
        lines.append(
            "未识别 legacy 资源（未迁移）: " + ", ".join(result.unknown_keys)
        )
    lines.append(
        "revision 取值说明: 继承 legacy 行 revision（缺失或非正数时置 1）;"
        " updated_at 继承 legacy 行 updated_at（缺失时取当前 UTC 时间）"
    )
    lines.append("本脚本可重复执行（幂等）: 再次运行将写入相同结果。")
    return "\n".join(lines)


async def run_migration(conn: asyncpg.Connection) -> int:
    """执行迁移并输出决算报告；返回失败资源数（0 表示全部成功）。"""
    result = await migrate_legacy_configs(conn)
    sys.stdout.write(render_report(result) + "\n")
    return sum(1 for report in result.reports if report.error is not None)


if __name__ == "__main__":
    main()

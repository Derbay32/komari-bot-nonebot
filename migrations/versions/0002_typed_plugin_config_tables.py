"""强类型动态配置表：14 个配置资源各自一张 SQLModel 单行表

迁移 ID: 0002
父迁移: 0001
创建时间: 2026-08-06 07:20:00

本 revision 为 14 个动态配置资源建立强类型存储（结构真源为
``komari_bot/config/typed_config.TypedConfigModel`` 与各插件
``config_schema`` 的 SQLModel 元数据）：

- 每张表单行使用，主键 ``id`` 恒为 1；
- ``revision`` 为跨进程 CAS 修订号，写操作原子自增；
- ``updated_at`` 为最后写入时间（带时区），由存储层显式赋值；
- 可扩展字段（列表/字典/嵌套配置）使用 JSONB；
- 旧版 ``komari_plugin_configs`` 表在本迁移中保留，不做数据回灌或删表，
  由运维按上线窗口决定退役。

跨进程配置变更通知：运行时的配置存储不再使用 asyncpg
LISTEN/NOTIFY，而是在应用事件循环上亚秒级轮询各表 ``revision``；
因此本迁移不创建触发器或通知函数。

本文件自包含，不导入任何 ``komari_bot`` 运行时代码；DDL 与
``migrations/env.py`` 合并的 SQLModel 元数据逐列一致。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE komari_agent_run_logger_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        log_enabled BOOLEAN NOT NULL,
        retention_days INTEGER NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE komari_embedding_provider_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        embedding_model VARCHAR NOT NULL,
        embedding_api_url VARCHAR NOT NULL,
        embedding_api_key VARCHAR NOT NULL,
        embedding_dimension INTEGER NOT NULL,
        request_connect_timeout_seconds FLOAT NOT NULL,
        request_read_timeout_seconds FLOAT NOT NULL,
        request_total_timeout_seconds FLOAT NOT NULL,
        request_retry_attempts INTEGER NOT NULL,
        request_retry_backoff_seconds FLOAT NOT NULL,
        response_max_bytes INTEGER NOT NULL,
        rerank_enabled BOOLEAN NOT NULL,
        rerank_model VARCHAR NOT NULL,
        rerank_api_url VARCHAR NOT NULL,
        rerank_api_key VARCHAR NOT NULL,
        rerank_top_n INTEGER NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE komari_group_history_summary_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        user_whitelist JSONB NOT NULL,
        group_whitelist JSONB NOT NULL,
        redis_db INTEGER NOT NULL,
        summary_lock_ttl_seconds INTEGER NOT NULL,
        history_min_coverage_ratio FLOAT NOT NULL,
        min_summary_count INTEGER NOT NULL,
        max_summary_count INTEGER NOT NULL,
        fetch_batch_size INTEGER NOT NULL,
        summary_default_count INTEGER NOT NULL,
        summary_planning_model VARCHAR NOT NULL,
        summary_planning_max_tokens INTEGER NOT NULL,
        summary_planning_round_limit INTEGER NOT NULL,
        summary_planning_request_api VARCHAR(32) NOT NULL,
        summary_planning_stream_enabled BOOLEAN NOT NULL,
        summary_planning_thinking_mode BOOLEAN NOT NULL,
        summary_planning_reasoning_effort VARCHAR NOT NULL,
        summary_tool_scan_limit INTEGER NOT NULL,
        summary_model VARCHAR NOT NULL,
        summary_temperature FLOAT NOT NULL,
        summary_max_tokens INTEGER NOT NULL,
        summary_request_api VARCHAR(32) NOT NULL,
        summary_stream_enabled BOOLEAN NOT NULL,
        summary_thinking_mode BOOLEAN NOT NULL,
        summary_reasoning_effort VARCHAR NOT NULL,
        assistant_prefill_enabled BOOLEAN NOT NULL,
        dsv4_roleplay_instruct_mode VARCHAR NOT NULL,
        layout_params JSONB NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE komari_custom_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        user_whitelist JSONB NOT NULL,
        group_whitelist JSONB NOT NULL,
        required_votes INTEGER NOT NULL,
        vote_emoji_id VARCHAR NOT NULL,
        proposal_expire_hours INTEGER NOT NULL,
        list_chunk_size INTEGER NOT NULL,
        max_proposals_per_user INTEGER NOT NULL,
        redis_db INTEGER NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE komari_decision_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        user_whitelist JSONB NOT NULL,
        group_whitelist JSONB NOT NULL,
        filter_min_length INTEGER NOT NULL,
        filter_history_check_size INTEGER NOT NULL,
        message_buffer_size INTEGER NOT NULL,
        bot_aliases JSONB NOT NULL,
        scene_top_k INTEGER NOT NULL,
        reply_threshold FLOAT NOT NULL,
        timing_weight FLOAT NOT NULL,
        noise_conf_threshold FLOAT NOT NULL,
        noise_margin_threshold FLOAT NOT NULL,
        call_margin_threshold FLOAT NOT NULL,
        social_window_activity_seconds INTEGER NOT NULL,
        social_window_dialogue_seconds INTEGER NOT NULL,
        social_silence_seconds INTEGER NOT NULL,
        social_bot_cooldown_seconds INTEGER NOT NULL,
        social_timing_mention_bonus FLOAT NOT NULL,
        social_timing_silence_bonus FLOAT NOT NULL,
        social_timing_activity_max_penalty FLOAT NOT NULL,
        social_timing_dialogue_penalty FLOAT NOT NULL,
        social_timing_cooldown_max_penalty FLOAT NOT NULL,
        social_timing_activity_threshold INTEGER NOT NULL,
        social_timing_activity_slope_denominator INTEGER NOT NULL,
        embedding_instruction_query VARCHAR NOT NULL,
        embedding_instruction_scene VARCHAR NOT NULL,
        rerank_instruction VARCHAR NOT NULL,
        scene_persist_enabled BOOLEAN NOT NULL,
        scene_sync_poll_seconds INTEGER NOT NULL,
        scene_embedding_lease_seconds INTEGER NOT NULL,
        scene_embedding_max_attempts INTEGER NOT NULL,
        scene_embedding_retry_base_seconds INTEGER NOT NULL,
        scene_keep_versions INTEGER NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE komari_help_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        user_whitelist JSONB NOT NULL,
        group_whitelist JSONB NOT NULL,
        similarity_threshold FLOAT NOT NULL,
        layer1_limit INTEGER NOT NULL,
        layer2_limit INTEGER NOT NULL,
        total_limit INTEGER NOT NULL,
        default_result_limit INTEGER NOT NULL,
        max_reply_result_count INTEGER NOT NULL,
        query_rewrite_rules JSONB NOT NULL,
        auto_scan_on_startup BOOLEAN NOT NULL,
        disabled_auto_help_plugins JSONB NOT NULL,
        show_category_emoji BOOLEAN NOT NULL,
        max_content_preview_length INTEGER NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE komari_knowledge_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        user_whitelist JSONB NOT NULL,
        group_whitelist JSONB NOT NULL,
        similarity_threshold FLOAT NOT NULL,
        query_rewrite_rules JSONB NOT NULL,
        layer1_limit INTEGER NOT NULL,
        layer2_limit INTEGER NOT NULL,
        total_limit INTEGER NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE komari_management_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        api_credentials JSONB NOT NULL,
        api_allowed_origins JSONB NOT NULL,
        announce_status_page_url VARCHAR NOT NULL,
        announce_max_group_count INTEGER NOT NULL,
        announce_send_interval_seconds FLOAT NOT NULL,
        announce_request_cooldown_seconds FLOAT NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE komari_memory_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        user_whitelist JSONB NOT NULL,
        group_whitelist JSONB NOT NULL,
        redis_db INTEGER NOT NULL,
        llm_model_chat VARCHAR NOT NULL,
        llm_temperature_chat FLOAT NOT NULL,
        llm_max_tokens_chat INTEGER NOT NULL,
        llm_request_api_chat VARCHAR(32) NOT NULL,
        llm_stream_enabled_chat BOOLEAN NOT NULL,
        llm_thinking_mode_chat BOOLEAN NOT NULL,
        llm_reasoning_effort_chat VARCHAR NOT NULL,
        assistant_prefill_enabled BOOLEAN NOT NULL,
        dsv4_roleplay_instruct_mode VARCHAR NOT NULL,
        vision_tool_enabled BOOLEAN NOT NULL,
        vision_image_download_max_count INTEGER NOT NULL,
        vision_image_download_max_bytes INTEGER NOT NULL,
        vision_image_download_total_max_bytes INTEGER NOT NULL,
        vision_image_download_max_pixels INTEGER NOT NULL,
        vision_image_download_concurrency INTEGER NOT NULL,
        vision_image_download_connect_timeout_seconds FLOAT NOT NULL,
        vision_image_download_read_timeout_seconds FLOAT NOT NULL,
        vision_image_download_total_timeout_seconds FLOAT NOT NULL,
        llm_model_summary VARCHAR NOT NULL,
        llm_temperature_summary FLOAT NOT NULL,
        llm_max_tokens_summary INTEGER NOT NULL,
        llm_request_api_summary VARCHAR(32) NOT NULL,
        llm_stream_enabled_summary BOOLEAN NOT NULL,
        llm_thinking_mode_summary BOOLEAN NOT NULL,
        llm_reasoning_effort_summary VARCHAR NOT NULL,
        knowledge_enabled BOOLEAN NOT NULL,
        knowledge_limit INTEGER NOT NULL,
        summary_idle_timeout INTEGER NOT NULL,
        summary_min_messages INTEGER NOT NULL,
        summary_max_buffer_size INTEGER NOT NULL,
        conversation_snapshot_ttl_seconds INTEGER NOT NULL,
        conversation_processing_lease_seconds INTEGER NOT NULL,
        profile_snapshot_ttl_seconds INTEGER NOT NULL,
        profile_snapshot_enable BOOLEAN NOT NULL,
        profile_trait_limit INTEGER NOT NULL,
        memory_agent_max_rounds INTEGER NOT NULL,
        memory_agent_staging_ttl_seconds INTEGER NOT NULL,
        memory_agent_max_tool_calls INTEGER NOT NULL,
        memory_agent_max_read_profiles INTEGER NOT NULL,
        memory_agent_max_write_operations INTEGER NOT NULL,
        memory_agent_lock_timeout_seconds INTEGER,
        memory_search_limit INTEGER NOT NULL,
        context_messages_limit INTEGER NOT NULL,
        context_max_utf8_bytes INTEGER NOT NULL,
        context_max_estimated_tokens INTEGER NOT NULL,
        global_interaction_enabled BOOLEAN NOT NULL,
        global_interaction_trigger_size INTEGER NOT NULL,
        global_interaction_summary_interval_minutes INTEGER NOT NULL,
        global_interaction_processing_lease_seconds INTEGER NOT NULL,
        proactive_enabled BOOLEAN NOT NULL,
        proactive_score_threshold FLOAT NOT NULL,
        proactive_cooldown INTEGER NOT NULL,
        proactive_max_per_hour INTEGER NOT NULL,
        proactive_reservation_ttl_seconds INTEGER NOT NULL,
        reply_commit_worker_interval_seconds INTEGER NOT NULL,
        reply_commit_batch_size INTEGER NOT NULL,
        reply_commit_lease_seconds INTEGER NOT NULL,
        reply_commit_max_attempts INTEGER NOT NULL,
        reply_commit_retry_base_seconds INTEGER NOT NULL,
        reply_commit_tombstone_retention_days INTEGER NOT NULL,
        bot_nickname VARCHAR NOT NULL,
        response_tag VARCHAR NOT NULL,
        forgetting_enabled BOOLEAN NOT NULL,
        forgetting_importance_threshold INTEGER NOT NULL,
        forgetting_decay_factor FLOAT NOT NULL,
        forgetting_access_boost FLOAT NOT NULL,
        forgetting_min_age_days INTEGER NOT NULL,
        forgetting_fuzzify_concurrency INTEGER NOT NULL,
        forgetting_job_lease_seconds INTEGER NOT NULL,
        query_rewrite_history_limit INTEGER NOT NULL,
        bot_aliases JSONB NOT NULL,
        face_reaction_enabled BOOLEAN NOT NULL,
        face_reaction_id VARCHAR NOT NULL,
        error_notify_enabled BOOLEAN NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE komari_search_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        user_whitelist JSONB NOT NULL,
        group_whitelist JSONB NOT NULL,
        search_provider VARCHAR(16) NOT NULL,
        search_api_key VARCHAR NOT NULL,
        search_enabled BOOLEAN NOT NULL,
        max_results INTEGER NOT NULL,
        result_content_limit INTEGER NOT NULL,
        search_timeout_seconds FLOAT NOT NULL,
        fetch_enabled BOOLEAN NOT NULL,
        fetch_max_urls INTEGER NOT NULL,
        fetch_content_limit INTEGER NOT NULL,
        fetch_timeout_seconds FLOAT NOT NULL,
        tavily_search_depth VARCHAR(16) NOT NULL,
        tavily_include_answer BOOLEAN NOT NULL,
        exa_search_type VARCHAR(16) NOT NULL,
        exa_fetch_format VARCHAR(16) NOT NULL,
        circuit_breaker_failure_threshold INTEGER NOT NULL,
        circuit_breaker_recovery_seconds FLOAT NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE komari_sentry_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        dsn VARCHAR NOT NULL,
        environment VARCHAR NOT NULL,
        release VARCHAR NOT NULL,
        debug BOOLEAN NOT NULL,
        error_sample_rate FLOAT NOT NULL,
        traces_sample_rate FLOAT NOT NULL,
        profiles_sample_rate FLOAT NOT NULL,
        attach_stacktrace BOOLEAN NOT NULL,
        send_default_pii BOOLEAN NOT NULL,
        max_breadcrumbs INTEGER NOT NULL,
        shutdown_timeout FLOAT NOT NULL,
        breadcrumb_level VARCHAR NOT NULL,
        sentry_logs_level VARCHAR NOT NULL,
        event_level VARCHAR NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE komari_llm_provider_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        api_token VARCHAR NOT NULL,
        api_base VARCHAR NOT NULL,
        model VARCHAR NOT NULL,
        request_api VARCHAR(32) NOT NULL,
        stream_enabled BOOLEAN NOT NULL,
        temperature FLOAT NOT NULL,
        max_tokens INTEGER NOT NULL,
        timeout_seconds FLOAT NOT NULL,
        summary_task_rpm_limit INTEGER NOT NULL,
        chat_rpm_limit INTEGER NOT NULL,
        frequency_penalty FLOAT NOT NULL,
        extra_params JSONB NOT NULL,
        vision_model VARCHAR NOT NULL,
        vision_request_api VARCHAR(32) NOT NULL,
        vision_stream_enabled BOOLEAN NOT NULL,
        vision_temperature FLOAT NOT NULL,
        vision_max_tokens INTEGER NOT NULL,
        vision_thinking_mode BOOLEAN NOT NULL,
        vision_reasoning_effort VARCHAR NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE komari_sr_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        user_whitelist JSONB NOT NULL,
        group_whitelist JSONB NOT NULL,
        sr_list JSONB NOT NULL,
        list_chunk_size INTEGER NOT NULL,
        redis_db INTEGER NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE komari_user_data_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        initial_favorability INTEGER NOT NULL,
        max_favorability_delta_per_reply INTEGER NOT NULL,
        PRIMARY KEY (id)
    )
    """,
)

_TABLE_NAMES: tuple[str, ...] = (
    "komari_agent_run_logger_config",
    "komari_embedding_provider_config",
    "komari_group_history_summary_config",
    "komari_custom_config",
    "komari_decision_config",
    "komari_help_config",
    "komari_knowledge_config",
    "komari_management_config",
    "komari_memory_config",
    "komari_search_config",
    "komari_sentry_config",
    "komari_llm_provider_config",
    "komari_sr_config",
    "komari_user_data_config",
)


def upgrade(name: str = "") -> None:
    if name:
        return

    for statement in _TABLE_STATEMENTS:
        op.execute(statement)


def downgrade(name: str = "") -> None:
    if name:
        return

    for table_name in reversed(_TABLE_NAMES):
        op.execute(f"DROP TABLE IF EXISTS {table_name}")

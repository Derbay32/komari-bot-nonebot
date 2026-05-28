"""Komari Decision 配置 Schema。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class KomariDecisionConfigSchema(BaseModel):
    """Komari Decision 插件配置。"""

    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )
    plugin_enable: bool = Field(default=False, description="插件启用状态")
    user_whitelist: list[str] = Field(
        default_factory=list, description="用户白名单，为空则允许所有用户"
    )
    group_whitelist: list[str] = Field(
        default_factory=list, description="群聊白名单，为空则允许所有群聊"
    )
    filter_min_length: int = Field(
        default=3, ge=1, le=20, description="最短消息长度阈值（字符数）"
    )
    filter_history_check_size: int = Field(
        default=50, ge=10, le=200, description="历史重复检测检查的最近消息数量"
    )
    message_buffer_size: int = Field(
        default=200, ge=50, le=1000, description="时机分析读取的消息缓存大小"
    )
    bot_aliases: list[str] = Field(
        default_factory=lambda: ["小鞠", "小鞠知花", "komari"],
        description="机器人别名列表（用于 alias 命中与 call-intent 判定）",
    )
    scene_top_k: int = Field(
        default=4, ge=1, le=8, description="scene embedding 召回数量"
    )
    reply_threshold: float = Field(
        default=0.72, ge=0.0, le=1.0, description="回复判定阈值"
    )
    timing_weight: float = Field(
        default=0.3, ge=0.0, le=1.0, description="reply_score 中 timing_score 的权重"
    )
    noise_conf_threshold: float = Field(
        default=0.76, ge=0.0, le=1.0, description="NOISE 置信度阈值"
    )
    noise_margin_threshold: float = Field(
        default=0.1, ge=0.0, le=1.0, description="NOISE 相对 MEANINGFUL 的最小领先幅度"
    )
    call_margin_threshold: float = Field(
        default=0.08,
        ge=0.0,
        le=1.0,
        description="CALL_DIRECT/CALL_MENTION 判定的最小领先幅度",
    )
    social_window_activity_seconds: int = Field(
        default=10, ge=1, le=120, description="群活跃度统计窗口（秒）"
    )
    social_window_dialogue_seconds: int = Field(
        default=30, ge=1, le=300, description="对话结构统计窗口（秒）"
    )
    social_silence_seconds: int = Field(
        default=60, ge=5, le=600, description="冷场判定阈值（秒）"
    )
    social_bot_cooldown_seconds: int = Field(
        default=10, ge=1, le=120, description="机器人近期发言惩罚阈值（秒）"
    )
    social_timing_mention_bonus: float = Field(
        default=0.2, ge=0.0, le=1.0, description="命中机器人别名时的时机加分"
    )
    social_timing_silence_bonus: float = Field(
        default=0.2, ge=0.0, le=1.0, description="冷场时的时机加分"
    )
    social_timing_activity_max_penalty: float = Field(
        default=0.25, ge=0.0, le=1.0, description="高活跃场景的最大时机惩罚"
    )
    social_timing_dialogue_penalty: float = Field(
        default=0.2, ge=0.0, le=1.0, description="两人对话场景的时机惩罚"
    )
    social_timing_cooldown_max_penalty: float = Field(
        default=0.25, ge=0.0, le=1.0, description="机器人冷却期内的最大时机惩罚"
    )
    social_timing_activity_threshold: int = Field(
        default=5, ge=1, le=50, description="群活跃惩罚开始生效的消息数量阈值"
    )
    social_timing_activity_slope_denominator: int = Field(
        default=10, ge=1, le=100, description="群活跃惩罚增长斜率分母"
    )
    embedding_instruction_query: str = Field(
        default=(
            "任务：将群聊消息编码为机器人回复场景检索向量。"
            "重点保留是否提问、是否请求机器人、情绪强度、信息密度和话题意图；"
            "忽略口头禅、语气词、无意义重复字符。"
        ),
        description="query embedding 的 instruction",
    )
    embedding_instruction_scene: str = Field(
        default=(
            "任务：将候选场景编码为可与用户消息匹配的语义原型向量。"
            "突出场景核心意图、适用边界和区分点。"
        ),
        description="scene/candidate embedding 的 instruction",
    )
    rerank_instruction: str = Field(
        default=(
            "你在做群聊机器人决策精排。按语义匹配强度给候选打分："
            "1) 记忆价值（MEANINGFUL vs NOISE）；"
            "2) 呼叫意图（CALL_DIRECT vs CALL_MENTION）；"
            "3) 场景匹配（SCENE_*）。优先语义，不因礼貌措辞或语气强弱偏置。"
        ),
        description="统一候选集 rerank 的 instruction",
    )
    scene_persist_enabled: bool = Field(
        default=False, description="是否启用 scene 持久化到 PostgreSQL"
    )
    scene_sync_poll_seconds: int = Field(
        default=30, ge=5, le=3600, description="scene runtime 指针轮询间隔（秒）"
    )
    scene_keep_versions: int = Field(
        default=3, ge=1, le=20, description="保留的 READY scene 版本数量"
    )

    @field_validator("user_whitelist", "group_whitelist", "bot_aliases", mode="before")
    @classmethod
    def parse_list_string(cls, v: Any) -> Any:
        """处理从 .env 格式解析列表。"""
        if isinstance(v, str):
            import json

            try:
                parsed = json.loads(v)
                return [str(item) for item in parsed]
            except (json.JSONDecodeError, TypeError):
                return [item.strip() for item in v.split(",") if item.strip()]
        return v

"""Komari Decision 配置 Schema。"""

import math
from typing import Any, ClassVar
from urllib.parse import urlsplit

from pydantic import field_validator
from sqlalchemy.dialects.postgresql import JSONB

from komari_bot.config.typed_config import Field, TypedConfigModel, typed_model_config

_JEV_BASE_URL_SCHEMES = frozenset({"http", "https"})
"""Jev 请求连接地址只接受这两种绝对 scheme。"""


def normalize_jev_base_url(value: str) -> str:
    """校验 Jev 连接地址；空字符串表示尚未配置。

    只接受带主机名的绝对 HTTP/HTTPS 地址，不允许夹带用户信息、查询串或
    片段。控制字符与内嵌空白在 ``urlsplit`` 之前就被拒绝：``urlsplit`` 会
    静默去掉部分控制字符，若先解析再检查，被夹带的换行或制表符可能改变
    实际请求目标。异常文本只描述字段与规则，不回显被拒值。
    """
    normalized = value.strip()
    if not normalized:
        return ""
    if any(char.isspace() or not char.isprintable() for char in normalized):
        msg = "jev_base_url 不得包含空白或控制字符"
        raise ValueError(msg)
    try:
        parts = urlsplit(normalized)
        # 端口非数字时 parts.port 在此抛出 ValueError。
        _port = parts.port
    except ValueError as exc:
        msg = "jev_base_url 不是合法的绝对 HTTP/HTTPS 地址"
        raise ValueError(msg) from exc
    if parts.scheme.lower() not in _JEV_BASE_URL_SCHEMES:
        msg = "jev_base_url 必须以 http:// 或 https:// 开头"
        raise ValueError(msg)
    if not parts.hostname:
        msg = "jev_base_url 必须包含主机名"
        raise ValueError(msg)
    if parts.username is not None or parts.password is not None:
        msg = "jev_base_url 不得夹带凭据"
        raise ValueError(msg)
    if parts.query or parts.fragment:
        msg = "jev_base_url 不得包含查询串或片段"
        raise ValueError(msg)
    return normalized


def normalize_jev_api_key(value: str) -> str:
    """按 SDK 规则校验 Jev API Key；空字符串表示尚未配置。

    SDK 只接受去首尾空白后非空、且全部为不带空格的可打印 ASCII 的凭据。
    该规则在配置写入边界执行；schema 打开 ``hide_input_in_errors``，因此
    Pydantic 的校验错误不含被拒值，凭据不会进入管理响应、审计或日志。
    """
    normalized = value.strip()
    if not normalized:
        return ""
    if not normalized.isascii() or not normalized.isprintable() or " " in normalized:
        msg = "jev_api_key 只能包含不带空格的可打印 ASCII 字符"
        raise ValueError(msg)
    return normalized


def normalize_jev_model(value: str) -> str:
    """删除模型名的首尾空白，并拒绝空模型名。"""
    normalized = value.strip()
    if not normalized:
        msg = "jev_model 不能为空"
        raise ValueError(msg)
    return normalized


def normalize_jev_timeout_seconds(value: float, *, field_name: str) -> float:
    """校验截止秒数：必须是正的有限值。"""
    if not math.isfinite(value) or value <= 0:
        msg = f"{field_name} 必须是正的有限秒数"
        raise ValueError(msg)
    return value


class KomariDecisionConfigSchema(TypedConfigModel, table=True):
    """Komari Decision 插件配置。"""

    plugin_name: ClassVar[str] = "komari_decision"
    __tablename__ = "komari_decision_config"

    model_config = typed_model_config(
        # 校验错误不得回显输入值：连接地址可能夹带凭据，API Key 本身就是秘密。
        hide_input_in_errors=True,
        json_schema_extra={
            "default_apply_mode": "immediate",
            "sections": [
                {
                    "section_id": "chat_scene",
                    "display_name": "聊天场景",
                    "order": 10,
                },
                {
                    "section_id": "summary_classification",
                    "display_name": "群总结归类",
                    "order": 20,
                },
                {
                    "section_id": "jev_request",
                    "display_name": "Jev 请求",
                    "order": 30,
                },
            ],
        },
    )

    plugin_enable: bool = Field(
        default=False,
        description=(
            "是否启用主动回复判定；关闭时聊天仅响应显式 @、文本 @ 别名或回复机器人"
        ),
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
        sa_type=JSONB,
        description="机器人别名列表（用于 alias 命中与 call-intent 判定）",
    )
    scene_top_k: int = Field(
        default=4,
        ge=1,
        le=8,
        description="scene embedding 召回数量",
        json_schema_extra={"section_id": "chat_scene"},
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
        json_schema_extra={"section_id": "chat_scene"},
    )
    embedding_instruction_scene: str = Field(
        default=(
            "任务：将候选场景编码为可与用户消息匹配的语义原型向量。"
            "突出场景核心意图、适用边界和区分点。"
        ),
        description="scene/candidate embedding 的 instruction",
        json_schema_extra={"section_id": "chat_scene"},
    )
    rerank_instruction: str = Field(
        default=(
            "你在做群聊机器人决策精排。按语义匹配强度给候选打分："
            "1) 记忆价值（MEANINGFUL vs NOISE）；"
            "2) 呼叫意图（CALL_DIRECT vs CALL_MENTION）；"
            "3) 场景匹配（SCENE_*）。优先语义，不因礼貌措辞或语气强弱偏置。"
        ),
        description="统一候选集 rerank 的 instruction",
        json_schema_extra={"section_id": "chat_scene"},
    )
    scene_persist_enabled: bool = Field(
        default=False,
        description=(
            "是否启用 scene PostgreSQL 运行时；关闭时主动回复判定按 disabled 降级，"
            "显式触发聊天仍可用"
        ),
    )
    scene_sync_poll_seconds: int = Field(
        default=30, ge=5, le=3600, description="scene runtime 指针轮询间隔（秒）"
    )
    scene_embedding_lease_seconds: int = Field(
        default=120,
        ge=30,
        le=1800,
        description="scene embedding 条目认领租约时长（秒）",
    )
    scene_embedding_max_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description="scene embedding 条目进入失败状态前的最大认领次数",
    )
    scene_embedding_retry_base_seconds: int = Field(
        default=30,
        ge=1,
        le=3600,
        description="scene embedding 失败后的指数退避基础秒数",
    )
    scene_keep_versions: int = Field(
        default=3, ge=1, le=20, description="保留的 READY scene 版本数量"
    )
    summary_embedding_instruction_query: str = Field(
        default=(
            "任务：将群聊消息编码为群总结场景归类检索向量。"
            "重点保留消息的对话意图、话题归属、事件类型与信息价值；"
            "忽略口头禅、语气词、无意义重复字符。"
        ),
        description="群总结归类 query embedding 的 instruction",
        json_schema_extra={"section_id": "summary_classification"},
    )
    summary_rerank_instruction: str = Field(
        default=(
            "你在做群聊总结候选场景归类精排。按语义匹配强度给候选打分："
            "1) 消息与场景的归属度；"
            "2) 场景区分度；"
            "3) 信息价值。优先语义，不因礼貌措辞或语气强弱偏置。"
        ),
        description="群总结归类 rerank 的 instruction",
        json_schema_extra={"section_id": "summary_classification"},
    )
    summary_scene_top_k: int = Field(
        default=4,
        ge=1,
        le=8,
        description="群总结归类 scene embedding 召回数量",
        json_schema_extra={"section_id": "summary_classification"},
    )
    summary_rerank_enabled: bool = Field(
        default=True,
        description="是否对群总结归类候选启用 rerank 精排",
        json_schema_extra={"section_id": "summary_classification"},
    )
    summary_rerank_threshold: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="群总结归类 rerank 采纳分数阈值",
        json_schema_extra={"section_id": "summary_classification"},
    )
    summary_similarity_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "群总结归类相似度阈值；为空表示未配置，显式余弦模式或 rerank "
            "失败 fallback 需要配置该阈值"
        ),
        json_schema_extra={"section_id": "summary_classification"},
    )
    summary_rerank_fallback_enabled: bool = Field(
        default=False,
        description="群总结归类 rerank 供应方失败时是否回退到相似度兜底",
        json_schema_extra={"section_id": "summary_classification"},
    )
    summary_rerank_failure_threshold: int = Field(
        default=3,
        ge=1,
        le=10,
        description=(
            "群总结归类 rerank 供应方持续失败、升级为不可用/诊断的累计次数阈值"
        ),
        json_schema_extra={"section_id": "summary_classification"},
    )
    summary_rerank_failure_window_seconds: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="群总结归类 rerank 供应方失败统计窗口（秒）",
        json_schema_extra={"section_id": "summary_classification"},
    )
    jev_base_url: str = Field(
        default="",
        description=(
            "TypeSafe System One 基础地址（绝对 http/https、不带凭据/查询串/片段）；"
            "空值表示尚未配置，请求前显式失败"
        ),
        json_schema_extra={"section_id": "jev_request"},
    )
    jev_api_key: str = Field(
        default="",
        description="TypeSafe API Key；空值表示尚未配置，请求前显式失败",
        json_schema_extra={"secret": True, "section_id": "jev_request"},
    )
    jev_model: str = Field(
        default="jev-1.14.0",
        description="System One 判定模型（删除首尾空白后必须非空）",
        json_schema_extra={"section_id": "jev_request"},
    )
    jev_http_timeout_seconds: float = Field(
        default=3.0,
        description="SDK 单次 HTTP 操作超时（秒），必须是正的有限值",
        json_schema_extra={"section_id": "jev_request"},
    )
    jev_request_timeout_seconds: float = Field(
        default=3.0,
        description=(
            "单次判定请求总截止（秒，含连接、发送、接收与响应校验），必须是正的有限值"
        ),
        json_schema_extra={"section_id": "jev_request"},
    )

    @field_validator("jev_base_url")
    @classmethod
    def validate_jev_base_url(cls, value: str) -> str:
        """拒绝畸形连接地址；空值表示尚未配置。"""
        return normalize_jev_base_url(value)

    @field_validator("jev_api_key")
    @classmethod
    def validate_jev_api_key(cls, value: str) -> str:
        """按 SDK 规则拒绝畸形凭据；空值表示尚未配置。"""
        return normalize_jev_api_key(value)

    @field_validator("jev_model")
    @classmethod
    def validate_jev_model(cls, value: str) -> str:
        """拒绝空模型，并去掉首尾空白。"""
        return normalize_jev_model(value)

    @field_validator("jev_http_timeout_seconds")
    @classmethod
    def validate_jev_http_timeout(cls, value: float) -> float:
        """拒绝非正或非有限的 SDK 单次操作超时。"""
        return normalize_jev_timeout_seconds(
            value, field_name="jev_http_timeout_seconds"
        )

    @field_validator("jev_request_timeout_seconds")
    @classmethod
    def validate_jev_request_timeout(cls, value: float) -> float:
        """拒绝非正或非有限的请求总截止。"""
        return normalize_jev_timeout_seconds(
            value, field_name="jev_request_timeout_seconds"
        )

    @field_validator("bot_aliases", mode="before")
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

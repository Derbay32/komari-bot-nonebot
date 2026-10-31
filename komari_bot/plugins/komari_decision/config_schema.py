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
    # 判定配置缺失或损坏只降级主动回复判定，不得中断整个进程启动
    # （TSK-180 §7 / §10）；未声明该政策的资源配置仍按启动必需处理。
    startup_required: ClassVar[bool] = False
    __tablename__ = "komari_decision_config"

    model_config = typed_model_config(
        # 校验错误不得回显输入值：连接地址可能夹带凭据，API Key 本身就是秘密。
        hide_input_in_errors=True,
        json_schema_extra={
            "default_apply_mode": "immediate",
            "sections": [
                {
                    "section_id": "jev_request",
                    "display_name": "Jev 请求",
                    "order": 30,
                },
                {
                    "section_id": "jev_execution",
                    "display_name": "Jev 执行",
                    "order": 40,
                },
            ],
        },
    )

    bot_aliases: list[str] = Field(
        default_factory=lambda: ["小鞠", "小鞠知花", "komari"],
        sa_type=JSONB,
        description=(
            "机器人称呼名单，只作为判定输入事实与超长正文的长度例外；"
            "文本别名不构成确定性的平台召唤"
        ),
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
    max_concurrent_decisions: int = Field(
        default=4,
        ge=1,
        description=(
            "同一部署内同时执行的判定任务上限（部署级共享额度，不是单进程或"
            "单个 SDK 调用的数量；执行名额同时覆盖引用补取与 Jev 调用）"
        ),
        json_schema_extra={"section_id": "jev_execution"},
    )
    max_queued_messages_per_group: int = Field(
        default=5,
        ge=1,
        description=(
            "同群等待执行的判定任务上限（不含当前正在执行的任务）；队列满时"
            "拒绝新任务，不挤掉已接收任务"
        ),
        json_schema_extra={"section_id": "jev_execution"},
    )
    queue_timeout_seconds: float = Field(
        default=3.0,
        description=(
            "排队等待执行名额的最长秒数，从入队时刻开始计算；必须是正的有限值"
        ),
        json_schema_extra={"section_id": "jev_execution"},
    )
    reference_timeout_seconds: float = Field(
        default=3.0,
        description=(
            "取得执行名额后整条引用链补取共享的截止秒数（单个截止覆盖整链）；"
            "必须是正的有限值"
        ),
        json_schema_extra={"section_id": "jev_execution"},
    )
    optional_reply_probability: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "optional 类判定结果的抽签命中概率（0 到 1 的有限值）；模型置信度"
            "不参与该概率，且仅新任务读取新值"
        ),
        json_schema_extra={"section_id": "jev_execution"},
    )
    decision_log_retention_days: int = Field(
        default=7,
        ge=1,
        le=30,
        description=(
            "判定诊断日志的保留天数（1 至 30）；只作用于判定诊断日志，不影响"
            "事件去重身份与回复日志"
        ),
        json_schema_extra={"section_id": "jev_execution"},
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

    @field_validator("queue_timeout_seconds")
    @classmethod
    def validate_queue_timeout(cls, value: float) -> float:
        """拒绝非正或非有限的排队等待截止。"""
        return normalize_jev_timeout_seconds(value, field_name="queue_timeout_seconds")

    @field_validator("reference_timeout_seconds")
    @classmethod
    def validate_reference_timeout(cls, value: float) -> float:
        """拒绝非正或非有限的整链引用补取截止。"""
        return normalize_jev_timeout_seconds(
            value, field_name="reference_timeout_seconds"
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

"""统一群聊准入策略的强类型单行存储 Schema。

``GroupAdmissionConfigSchema`` 是群聊准入策略在 ``komari_group_admission_config``
单行表中的 SQLModel 映射（结构真源，ADR-0012）：只持久化不可变策略修订快照
``policy``（JSONB NOT NULL），由 ``group_admission/runtime`` 解释为资格裁决。

- 存储专用字段（``id`` / ``revision`` / ``updated_at``）由
  ``TypedConfigModel`` 基类提供；
- 本资源不提供 ``plugin_enable`` 关闭总闸，也不提供旧名单兼容参数
  （统一准入接管）。
"""

from __future__ import annotations

from typing import Any, ClassVar

from sqlalchemy.dialects.postgresql import JSONB

from komari_bot.config.typed_config import Field, TypedConfigModel, typed_model_config


class GroupAdmissionConfigSchema(TypedConfigModel, table=True):
    """群聊准入策略单行表模型（仅持久化 policy 快照）。"""

    plugin_name: ClassVar[str] = "group_admission"
    __tablename__ = "komari_group_admission_config"

    model_config = typed_model_config(
        json_schema_extra={"default_apply_mode": "immediate"},
    )

    policy: dict[str, Any] = Field(
        default_factory=lambda: {"mode": "blacklist", "group_ids": []},
        description="统一准入策略修订快照（mode + 群名单）",
        sa_type=JSONB,
        json_schema_extra={"apply_mode": "immediate"},
    )
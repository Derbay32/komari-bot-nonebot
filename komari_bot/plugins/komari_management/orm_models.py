"""komari_management 公告派发账本表的 SQLModel 强类型模型。

本模块是**无副作用**的纯模型模块：只导入 SQLModel/SQLAlchemy，不导入
NoneBot 与任何业务插件，因此 Alembic 迁移环境可以直接以源文件方式加载
（见 ``komari_bot/common/typed_config.load_all_plugin_orm_models``），
模型注册进 ``SQLModel.metadata`` 供 autogenerate/check 使用。

列定义与 ``migrations/versions/0001_baseline_full_schema.py`` 中
``_announcement_statements()`` 的既有 DDL 精确对齐（列名/类型/可空/
默认/约束/索引），目标是 ``orm_bootstrap check`` 零 diff：

- ``status`` 的 CHECK 约束沿用 PostgreSQL 自动命名规则
  （``{表}_{列}_check``）显式命名，以便 autogenerate 按名匹配，
  避免生成重复约束；
- ``created_at`` / ``updated_at`` 的数据库侧默认是
  ``CURRENT_TIMESTAMP``（``server_default``），Python 侧默认值只用于
  ORM 直接构造实例（测试夹具），两者写入值语义一致；
- ``response_payload`` 用 JSONB 表达，写入 SQL NULL 必须使用
  ``sqlalchemy.null()``。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    """返回带时区的当前时间（ORM 构造默认值用）。"""
    return datetime.now(UTC)


class _AnnouncementModelBase(SQLModel):
    """非表基类：只为 ``__tablename__`` 提供 Pyright 友好的类型声明。

    SQLModel 把 ``__tablename__`` 声明为 ``declared_attr``，子类直接赋值
    字符串会被 Pyright 判定为类型冲突；这里在非表基类重新声明一次
    （只写注解、不写值，不改变任何运行时行为）。非表基类不注册
    metadata、不产生列继承。
    """

    __tablename__: ClassVar[str]  # pyright: ignore[reportIncompatibleVariableOverride]
    __table__: ClassVar[Table]


class AnnouncementDispatchRow(_AnnouncementModelBase, table=True):
    """公告请求跨 worker 幂等与冷却账本。"""

    __tablename__ = "komari_announcement_dispatches"

    request_id: str = Field(
        sa_column=Column(Text, primary_key=True, nullable=False)
    )
    payload_hash: str = Field(sa_column=Column(Text, nullable=False))
    status: str = Field(sa_column=Column(Text, nullable=False))
    owner_token: str | None = Field(
        default=None, sa_column=Column(Text)
    )
    lease_expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )
    response_payload: Any | None = Field(
        default=None, sa_column=Column(JSONB)
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('processing', 'completed', 'reconciliation_required')",
            name="komari_announcement_dispatches_status_check",
        ),
        Index(
            "idx_komari_announcement_dispatches_created_at",
            text("created_at DESC"),
        ),
    )


__all__ = ["AnnouncementDispatchRow"]

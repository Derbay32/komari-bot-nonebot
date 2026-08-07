"""用户封禁三张表的 SQLModel 强类型模型。

本模块是**无副作用**的纯模型模块：只导入 SQLModel/SQLAlchemy，不导入
NoneBot 与任何业务插件，因此 Alembic 迁移环境可以直接以源文件方式加载
（见 ``komari_bot/config/typed_config.load_all_plugin_orm_models``），
模型注册进 ``SQLModel.metadata`` 供 autogenerate/check 使用。

列定义与 ``migrations/versions/0001_baseline_full_schema.py`` 中
``_user_ban_statements()`` 的既有 DDL 精确对齐（列名/类型/可空/默认/
约束/索引），目标是 ``orm_bootstrap check`` 零 diff：

- 复合主键 ``(user_id, ban_scope)``；
- CHECK 约束沿用 PostgreSQL 自动命名规则（``{表}_{列}_check``），
  显式命名以便 autogenerate 按名匹配，避免生成重复约束；
- ``created_at`` / ``updated_at`` 的数据库侧默认是
  ``CURRENT_TIMESTAMP``（``server_default``），Python 侧默认值只用于
  ORM 直接构造实例（测试夹具与 outbox 写入），两者写入值语义一致；
- 部分索引（``WHERE ... IS NOT NULL``）用 ``postgresql_where`` 表达。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    """返回带时区的当前时间（ORM 构造默认值用）。"""
    return datetime.now(UTC)


class _UserBanModelBase(SQLModel):
    """非表基类：只为 ``__tablename__`` 提供 Pyright 友好的类型声明。

    SQLModel 把 ``__tablename__`` 声明为 ``declared_attr``，子类直接赋值
    字符串会被 Pyright 判定为类型冲突；这里在非表基类重新声明一次
    （只写注解、不写值，不改变任何运行时行为），三个表子类即可保持自然
    写法。非表基类不注册 metadata、不产生列继承。
    """

    __tablename__: ClassVar[str]  # pyright: ignore[reportIncompatibleVariableOverride]
    __table__: ClassVar[Table]


class UserBanRow(_UserBanModelBase, table=True):
    """单个用户、单个作用域的封禁记录主表。"""

    __tablename__ = "komari_user_bans"

    user_id: str = Field(
        sa_column=Column(Text, primary_key=True, nullable=False)
    )
    ban_scope: str = Field(
        sa_column=Column(Text, primary_key=True, nullable=False)
    )
    operator_id: str = Field(sa_column=Column(Text, nullable=False))
    reason: str | None = Field(sa_column=Column(Text))
    expires_at: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True))
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

    __table_args__ = (
        CheckConstraint(
            "ban_scope IN ('chat', 'command')",
            name="komari_user_bans_ban_scope_check",
        ),
        Index("idx_komari_user_bans_updated_at", text("updated_at DESC")),
        Index(
            "idx_komari_user_bans_expires_at",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL"),
        ),
    )


class UserBanCacheState(_UserBanModelBase, table=True):
    """跨 worker 缓存 revision 单例行（singleton_id 恒为 1）。"""

    __tablename__ = "komari_user_ban_cache_state"

    singleton_id: int = Field(
        sa_column=Column(SmallInteger, primary_key=True, nullable=False)
    )
    revision: int = Field(
        default=1,
        sa_column=Column(
            BigInteger,
            nullable=False,
            server_default=text("1"),
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

    __table_args__ = (
        CheckConstraint(
            "singleton_id = 1",
            name="komari_user_ban_cache_state_singleton_id_check",
        ),
    )


class UserBanNotificationOutbox(_UserBanModelBase, table=True):
    """自然解封通知持久化 outbox。"""

    __tablename__ = "komari_user_ban_notification_outbox"

    notification_id: str = Field(
        sa_column=Column(Text, primary_key=True, nullable=False)
    )
    user_id: str = Field(sa_column=Column(Text, nullable=False))
    notification_kind: str = Field(
        sa_column=Column(Text, nullable=False)
    )
    records: Any | None = Field(sa_column=Column(JSONB))
    status: str = Field(
        default="pending",
        sa_column=Column(
            Text,
            nullable=False,
            server_default=text("'pending'"),
        ),
    )
    owner_token: str | None = Field(
        default=None, sa_column=Column(Text)
    )
    lease_expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )
    available_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )
    attempt_count: int = Field(
        default=0,
        sa_column=Column(
            Integer,
            nullable=False,
            server_default=text("0"),
        ),
    )
    last_error_code: str | None = Field(
        default=None, sa_column=Column(Text)
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
    sent_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )

    __table_args__ = (
        CheckConstraint(
            "notification_kind = 'natural_expiry'",
            name="komari_user_ban_notification_outbox_notification_kind_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'sent')",
            name="komari_user_ban_notification_outbox_status_check",
        ),
        Index(
            "idx_komari_user_ban_notification_outbox_claim",
            "available_at",
            "created_at",
            postgresql_where=text("status IN ('pending', 'processing')"),
        ),
    )


__all__ = [
    "UserBanCacheState",
    "UserBanNotificationOutbox",
    "UserBanRow",
]

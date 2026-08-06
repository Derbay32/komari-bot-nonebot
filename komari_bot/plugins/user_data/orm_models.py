"""好感度两张表的 SQLModel 强类型模型。

本模块是**无副作用**的纯模型模块：只导入 SQLModel/SQLAlchemy，不导入
NoneBot 与任何业务插件，因此 Alembic 迁移环境可以直接以源文件方式加载
（见 ``komari_bot/common/typed_config.load_all_plugin_orm_models``），
模型注册进 ``SQLModel.metadata`` 供 autogenerate/check 使用。

列定义与 ``migrations/versions/0001_baseline_full_schema.py`` 中
``_user_data_statements()`` 的既有 DDL 精确对齐（列名/类型/可空/
默认/约束），目标是 ``orm_bootstrap check`` 零 diff：

- ``user_favorability`` 的 ``favorability`` 列级 CHECK 沿用 PostgreSQL
  自动命名规则（``user_favorability_favorability_check``），账本的多列
  表级 CHECK 沿用 ``user_favorability_adjustment_ledger_check``，显式
  命名以便 autogenerate 按名匹配，避免生成重复约束；
- 时间列的数据库侧默认是 ``CURRENT_TIMESTAMP``（``server_default``），
  Python 侧默认值只用于 ORM 直接构造实例（测试夹具），两者写入值
  语义一致。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    Table,
    Text,
    text,
)
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    """返回带时区的当前时间（ORM 构造默认值用）。"""
    return datetime.now(UTC)


class _UserDataModelBase(SQLModel):
    """非表基类：只为 ``__tablename__`` 提供 Pyright 友好的类型声明。

    SQLModel 把 ``__tablename__`` 声明为 ``declared_attr``，子类直接赋值
    字符串会被 Pyright 判定为类型冲突；这里在非表基类重新声明一次
    （只写注解、不写值，不改变任何运行时行为）。非表基类不注册
    metadata、不产生列继承。
    """

    __tablename__: ClassVar[str]  # pyright: ignore[reportIncompatibleVariableOverride]
    __table__: ClassVar[Table]


class UserFavorabilityRow(_UserDataModelBase, table=True):
    """单个用户的当前好感度主表（user_id 主键）。"""

    __tablename__ = "user_favorability"

    user_id: str = Field(sa_column=Column(Text, primary_key=True, nullable=False))
    favorability: int = Field(
        default=0,
        sa_column=Column(
            Integer,
            nullable=False,
            server_default=text("0"),
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
            "favorability >= 0 AND favorability <= 400",
            name="user_favorability_favorability_check",
        ),
    )


class UserFavorabilityAdjustmentLedgerRow(_UserDataModelBase, table=True):
    """好感度调整幂等账本（operation_id 主键，先认领后回填结果）。"""

    __tablename__ = "user_favorability_adjustment_ledger"

    operation_id: str = Field(sa_column=Column(Text, primary_key=True, nullable=False))
    user_id: str = Field(sa_column=Column(Text, nullable=False))
    requested_delta: int = Field(sa_column=Column(Integer, nullable=False))
    before_value: int | None = Field(default=None, sa_column=Column(Integer))
    after_value: int | None = Field(default=None, sa_column=Column(Integer))
    result_updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )

    __table_args__ = (
        CheckConstraint(
            "(before_value IS NULL AND after_value IS NULL"
            " AND result_updated_at IS NULL)"
            " OR (before_value IS NOT NULL AND after_value IS NOT NULL"
            " AND result_updated_at IS NOT NULL)",
            name="user_favorability_adjustment_ledger_check",
        ),
    )


__all__ = [
    "UserFavorabilityAdjustmentLedgerRow",
    "UserFavorabilityRow",
]

"""角色名绑定表的 SQLModel 强类型模型。

本模块是**无副作用**的纯模型模块：只导入 SQLModel/SQLAlchemy，不导入
NoneBot 与任何业务插件，因此 Alembic 迁移环境可以直接以源文件方式加载
（见 ``komari_bot/config/typed_config.load_all_plugin_orm_models``），
模型注册进 ``SQLModel.metadata`` 供 autogenerate/check 使用。

列定义与 ``migrations/versions/0002_typed_schema.py`` 中
``_character_binding_statements()`` 的既有 DDL 精确对齐（列名/类型/
可空/默认/约束），目标是 ``orm_bootstrap check`` 零 diff：

- ``user_id`` 为文本主键；
- ``created_at`` / ``updated_at`` 的数据库侧默认是 ``now()``
  （``server_default``），Python 侧默认值只用于 ORM 直接构造实例
  （测试夹具），两者写入值语义一致。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    """返回带时区的当前时间（ORM 构造默认值用）。"""
    return datetime.now(UTC)


class _CharacterBindingModelBase(SQLModel):
    """非表基类：只为 ``__tablename__`` 提供 Pyright 友好的类型声明。

    SQLModel 把 ``__tablename__`` 声明为 ``declared_attr``，子类直接赋值
    字符串会被 Pyright 判定为类型冲突；这里在非表基类重新声明一次
    （只写注解、不写值，不改变任何运行时行为）。非表基类不注册
    metadata、不产生列继承。
    """

    __tablename__: ClassVar[str]  # pyright: ignore[reportIncompatibleVariableOverride]
    __table__: ClassVar[Table]


class CharacterBindingRow(_CharacterBindingModelBase, table=True):
    """单个用户的角色名绑定记录（user_id 主键）。"""

    __tablename__ = "komari_character_bindings"

    user_id: str = Field(sa_column=Column(Text, primary_key=True, nullable=False))
    character_name: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("now()"),
        ),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("now()"),
        ),
    )


class CharacterBindingGroupRow(_CharacterBindingModelBase, table=True):
    """已验证的 QQ 应用群映射。

    ``group_id`` 是 OneBot 侧群号，``group_openid`` 是 QQ 官方协议的群
    身份。两者只在同一个 ``app_id`` 内互相唯一；OneBot 的 ``self_id``
    是连接属性，不进入这个关系，因此更换连接不会拆分同一个群。
    """

    __tablename__ = "komari_character_binding_groups"

    app_id: str = Field(sa_column=Column(Text, primary_key=True, nullable=False))
    group_openid: str = Field(
        sa_column=Column(Text, primary_key=True, nullable=False)
    )
    group_id: str = Field(sa_column=Column(Text, nullable=False))
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
        UniqueConstraint(
            "app_id",
            "group_id",
            name="uq_komari_character_binding_groups_app_group_id",
        ),
    )


class CharacterBindingMemberRow(_CharacterBindingModelBase, table=True):
    """已验证群成员的双协议身份及当前群角色名。"""

    __tablename__ = "komari_character_binding_members"

    app_id: str = Field(sa_column=Column(Text, primary_key=True, nullable=False))
    group_openid: str = Field(
        sa_column=Column(Text, primary_key=True, nullable=False)
    )
    member_openid: str = Field(
        sa_column=Column(Text, primary_key=True, nullable=False)
    )
    member_qq: str = Field(sa_column=Column(Text, nullable=False))
    character_name: str | None = Field(default=None, sa_column=Column(Text))
    character_name_key: str | None = Field(
        default=None,
        sa_column=Column(Text),
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
        ForeignKeyConstraint(
            ["app_id", "group_openid"],
            [
                "komari_character_binding_groups.app_id",
                "komari_character_binding_groups.group_openid",
            ],
            ondelete="CASCADE",
            name="fk_komari_character_binding_members_group",
        ),
        UniqueConstraint(
            "app_id",
            "group_openid",
            "member_qq",
            name="uq_komari_character_binding_members_qq",
        ),
        UniqueConstraint(
            "app_id",
            "group_openid",
            "character_name_key",
            name="uq_komari_character_binding_members_name",
        ),
    )


__all__ = [
    "CharacterBindingGroupRow",
    "CharacterBindingMemberRow",
    "CharacterBindingRow",
]

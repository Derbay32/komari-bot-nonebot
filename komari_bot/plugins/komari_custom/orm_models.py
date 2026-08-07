"""komari_custom 知识库提案表的 SQLModel 强类型模型。

本模块是**无副作用**的纯模型模块：只导入 SQLModel/SQLAlchemy，不导入
NoneBot 与任何业务插件，因此 Alembic 迁移环境可以直接以源文件方式加载
（见 ``komari_bot/config/typed_config.load_all_plugin_orm_models``），
模型注册进 ``SQLModel.metadata`` 供 autogenerate/check 使用。

列定义与 ``migrations/versions/0001_baseline_full_schema.py`` 中
``_custom_proposal_statements()`` 的既有 DDL 精确对齐（列名/类型/可空/
默认/约束/索引），目标是 ``orm_bootstrap check`` 零 diff：

- ``id SERIAL PRIMARY KEY`` 由 SQLAlchemy 对整数主键的 autoincrement 渲染；
- ``status VARCHAR(20)`` 用 ``String(20)`` 表达，避免 autogenerate 把
  TEXT/VARCHAR 判为差异；
- 唯一索引 ``idx_custom_proposals_publication_key`` 以 unique 索引表达，
  供 ``ON CONFLICT (publication_key)`` 幂等写入使用；
- 部分索引（``WHERE vote_message_id IS NOT NULL``）用 ``postgresql_where``
  表达；
- 时间列的数据库侧默认是 ``now()``（``server_default``），Python 侧默认值
  只用于 ORM 直接构造实例（测试夹具），两者写入值语义一致。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Table,
    Text,
    text,
)
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    """返回带时区的当前时间（ORM 构造默认值用）。"""
    return datetime.now(UTC)


class _ProposalModelBase(SQLModel):
    """非表基类：只为 ``__tablename__`` 提供 Pyright 友好的类型声明。

    SQLModel 把 ``__tablename__`` 声明为 ``declared_attr``，子类直接赋值
    字符串会被 Pyright 判定为类型冲突；这里在非表基类重新声明一次
    （只写注解、不写值，不改变任何运行时行为）。非表基类不注册
    metadata、不产生列继承。
    """

    __tablename__: ClassVar[str]  # pyright: ignore[reportIncompatibleVariableOverride]
    __table__: ClassVar[Table]


class ProposalRow(_ProposalModelBase, table=True):
    """.custom 知识库提案表（含发布/采纳状态机字段）。"""

    __tablename__ = "komari_custom_proposals"

    id: int = Field(
        sa_column=Column(Integer, primary_key=True, nullable=False)
    )
    group_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    proposer_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    proposer_name: str | None = Field(
        default=None, sa_column=Column(Text)
    )
    title: str = Field(sa_column=Column(Text, nullable=False))
    content: str = Field(sa_column=Column(Text, nullable=False))
    status: str = Field(
        default="publishing",
        sa_column=Column(
            String(20),
            nullable=False,
            server_default=text("'publishing'"),
        ),
    )
    publication_key: str = Field(sa_column=Column(Text, nullable=False))
    publication_token: str | None = Field(
        default=None, sa_column=Column(Text)
    )
    publication_started_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )
    publication_attempts: int = Field(
        default=0,
        sa_column=Column(
            Integer,
            nullable=False,
            server_default=text("0"),
        ),
    )
    publication_error_code: str | None = Field(
        default=None, sa_column=Column(Text)
    )
    approval_token: str | None = Field(
        default=None, sa_column=Column(Text)
    )
    approval_started_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )
    vote_message_id: int | None = Field(
        default=None, sa_column=Column(BigInteger)
    )
    vote_count: int = Field(
        default=0,
        sa_column=Column(
            Integer,
            nullable=False,
            server_default=text("0"),
        ),
    )
    required_votes: int = Field(sa_column=Column(Integer, nullable=False))
    voted_users: list[str] = Field(
        default_factory=list,
        sa_column=Column(
            ARRAY(Text),
            nullable=False,
            server_default=text("'{}'"),
        ),
    )
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
    approved_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )
    knowledge_id: int | None = Field(
        default=None, sa_column=Column(Integer)
    )
    expired_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )

    __table_args__ = (
        Index("idx_custom_proposals_status", "status"),
        Index("idx_custom_proposals_group_id", "group_id"),
        Index(
            "idx_custom_proposals_proposer_status",
            "proposer_id",
            "status",
        ),
        Index(
            "idx_custom_proposals_vote_message_id",
            "vote_message_id",
            postgresql_where=text("vote_message_id IS NOT NULL"),
        ),
        Index(
            "idx_custom_proposals_publication_key",
            "publication_key",
            unique=True,
        ),
    )


__all__ = ["ProposalRow"]

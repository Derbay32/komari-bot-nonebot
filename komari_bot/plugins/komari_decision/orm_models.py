"""Jev 判定存储的 SQLModel 强类型模型。

本模块是**无副作用**的纯模型模块：只导入 SQLModel/SQLAlchemy，不导入
NoneBot 与任何业务插件，因此 Alembic 迁移环境可以直接以源文件方式加载
（见 ``komari_bot/config/typed_config.load_all_plugin_orm_models``），
模型注册进 ``SQLModel.metadata`` 供 autogenerate/check 使用。

TSK-190 之后这里只保留 Jev 判定链的存储模型：判定进程、调度器、群轮转
位置、事件占位/交接资格与可清理的判定诊断日志。旧场景四表
（``komari_decision_scenes`` / ``komari_memory_scene_set`` /
``komari_memory_scene_item`` / ``komari_memory_scene_runtime``）已由
0025 退役，本模块不再声明它们。

列定义与 ``migrations/versions/0002_typed_schema.py`` /
``0023_jev_decision_storage.py`` / ``0024_jev_recent_settlement.py`` 的既有 DDL
精确对齐（列名/类型/可空/默认/约束/索引），目标是 ``orm_bootstrap check``
零 diff：

- 时间列的数据库侧默认是 ``now()``（``server_default``），Python 侧默认值
  只用于 ORM 直接构造实例（测试夹具），两者写入值语义一致；
- CHECK / UNIQUE / FK 约束沿用 PostgreSQL 自动命名规则显式命名，以便
  autogenerate 按名匹配，避免生成重复约束。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    """返回带时区的当前时间（ORM 构造默认值用）。"""
    return datetime.now(UTC)


class _DecisionModelBase(SQLModel):
    """非表基类：只为 ``__tablename__`` 提供 Pyright 友好的类型声明。

    SQLModel 把 ``__tablename__`` 声明为 ``declared_attr``，子类直接赋值
    字符串会被 Pyright 判定为类型冲突；这里在非表基类重新声明一次
    （只写注解、不写值，不改变任何运行时行为）。非表基类不注册
    metadata、不产生列继承。
    """

    __tablename__: ClassVar[str]  # pyright: ignore[reportIncompatibleVariableOverride]
    __table__: ClassVar[Table]


class DecisionProcessRow(_DecisionModelBase, table=True):
    """判定进程实例；存活由该实例持有的 PostgreSQL 会话锁证明。"""

    __tablename__ = "komari_decision_processes"

    id: int | None = Field(default=None, sa_column=Column(Integer, primary_key=True))
    started_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=text("now()")
        ),
    )
    stopped_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    __table_args__ = (
        CheckConstraint("id > 0", name="ck_decision_process_id_positive"),
        Index("idx_decision_processes_active", "stopped_at"),
    )


class DecisionSchedulerRow(_DecisionModelBase, table=True):
    """部署共享的调度序号、停用代数与私有代号密钥。"""

    __tablename__ = "komari_decision_scheduler"

    id: int = Field(default=1, sa_column=Column(Integer, primary_key=True))
    pseudonym_salt: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    next_grant_sequence: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default=text("0")),
    )
    generation: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default=text("0")),
    )
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_decision_scheduler_singleton"),
        CheckConstraint(
            "octet_length(pseudonym_salt) = 32", name="ck_decision_salt_length"
        ),
        CheckConstraint("next_grant_sequence >= 0", name="ck_decision_grant_sequence"),
        CheckConstraint("generation >= 0", name="ck_decision_generation"),
    )


class DecisionGroupRow(_DecisionModelBase, table=True):
    """群在共享轮转中的位置；不保存群消息正文。"""

    __tablename__ = "komari_decision_groups"

    protocol: str = Field(sa_column=Column(Text, primary_key=True))
    account_id: str = Field(sa_column=Column(Text, primary_key=True))
    group_id: str = Field(sa_column=Column(Text, primary_key=True))
    last_grant_sequence: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default=text("0")),
    )
    __table_args__ = (
        CheckConstraint("last_grant_sequence >= 0", name="ck_decision_group_sequence"),
    )


class DecisionDispatchRow(_DecisionModelBase, table=True):
    """持久事件占位与单次交接资格；日志清理不能删除本表记录。"""

    __tablename__ = "komari_decision_dispatches"

    id: int | None = Field(default=None, sa_column=Column(BigInteger, primary_key=True))
    decision_id: str = Field(sa_column=Column(Uuid(as_uuid=False), nullable=False))
    protocol: str = Field(sa_column=Column(Text, nullable=False))
    account_id: str = Field(sa_column=Column(Text, nullable=False))
    group_id: str = Field(sa_column=Column(Text, nullable=False))
    message_id: str = Field(sa_column=Column(Text, nullable=False))
    owner_process_id: int = Field(
        sa_column=Column(
            Integer, ForeignKey("komari_decision_processes.id"), nullable=False
        )
    )
    dispatch_id: str = Field(sa_column=Column(Uuid(as_uuid=False), nullable=False))
    generation: int = Field(sa_column=Column(BigInteger, nullable=False))
    state: str = Field(sa_column=Column(Text, nullable=False))
    reason_code: str = Field(sa_column=Column(Text, nullable=False))
    enqueued_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    queue_deadline: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    granted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    slot_held: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    business_route: str = Field(
        default="none",
        sa_column=Column(Text, nullable=False, server_default=text("'none'")),
    )
    handed_off_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    notification_eligible: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    notified_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    recent_retained: bool | None = Field(
        default=None, sa_column=Column(Boolean, nullable=True)
    )
    """成功判定的输入缓存处置事实；不保存分类或正文。"""

    __table_args__ = (
        UniqueConstraint(
            "protocol",
            "account_id",
            "group_id",
            "message_id",
            name="uq_decision_event_identity",
        ),
        UniqueConstraint("decision_id", name="uq_decision_dispatch_id"),
        CheckConstraint("generation >= 0", name="ck_decision_dispatch_generation"),
        CheckConstraint(
            "state IN ('queued','running','success','filtered','bypass','failed','cancelled')",
            name="ck_decision_dispatch_state",
        ),
        CheckConstraint(
            "business_route IN ('none','chat','summary')",
            name="ck_decision_business_route",
        ),
        CheckConstraint(
            "NOT slot_held OR state IN ('running','cancelled')",
            name="ck_decision_slot_state",
        ),
        Index("idx_decision_dispatch_owner", "owner_process_id", "state"),
        Index("idx_decision_dispatch_queue", "state", "queue_deadline"),
        Index(
            "idx_decision_dispatch_group", "protocol", "account_id", "group_id", "id"
        ),
    )


class DecisionLogRow(_DecisionModelBase, table=True):
    """可清理的判定诊断；正文列只用于独立受权读取。"""

    __tablename__ = "komari_decision_logs"

    id: str = Field(sa_column=Column(Uuid(as_uuid=False), primary_key=True))
    dispatch_row_id: int | None = Field(
        default=None,
        sa_column=Column(
            BigInteger, ForeignKey("komari_decision_dispatches.id"), nullable=True
        ),
    )
    protocol: str = Field(sa_column=Column(Text, nullable=False))
    account_id: str = Field(sa_column=Column(Text, nullable=False))
    group_id: str = Field(sa_column=Column(Text, nullable=False))
    message_id: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    finished_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    status: str = Field(sa_column=Column(Text, nullable=False))
    reason_code: str = Field(sa_column=Column(Text, nullable=False))
    config_revision: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    prompt_revision: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    requested_model: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    actual_model: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    body: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB(none_as_null=True), nullable=True)
    )
    __table_args__ = (
        CheckConstraint(
            "status IN ('success','filtered','bypass','failed','cancelled','duplicate')",
            name="ck_decision_log_status",
        ),
        Index("idx_decision_logs_created", "created_at"),
        Index("idx_decision_logs_status", "status", "created_at"),
        Index("idx_decision_logs_requested_model", "requested_model", "created_at"),
        Index("idx_decision_logs_actual_model", "actual_model", "created_at"),
    )

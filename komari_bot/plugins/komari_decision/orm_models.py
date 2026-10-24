"""旧场景与 Jev 判定存储的 SQLModel 强类型模型。

本模块是**无副作用**的纯模型模块：只导入 SQLModel/SQLAlchemy，不导入
NoneBot 与任何业务插件，因此 Alembic 迁移环境可以直接以源文件方式加载
（见 ``komari_bot/config/typed_config.load_all_plugin_orm_models``），
模型注册进 ``SQLModel.metadata`` 供 autogenerate/check 使用。

列定义与 ``migrations/versions/0002_typed_schema.py`` 中
``_scene_statements()`` 的既有 DDL 精确对齐（列名/类型/可空/默认/
约束/索引），目标是 ``orm_bootstrap check`` 零 diff：

- 四张表均为纯关系表：场景主表、构建集、条目快照、运行时单例；
- BIGSERIAL 主键由 SQLAlchemy 对整数主键的 autoincrement 渲染；
- 时间列的数据库侧默认是 ``now()``（``server_default``），Python 侧
  默认值只用于 ORM 直接构造实例（测试夹具），两者写入值语义一致；
- CHECK / UNIQUE / FK 约束沿用 PostgreSQL 自动命名规则显式命名，
  以便 autogenerate 按名匹配，避免生成重复约束；
- ``embedding REAL[]`` 用 ``ARRAY(REAL)`` 映射，写入 None 一律使用
  ``sqlalchemy.null()``（数组列写 Python None 不保证为 SQL NULL）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    ARRAY,
    REAL,
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


class DecisionSceneRow(_DecisionModelBase, table=True):
    """判定场景主表：当前生效的场景内容定义。"""

    __tablename__ = "komari_decision_scenes"

    id: int = Field(
        sa_column=Column(BigInteger, primary_key=True, nullable=False)
    )
    scene_key: str = Field(sa_column=Column(Text, nullable=False))
    scene_type: str = Field(sa_column=Column(Text, nullable=False))
    content_text: str = Field(sa_column=Column(Text, nullable=False))
    content_hash: str = Field(sa_column=Column(Text, nullable=False))
    enabled: bool = Field(
        default=True,
        sa_column=Column(
            Boolean,
            nullable=False,
            server_default=text("true"),
        ),
    )
    order_index: int = Field(
        default=0,
        sa_column=Column(
            Integer,
            nullable=False,
            server_default=text("0"),
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

    __table_args__ = (
        UniqueConstraint(
            "scene_key",
            name="komari_decision_scenes_scene_key_key",
        ),
        CheckConstraint(
            "scene_type IN ('fixed', 'general')",
            name="komari_decision_scenes_scene_type_check",
        ),
        Index(
            "idx_komari_decision_scenes_type_order",
            "scene_type",
            "enabled",
            "order_index",
        ),
        Index("idx_komari_decision_scenes_content_hash", "content_hash"),
    )


class MemorySceneSetRow(_DecisionModelBase, table=True):
    """场景构建集：同一来源指纹的唯一构建批次。"""

    __tablename__ = "komari_memory_scene_set"

    id: int = Field(
        sa_column=Column(BigInteger, primary_key=True, nullable=False)
    )
    source_path: str = Field(sa_column=Column(Text, nullable=False))
    source_hash: str = Field(sa_column=Column(Text, nullable=False))
    embedding_model: str = Field(sa_column=Column(Text, nullable=False))
    embedding_instruction_hash: str = Field(
        sa_column=Column(Text, nullable=False)
    )
    status: str = Field(sa_column=Column(Text, nullable=False))
    item_total: int = Field(
        default=0,
        sa_column=Column(
            Integer,
            nullable=False,
            server_default=text("0"),
        ),
    )
    item_ready: int = Field(
        default=0,
        sa_column=Column(
            Integer,
            nullable=False,
            server_default=text("0"),
        ),
    )
    item_failed: int = Field(
        default=0,
        sa_column=Column(
            Integer,
            nullable=False,
            server_default=text("0"),
        ),
    )
    error_message: str | None = Field(
        default=None, sa_column=Column(Text)
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("now()"),
        ),
    )
    ready_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('BUILDING', 'READY', 'FAILED')",
            name="komari_memory_scene_set_status_check",
        ),
        CheckConstraint(
            "item_total >= 0",
            name="komari_memory_scene_set_item_total_check",
        ),
        CheckConstraint(
            "item_ready >= 0",
            name="komari_memory_scene_set_item_ready_check",
        ),
        CheckConstraint(
            "item_failed >= 0",
            name="komari_memory_scene_set_item_failed_check",
        ),
        Index(
            "idx_komari_memory_scene_set_status",
            "status",
            text("created_at DESC"),
        ),
        Index("idx_komari_memory_scene_set_source_hash", "source_hash"),
        Index(
            "idx_komari_memory_scene_set_fingerprint",
            "source_hash",
            "embedding_model",
            "embedding_instruction_hash",
            unique=True,
        ),
    )


class MemorySceneItemRow(_DecisionModelBase, table=True):
    """场景条目快照：保存构建时刻的场景内容，外键级联删除。"""

    __tablename__ = "komari_memory_scene_item"

    id: int = Field(
        sa_column=Column(BigInteger, primary_key=True, nullable=False)
    )
    set_id: int = Field(
        sa_column=Column(
            BigInteger,
            ForeignKey(
                "komari_memory_scene_set.id",
                ondelete="CASCADE",
                name="komari_memory_scene_item_set_id_fkey",
            ),
            nullable=False,
        )
    )
    scene_id: int = Field(
        sa_column=Column(
            BigInteger,
            ForeignKey(
                "komari_decision_scenes.id",
                ondelete="CASCADE",
                name="komari_memory_scene_item_scene_id_fkey",
            ),
            nullable=False,
        )
    )
    scene_key_snapshot: str = Field(sa_column=Column(Text, nullable=False))
    scene_type_snapshot: str = Field(sa_column=Column(Text, nullable=False))
    content_text_snapshot: str = Field(sa_column=Column(Text, nullable=False))
    enabled_snapshot: bool = Field(sa_column=Column(Boolean, nullable=False))
    order_index_snapshot: int = Field(sa_column=Column(Integer, nullable=False))
    content_hash: str = Field(sa_column=Column(Text, nullable=False))
    embedding: list[float] | None = Field(
        default=None,
        sa_column=Column(ARRAY(REAL)),
    )
    embedding_dim: int | None = Field(
        default=None, sa_column=Column(Integer)
    )
    status: str = Field(sa_column=Column(Text, nullable=False))
    error_message: str | None = Field(
        default=None, sa_column=Column(Text)
    )
    last_error_code: str | None = Field(
        default=None, sa_column=Column(Text)
    )
    attempt_count: int = Field(
        default=0,
        sa_column=Column(
            Integer,
            nullable=False,
            server_default=text("0"),
        ),
    )
    next_retry_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )
    lease_owner: str | None = Field(
        default=None, sa_column=Column(Text)
    )
    lease_expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )
    embedded_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True)),
    )

    __table_args__ = (
        UniqueConstraint(
            "set_id",
            "scene_id",
            name="komari_memory_scene_item_set_id_scene_id_key",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'READY', 'FAILED')",
            name="ck_komari_memory_scene_item_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="komari_memory_scene_item_attempt_count_check",
        ),
        Index("idx_komari_memory_scene_item_scene_id", "scene_id"),
        Index(
            "idx_komari_memory_scene_item_set_status",
            "set_id",
            "status",
        ),
        Index(
            "idx_komari_memory_scene_item_claim",
            "set_id",
            "status",
            "next_retry_at",
            "lease_expires_at",
        ),
        Index(
            "idx_komari_memory_scene_item_reuse",
            "scene_id",
            "content_hash",
        ),
    )


class MemorySceneRuntimeRow(_DecisionModelBase, table=True):
    """运行时单例行：指向当前 active scene set 的指针（id 恒为 1）。"""

    __tablename__ = "komari_memory_scene_runtime"

    id: int = Field(
        sa_column=Column(
            Integer,
            primary_key=True,
            nullable=False,
            server_default=text("1"),
            autoincrement=False,
        )
    )
    active_set_id: int | None = Field(
        default=None,
        sa_column=Column(
            BigInteger,
            ForeignKey(
                "komari_memory_scene_set.id",
                name="komari_memory_scene_runtime_active_set_id_fkey",
            ),
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

    __table_args__ = (
        CheckConstraint(
            "id = 1",
            name="komari_memory_scene_runtime_id_check",
        ),
    )


__all__ = [
    "DecisionSceneRow",
    "MemorySceneItemRow",
    "MemorySceneRuntimeRow",
    "MemorySceneSetRow",
]

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

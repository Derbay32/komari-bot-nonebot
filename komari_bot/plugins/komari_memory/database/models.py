"""Komari Memory ORM 数据模型。"""

from datetime import datetime

from nonebot_plugin_orm import Model
from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column


class Entity(Model):
    """实体表 - 存储用户/群组结构化信息。

    表名自动生成: komari_memory_entity
    """

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String(50))
    importance: Mapped[int] = mapped_column(Integer, default=3)
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    last_accessed: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ProactiveLog(Model):
    """主动回复日志。

    表名自动生成: komari_memory_proactivelog
    """

    group_id: Mapped[str] = mapped_column(String(64))
    trigger_score: Mapped[float] = mapped_column(Float)
    trigger_message_id: Mapped[str] = mapped_column(String(64))
    response_content: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

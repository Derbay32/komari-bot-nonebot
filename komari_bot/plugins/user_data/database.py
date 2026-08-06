"""User data PostgreSQL access layer（SQLModel + nonebot-plugin-orm AsyncSession）。

连接池与 engine 生命周期由 nonebot-plugin-orm 托管（本模块不再依赖
``komari_bot.common.postgres`` 自研池）；表结构由 Alembic 迁移统一管理，
启动期与懒路径均无任何 DDL。同表只保留本仓储一套访问路径。

SQLModel 字段在 Pyright 下被推断为 Python 值类型而非列表达式，因此
列访问统一走 ``模型.__table__.c``（与 user_ban 仓储同一约定）。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from nonebot import logger
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from .models import (
    FavorabilityAdjustmentResult,
    FavorabilitySetResult,
    UserFavorability,
)
from .orm_models import (
    UserFavorabilityAdjustmentLedgerRow,
    UserFavorabilityRow,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession

    from .config_schema import DynamicConfigSchema

_FAV = UserFavorabilityRow.__table__
_LEDGER = UserFavorabilityAdjustmentLedgerRow.__table__


def _open_session() -> "AsyncSession":
    """打开绑定 nonebot-plugin-orm 共享引擎的会话。"""
    from nonebot_plugin_orm import get_session

    return get_session(expire_on_commit=False)


class UserDataDB:
    """用户数据数据库操作类（PostgreSQL）。"""

    def __init__(self, config: "DynamicConfigSchema") -> None:
        self.config = config
        self._initialize_lock = asyncio.Lock()
        self._ready = False

    async def initialize(self) -> None:
        """单飞确认 ORM 存储可连接；表结构由 Alembic 迁移统一管理。"""
        async with self._initialize_lock:
            if self._ready:
                return
            logger.debug("[UserDataDB] 开始初始化 PostgreSQL 连接")
            session = _open_session()
            try:
                await session.execute(select(1))
            finally:
                await session.close()
            self._ready = True
            logger.debug("[UserDataDB] PostgreSQL 连接初始化完成")

    def _require_ready(self) -> None:
        """未初始化时拒绝读写，保持原“连接池未初始化”错误语义。"""
        if not self._ready:
            msg = "UserDataDB 连接池未初始化"
            raise RuntimeError(msg)

    async def close(self) -> None:
        """重置就绪状态；engine 生命周期由 nonebot-plugin-orm 托管。"""
        async with self._initialize_lock:
            self._ready = False

    async def get_user_favorability(self, user_id: str) -> UserFavorability:
        """获取用户当前好感度，无记录时创建初始值。"""
        self._require_ready()
        session = _open_session()
        try:
            async with session.begin():
                statement = postgres_insert(UserFavorabilityRow).values(
                    user_id=user_id,
                    favorability=self.config.initial_favorability,
                )
                await session.execute(statement.on_conflict_do_nothing())
                row = await session.get(UserFavorabilityRow, user_id)
        finally:
            await session.close()

        if row is None:
            msg = "好感度读取或初始化后未返回记录"
            raise RuntimeError(msg)

        return UserFavorability.from_score(
            user_id=row.user_id,
            favorability=row.favorability,
            updated_at=row.updated_at.isoformat(),
        )

    async def adjust_user_favorability(
        self,
        user_id: str,
        delta: int,
        *,
        operation_id: str | None = None,
    ) -> FavorabilityAdjustmentResult:
        """原子调整用户当前好感度，并限制在 [0, 400]。"""
        self._require_ready()

        logger.debug(
            "[UserDataDB] 开始调整好感度: user={} delta={} initial={} idempotent={}",
            user_id,
            delta,
            self.config.initial_favorability,
            operation_id is not None,
        )
        session = _open_session()
        try:
            async with session.begin():
                if operation_id is not None:
                    operation_id = operation_id.strip()
                    if not operation_id:
                        msg = "operation_id 不能为空"
                        raise ValueError(msg)
                    claimed = (
                        await session.execute(
                            postgres_insert(UserFavorabilityAdjustmentLedgerRow)
                            .values(
                                operation_id=operation_id,
                                user_id=user_id,
                                requested_delta=delta,
                            )
                            .on_conflict_do_nothing()
                            .returning(_LEDGER.c.operation_id)
                        )
                    ).scalar_one_or_none()
                    if claimed is None:
                        existing = (
                            await session.execute(
                                select(
                                    _LEDGER.c.user_id,
                                    _LEDGER.c.requested_delta,
                                    _LEDGER.c.before_value,
                                    _LEDGER.c.after_value,
                                    _LEDGER.c.result_updated_at,
                                ).where(_LEDGER.c.operation_id == operation_id)
                            )
                        ).one_or_none()
                        if existing is None:
                            msg = "好感度幂等账本记录不可用"
                            raise RuntimeError(msg)
                        existing_user = str(existing[0])
                        existing_delta = int(existing[1])
                        before_value = existing[2]
                        after_value = existing[3]
                        result_updated_at = existing[4]
                        if (
                            before_value is None
                            or after_value is None
                            or result_updated_at is None
                        ):
                            msg = "好感度幂等账本记录不可用"
                            raise RuntimeError(msg)
                        if existing_user != user_id or existing_delta != delta:
                            msg = "好感度 operation_id 与既有请求载荷冲突"
                            raise ValueError(msg)
                        return FavorabilityAdjustmentResult.from_values(
                            user_id=existing_user,
                            before=int(before_value),
                            delta=existing_delta,
                            after=int(after_value),
                            updated_at=result_updated_at.isoformat(),
                        )

                statement = postgres_insert(UserFavorabilityRow).values(
                    user_id=user_id,
                    favorability=self.config.initial_favorability,
                )
                await session.execute(statement.on_conflict_do_nothing())
                before_value = (
                    await session.execute(
                        select(_FAV.c.favorability)
                        .where(_FAV.c.user_id == user_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                logger.debug(
                    "[UserDataDB] 已锁定好感度行: user={} before={}",
                    user_id,
                    before_value,
                )
                before = int(
                    before_value
                    if before_value is not None
                    else self.config.initial_favorability
                )
                row = (
                    await session.execute(
                        update(UserFavorabilityRow)
                        .where(_FAV.c.user_id == user_id)
                        .values(
                            favorability=func.least(
                                400,
                                func.greatest(0, _FAV.c.favorability + delta),
                            ),
                            updated_at=func.now(),
                        )
                        .returning(
                            _FAV.c.user_id,
                            _FAV.c.favorability,
                            _FAV.c.updated_at,
                        )
                    )
                ).one()

                if operation_id is not None:
                    await session.execute(
                        update(UserFavorabilityAdjustmentLedgerRow)
                        .where(_LEDGER.c.operation_id == operation_id)
                        .values(
                            before_value=before,
                            after_value=int(row[1]),
                            result_updated_at=row[2],
                        )
                    )
        finally:
            await session.close()

        after = int(row[1])
        logger.debug(
            "[UserDataDB] 好感度调整完成: user={} before={} delta={} after={} updated_at={}",
            row[0],
            before,
            delta,
            after,
            row[2],
        )

        return FavorabilityAdjustmentResult.from_values(
            user_id=row[0],
            before=before,
            delta=delta,
            after=after,
            updated_at=row[2].isoformat(),
        )

    async def cleanup_adjustment_ledger(self, *, retention_days: int) -> int:
        """清理超过防重窗口的好感度 operation 账本。"""
        self._require_ready()
        session = _open_session()
        try:
            async with session.begin():
                result = cast(
                    "CursorResult[Any]",
                    await session.execute(
                        delete(UserFavorabilityAdjustmentLedgerRow).where(
                            _LEDGER.c.created_at
                            < func.now()
                            - func.make_interval(0, 0, 0, max(1, retention_days))
                        )
                    ),
                )
        finally:
            await session.close()
        return int(result.rowcount or 0)

    async def set_user_favorability(
        self,
        user_id: str,
        value: int,
    ) -> FavorabilitySetResult:
        """原子设置用户当前好感度为绝对值。

        对新用户以配置中的 initial_favorability 作为 before；
        已有用户以当前实际值作为 before。通过行锁与 adjust 串行化。
        """
        if not 0 <= value <= 400:
            msg = f"好感度值 {value} 越界，需在 [0, 400] 范围内"
            raise ValueError(msg)

        self._require_ready()

        logger.debug(
            "[UserDataDB] 开始设置好感度: user={} value={} initial={}",
            user_id,
            value,
            self.config.initial_favorability,
        )
        session = _open_session()
        try:
            async with session.begin():
                statement = postgres_insert(UserFavorabilityRow).values(
                    user_id=user_id,
                    favorability=self.config.initial_favorability,
                )
                await session.execute(statement.on_conflict_do_nothing())
                before_value = (
                    await session.execute(
                        select(_FAV.c.favorability)
                        .where(_FAV.c.user_id == user_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                before = int(
                    before_value
                    if before_value is not None
                    else self.config.initial_favorability
                )
                logger.debug(
                    "[UserDataDB] 已锁定好感度行: user={} before={}",
                    user_id,
                    before,
                )
                row = (
                    await session.execute(
                        update(UserFavorabilityRow)
                        .where(_FAV.c.user_id == user_id)
                        .values(
                            favorability=value,
                            updated_at=func.now(),
                        )
                        .returning(
                            _FAV.c.user_id,
                            _FAV.c.favorability,
                            _FAV.c.updated_at,
                        )
                    )
                ).one_or_none()
        finally:
            await session.close()

        if row is None:
            logger.error(
                "[UserDataDB] 好感度 SET 未返回记录: user={} value={}",
                user_id,
                value,
            )
            msg = "好感度 SET 未返回记录"
            raise RuntimeError(msg)

        after = int(row[1])
        logger.debug(
            "[UserDataDB] 好感度设置完成: user={} before={} after={} updated_at={}",
            row[0],
            before,
            after,
            row[2],
        )

        return FavorabilitySetResult.from_values(
            user_id=row[0],
            before=before,
            after=after,
            updated_at=row[2].isoformat(),
        )

    async def get_user_count(self) -> int:
        """获取总用户数。"""
        self._require_ready()
        session = _open_session()
        try:
            value = (
                await session.execute(
                    select(func.count()).select_from(UserFavorabilityRow)
                )
            ).scalar_one()
        finally:
            await session.close()
        return int(value or 0)

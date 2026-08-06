"""Scene 持久化数据访问仓库（SQLModel + nonebot-plugin-orm AsyncSession）。

连接池与 engine 生命周期由 nonebot-plugin-orm 托管：本仓库所有读写统一走
``nonebot_plugin_orm.get_session()`` 打开的 AsyncSession，不再使用 asyncpg
直连。构造参数 ``pg_pool`` 仅为保持外部调用面（komari_decision 插件管理器
与 komari_management scene API 均以该签名构造）；连接来源切换属于 ticket
10 范围，此处不消费该池。表结构由 Alembic 迁移统一管理，启动期与懒路径
均无任何 DDL。

SQLModel 字段在 Pyright 下被推断为 Python 值类型而非列表达式，因此
列访问统一走 ``模型.__table__.c``（与 user_ban / user_data 仓储同一约定）。
JSONB / ARRAY 列写 SQL NULL 必须使用 ``sqlalchemy.null()``。
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, cast

from nonebot import logger
from sqlalchemy import (
    Interval,
    case,
    delete,
    func,
    null,
    or_,
    select,
    update,
)
from sqlalchemy import (
    cast as sqlalchemy_cast,
)
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from ..orm_models import (
    DecisionSceneRow,
    MemorySceneItemRow,
    MemorySceneRuntimeRow,
    MemorySceneSetRow,
)

if TYPE_CHECKING:
    import asyncpg
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession

_SCENES = DecisionSceneRow.__table__
_SETS = MemorySceneSetRow.__table__
_ITEMS = MemorySceneItemRow.__table__
_RUNTIME = MemorySceneRuntimeRow.__table__

_SCENE_COLUMNS = (
    _SCENES.c.id,
    _SCENES.c.scene_key,
    _SCENES.c.scene_type,
    _SCENES.c.content_text,
    _SCENES.c.content_hash,
    _SCENES.c.enabled,
    _SCENES.c.order_index,
    _SCENES.c.created_at,
    _SCENES.c.updated_at,
)

_SET_COLUMNS = (
    _SETS.c.id,
    _SETS.c.source_path,
    _SETS.c.source_hash,
    _SETS.c.embedding_model,
    _SETS.c.embedding_instruction_hash,
    _SETS.c.status,
    _SETS.c.item_total,
    _SETS.c.item_ready,
    _SETS.c.item_failed,
    _SETS.c.error_message,
    _SETS.c.created_at,
    _SETS.c.ready_at,
)

_ITEM_ALIASED_FULL = (
    _ITEMS.c.id,
    _ITEMS.c.set_id,
    _ITEMS.c.scene_id,
    _ITEMS.c.scene_key_snapshot.label("scene_key"),
    _ITEMS.c.scene_type_snapshot.label("scene_type"),
    _ITEMS.c.content_text_snapshot.label("content_text"),
    _ITEMS.c.content_hash,
    _ITEMS.c.enabled_snapshot.label("enabled"),
    _ITEMS.c.order_index_snapshot.label("order_index"),
    _ITEMS.c.embedding,
    _ITEMS.c.embedding_dim,
    _ITEMS.c.status,
    _ITEMS.c.error_message,
    _ITEMS.c.last_error_code,
    _ITEMS.c.attempt_count,
    _ITEMS.c.next_retry_at,
    _ITEMS.c.lease_owner,
    _ITEMS.c.lease_expires_at,
    _ITEMS.c.embedded_at,
)

_ITEM_ALIASED_REUSE = (
    _ITEMS.c.id,
    _ITEMS.c.set_id,
    _ITEMS.c.scene_id,
    _ITEMS.c.scene_key_snapshot.label("scene_key"),
    _ITEMS.c.scene_type_snapshot.label("scene_type"),
    _ITEMS.c.content_text_snapshot.label("content_text"),
    _ITEMS.c.content_hash,
    _ITEMS.c.enabled_snapshot.label("enabled"),
    _ITEMS.c.order_index_snapshot.label("order_index"),
    _ITEMS.c.embedding,
    _ITEMS.c.embedding_dim,
    _ITEMS.c.status,
    _ITEMS.c.error_message,
    _ITEMS.c.embedded_at,
)


def _interval(seconds: float) -> Any:
    """把秒数构造成 ``CAST(%s AS INTERVAL)`` 表达式（PG 侧原生 interval）。"""
    from datetime import timedelta

    return sqlalchemy_cast(timedelta(seconds=seconds), Interval)


def _open_session() -> "AsyncSession":
    """打开绑定 nonebot-plugin-orm 共享引擎的会话。"""
    from nonebot_plugin_orm import get_session

    return get_session(expire_on_commit=False)


class SceneRepository:
    """Scene 持久化数据访问仓库。"""

    def __init__(self, pg_pool: "asyncpg.Pool") -> None:
        """初始化仓库。

        ``pg_pool`` 仅保留签名兼容，实际读写走 nonebot-plugin-orm 共享引擎；
        表结构由 Alembic 迁移统一管理，启动期/懒路径均无 DDL。
        """
        self.pg_pool = pg_pool

    @staticmethod
    def compute_text_hash(text: str) -> str:
        """计算 scene 内容哈希。"""
        return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()

    @staticmethod
    def compute_scene_source_hash(scenes: list[dict[str, Any]]) -> str:
        """基于启用 scene 内容计算规范化来源哈希。"""
        payload = {
            "scenes": [
                {
                    "scene_key": str(scene["scene_key"]),
                    "scene_type": str(scene["scene_type"]),
                    "content_hash": str(scene["content_hash"]),
                    "enabled": bool(scene.get("enabled", True)),
                    "order_index": int(scene.get("order_index", 0)),
                }
                for scene in scenes
            ]
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def list_scenes(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        """列出 scene 内容表记录。"""
        statement = select(*_SCENE_COLUMNS).order_by(
            _SCENES.c.order_index,
            _SCENES.c.id,
        )
        if enabled_only:
            statement = statement.where(_SCENES.c.enabled.is_(True))
        session = _open_session()
        try:
            rows = (await session.execute(statement)).mappings().all()
        finally:
            await session.close()
        return [dict(row) for row in rows]

    async def get_scene_by_key(self, scene_key: str) -> dict[str, Any] | None:
        """按 scene_key 获取 scene 内容记录。"""
        session = _open_session()
        try:
            row = (
                await session.execute(
                    select(*_SCENE_COLUMNS).where(_SCENES.c.scene_key == scene_key)
                )
            ).mappings().one_or_none()
        finally:
            await session.close()
        return dict(row) if row else None

    async def upsert_scene(
        self,
        *,
        scene_key: str,
        scene_type: str,
        content_text: str,
        enabled: bool = True,
        order_index: int = 0,
    ) -> dict[str, Any]:
        """新增或更新 scene 内容记录。"""
        scene_key = scene_key.strip()
        scene_type = scene_type.strip()
        content_text = content_text.strip()
        if not scene_key:
            msg = "scene_key 不能为空"
            raise ValueError(msg)
        if scene_type not in {"fixed", "general"}:
            msg = "scene_type 只能是 fixed 或 general"
            raise ValueError(msg)
        if not content_text:
            msg = "content_text 不能为空"
            raise ValueError(msg)
        content_hash = self.compute_text_hash(content_text)
        statement = (
            postgres_insert(_SCENES)
            .values(
                scene_key=scene_key,
                scene_type=scene_type,
                content_text=content_text,
                content_hash=content_hash,
                enabled=enabled,
                order_index=order_index,
            )
            .on_conflict_do_update(
                index_elements=["scene_key"],
                set_={
                    "scene_type": scene_type,
                    "content_text": content_text,
                    "content_hash": content_hash,
                    "enabled": enabled,
                    "order_index": order_index,
                    "updated_at": func.now(),
                },
            )
            .returning(*_SCENE_COLUMNS)
        )
        session = _open_session()
        try:
            async with session.begin():
                row = (await session.execute(statement)).mappings().one()
        finally:
            await session.close()
        return dict(row)

    async def delete_scene(self, scene_key: str) -> bool:
        """删除未引用 scene；已有历史 set 引用时仅停用当前版本。"""
        if scene_key in {"NOISE", "MEANINGFUL", "CALL_DIRECT", "CALL_MENTION"}:
            msg = f"必需 fixed scene 不允许删除: {scene_key}"
            raise ValueError(msg)
        session = _open_session()
        try:
            async with session.begin():
                referenced = (
                    await session.execute(
                        select(_ITEMS.c.id)
                        .select_from(_ITEMS)
                        .join(_SCENES, _SCENES.c.id == _ITEMS.c.scene_id)
                        .where(_SCENES.c.scene_key == scene_key)
                        .limit(1)
                    )
                ).first()
                if referenced is not None:
                    result = cast(
                        "CursorResult[Any]",
                        await session.execute(
                            update(_SCENES)
                            .where(_SCENES.c.scene_key == scene_key)
                            .values(enabled=False, updated_at=func.now())
                        ),
                    )
                else:
                    result = cast(
                        "CursorResult[Any]",
                        await session.execute(
                            delete(_SCENES).where(_SCENES.c.scene_key == scene_key)
                        ),
                    )
        finally:
            await session.close()
        return int(result.rowcount or 0) > 0

    async def has_any_scene(self) -> bool:
        """检查 scene 内容表是否已有记录。"""
        session = _open_session()
        try:
            row = (await session.execute(select(_SCENES.c.id).limit(1))).first()
        finally:
            await session.close()
        return row is not None

    async def get_or_create_scene_set(
        self,
        *,
        source_path: str,
        source_hash: str,
        embedding_model: str,
        embedding_instruction_hash: str,
        status: str = "BUILDING",
    ) -> tuple[dict[str, Any], bool]:
        """按唯一 fingerprint 原子创建或返回已有 scene set。"""
        session = _open_session()
        try:
            async with session.begin():
                row = (
                    await session.execute(
                        postgres_insert(_SETS)
                        .values(
                            source_path=source_path,
                            source_hash=source_hash,
                            embedding_model=embedding_model,
                            embedding_instruction_hash=embedding_instruction_hash,
                            status=status,
                        )
                        .on_conflict_do_nothing(
                            index_elements=[
                                "source_hash",
                                "embedding_model",
                                "embedding_instruction_hash",
                            ]
                        )
                        .returning(*_SET_COLUMNS)
                    )
                ).mappings().one_or_none()
                created = row is not None
                if row is None:
                    row = (
                        await session.execute(
                            select(*_SET_COLUMNS).where(
                                _SETS.c.source_hash == source_hash,
                                _SETS.c.embedding_model == embedding_model,
                                _SETS.c.embedding_instruction_hash
                                == embedding_instruction_hash,
                            )
                        )
                    ).mappings().one_or_none()
                if row is None:
                    msg = "scene set fingerprint 写入后无法读取"
                    raise RuntimeError(msg)
        finally:
            await session.close()

        scene_set = dict(row)
        if created:
            logger.info(
                "[KomariDecision] 创建 scene set: id={} status={} model={}",
                scene_set["id"],
                status,
                embedding_model,
            )
        return scene_set, created

    async def insert_scene_items(
        self,
        set_id: int,
        items: list[dict[str, Any]],
    ) -> int:
        """批量插入 scene 条目。"""
        if not items:
            return 0

        values_list: list[dict[str, Any]] = []
        for item in items:
            scene_id = item.get("scene_id")
            if scene_id is None:
                scene = await self.get_scene_by_key(str(item["scene_key"]))
                if scene is None:
                    msg = f"scene 内容记录不存在: {item['scene_key']}"
                    raise ValueError(msg)
                scene_id = scene["id"]
            embedding = item.get("embedding")
            values_list.append(
                {
                    "set_id": set_id,
                    "scene_id": int(scene_id),
                    "scene_key_snapshot": str(item["scene_key"]),
                    "scene_type_snapshot": str(item["scene_type"]),
                    "content_text_snapshot": str(item["content_text"]),
                    "enabled_snapshot": bool(item.get("enabled", True)),
                    "order_index_snapshot": int(item.get("order_index", 0)),
                    "content_hash": str(item["content_hash"]),
                    # ARRAY 列写 SQL NULL 必须显式使用 null()（JSONB/数组列
                    # 传 Python None 不保证为 SQL NULL）
                    "embedding": embedding if embedding is not None else null(),
                    "embedding_dim": item.get("embedding_dim"),
                    "status": str(item.get("status", "PENDING")),
                    "error_message": item.get("error_message"),
                    "embedded_at": item.get("embedded_at"),
                }
            )

        statement = (
            postgres_insert(_ITEMS)
            .on_conflict_do_nothing(index_elements=["set_id", "scene_id"])
            .returning(_ITEMS.c.id)
        )
        session = _open_session()
        try:
            inserted_count = 0
            async with session.begin():
                for values in values_list:
                    inserted_id = (
                        await session.execute(statement.values(**values))
                    ).scalar_one_or_none()
                    if inserted_id is not None:
                        inserted_count += 1
        finally:
            await session.close()

        logger.info(
            "[KomariDecision] 批量插入 scene item: set={} count={}",
            set_id,
            inserted_count,
        )
        return inserted_count

    async def get_scene_set(self, set_id: int) -> dict[str, Any] | None:
        """获取指定 scene set。"""
        session = _open_session()
        try:
            row = (
                await session.execute(
                    select(*_SET_COLUMNS).where(_SETS.c.id == set_id)
                )
            ).mappings().one_or_none()
        finally:
            await session.close()
        return dict(row) if row else None

    async def get_latest_ready_set(self) -> dict[str, Any] | None:
        """获取最新 READY scene set。"""
        session = _open_session()
        try:
            row = (
                await session.execute(
                    select(*_SET_COLUMNS)
                    .where(_SETS.c.status == "READY")
                    .order_by(
                        func.coalesce(_SETS.c.ready_at, _SETS.c.created_at).desc(),
                        _SETS.c.id.desc(),
                    )
                    .limit(1)
                )
            ).mappings().one_or_none()
        finally:
            await session.close()
        return dict(row) if row else None

    async def list_ready_sets(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """按时间倒序列出 READY scene set。"""
        statement = (
            select(*_SET_COLUMNS)
            .where(_SETS.c.status == "READY")
            .order_by(
                func.coalesce(_SETS.c.ready_at, _SETS.c.created_at).desc(),
                _SETS.c.id.desc(),
            )
        )
        if limit is not None:
            statement = statement.limit(limit)
        session = _open_session()
        try:
            rows = (await session.execute(statement)).mappings().all()
        finally:
            await session.close()
        return [dict(row) for row in rows]

    async def get_latest_set_by_fingerprint(
        self,
        source_hash: str,
        embedding_model: str,
        embedding_instruction_hash: str,
        *,
        status: str | None = None,
    ) -> dict[str, Any] | None:
        """按 fingerprint 获取最新 set，可选限定状态。"""
        statement = select(*_SET_COLUMNS).where(
            _SETS.c.source_hash == source_hash,
            _SETS.c.embedding_model == embedding_model,
            _SETS.c.embedding_instruction_hash == embedding_instruction_hash,
        )
        if status is not None:
            statement = statement.where(_SETS.c.status == status)
        statement = statement.order_by(
            _SETS.c.created_at.desc(),
            _SETS.c.id.desc(),
        ).limit(1)
        session = _open_session()
        try:
            row = (await session.execute(statement)).mappings().one_or_none()
        finally:
            await session.close()
        return dict(row) if row else None

    async def _ensure_runtime_row(self, session: "AsyncSession") -> None:
        """确保 runtime 指针行存在（幂等）。"""
        await session.execute(
            postgres_insert(_RUNTIME)
            .values(id=1, active_set_id=null())
            .on_conflict_do_nothing(index_elements=["id"])
        )

    async def get_active_set(self) -> dict[str, Any] | None:
        """在事务中读取当前 active scene set 指针。"""
        session = _open_session()
        try:
            async with session.begin():
                await self._ensure_runtime_row(session)
                row = (
                    await session.execute(
                        select(
                            *_SET_COLUMNS,
                            _RUNTIME.c.updated_at.label("runtime_updated_at"),
                        )
                        .select_from(_RUNTIME)
                        .join(
                            _SETS,
                            _SETS.c.id == _RUNTIME.c.active_set_id,
                            isouter=True,
                        )
                        .where(_RUNTIME.c.id == 1)
                        .with_for_update(key_share=True, of=_RUNTIME)
                    )
                ).mappings().one_or_none()
        finally:
            await session.close()
        if row is None or row["id"] is None:
            return None
        return dict(row)

    async def switch_active_set(self, set_id: int) -> None:
        """原子切换 active set（仅允许 READY 版本）。"""
        session = _open_session()
        try:
            async with session.begin():
                set_row = (
                    await session.execute(
                        select(_SETS.c.id, _SETS.c.status)
                        .where(_SETS.c.id == set_id)
                        .with_for_update()
                    )
                ).mappings().one_or_none()
                if set_row is None:
                    msg = f"scene set 不存在: {set_id}"
                    raise ValueError(msg)
                status = str(set_row["status"])
                if status != "READY":
                    msg = f"scene set 非 READY 状态，无法激活: id={set_id} status={status}"
                    raise ValueError(msg)

                await self._ensure_runtime_row(session)
                await session.execute(
                    select(_RUNTIME.c.active_set_id)
                    .where(_RUNTIME.c.id == 1)
                    .with_for_update()
                )
                await session.execute(
                    update(_RUNTIME)
                    .where(_RUNTIME.c.id == 1)
                    .values(active_set_id=set_id, updated_at=func.now())
                )
        finally:
            await session.close()
        logger.info("[KomariDecision] 原子切换 active scene set: id={}", set_id)

    async def list_items_by_set(
        self,
        set_id: int,
        status: str | None = None,
        *,
        enabled_only: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """按 set 获取 scene 条目。"""
        statement = select(*_ITEM_ALIASED_FULL).where(_ITEMS.c.set_id == set_id)
        if status is not None:
            statement = statement.where(_ITEMS.c.status == status)
        if enabled_only:
            statement = statement.where(_ITEMS.c.enabled_snapshot.is_(True))
        statement = statement.order_by(
            _ITEMS.c.order_index_snapshot,
            _ITEMS.c.id,
        )
        if limit is not None:
            statement = statement.limit(limit)
        session = _open_session()
        try:
            rows = (await session.execute(statement)).mappings().all()
        finally:
            await session.close()
        return [dict(row) for row in rows]

    async def find_reusable_ready_item(
        self,
        *,
        scene_id: int | None = None,
        content_hash: str,
        embedding_model: str,
        embedding_instruction_hash: str,
        scene_key: str | None = None,
    ) -> dict[str, Any] | None:
        """查找可复用 embedding 的 READY 条目。"""
        if scene_id is None:
            if scene_key is None:
                msg = "scene_id 或 scene_key 必须提供一个"
                raise ValueError(msg)
            scene = await self.get_scene_by_key(scene_key)
            if scene is None:
                return None
            scene_id = int(scene["id"])
        session = _open_session()
        try:
            row = (
                await session.execute(
                    select(*_ITEM_ALIASED_REUSE)
                    .join(_SETS, _SETS.c.id == _ITEMS.c.set_id)
                    .where(
                        _ITEMS.c.scene_id == scene_id,
                        _ITEMS.c.content_hash == content_hash,
                        _ITEMS.c.status == "READY",
                        _ITEMS.c.embedding.is_not(None),
                        _SETS.c.status == "READY",
                        _SETS.c.embedding_model == embedding_model,
                        _SETS.c.embedding_instruction_hash
                        == embedding_instruction_hash,
                    )
                    .order_by(
                        func.coalesce(_SETS.c.ready_at, _SETS.c.created_at).desc(),
                        _SETS.c.id.desc(),
                    )
                    .limit(1)
                )
            ).mappings().one_or_none()
        finally:
            await session.close()
        return dict(row) if row else None

    async def claim_pending_items(
        self,
        set_id: int,
        *,
        owner_token: str,
        limit: int = 32,
        lease_seconds: int = 120,
        max_attempts: int = 3,
        retry_base_seconds: int = 30,
    ) -> list[dict[str, Any]]:
        """回收过期租约，并用 SKIP LOCKED 原子认领待嵌入条目。"""
        if limit <= 0:
            return []

        max_attempts = max(1, max_attempts)
        retry_base = max(1, retry_base_seconds)
        exhausted = _ITEMS.c.attempt_count >= max_attempts
        retry_delay = _interval(1) * func.least(
            retry_base * func.power(
                2, func.greatest(_ITEMS.c.attempt_count - 1, 0)
            ),
            3600,
        )

        candidate_ids = (
            select(_ITEMS.c.id)
            .select_from(_ITEMS)
            .join(_SETS, _SETS.c.id == _ITEMS.c.set_id)
            .where(
                _ITEMS.c.set_id == set_id,
                _SETS.c.status == "BUILDING",
                _ITEMS.c.status == "PENDING",
                or_(
                    _ITEMS.c.next_retry_at.is_(None),
                    _ITEMS.c.next_retry_at <= func.now(),
                ),
            )
            .order_by(_ITEMS.c.order_index_snapshot, _ITEMS.c.id)
            .with_for_update(skip_locked=True, of=_ITEMS)
            .limit(limit)
            .scalar_subquery()
        )

        session = _open_session()
        try:
            async with session.begin():
                await session.execute(
                    update(_ITEMS)
                    .where(
                        _ITEMS.c.set_id == set_id,
                        _ITEMS.c.status == "PROCESSING",
                        _ITEMS.c.lease_expires_at <= func.now(),
                    )
                    .values(
                        status=case((exhausted, "FAILED"), else_="PENDING"),
                        error_message=case(
                            (exhausted, "embedding 处理租约超过最大重试次数"),
                            else_=_ITEMS.c.error_message,
                        ),
                        last_error_code="lease_expired",
                        next_retry_at=case(
                            (exhausted, null()),
                            else_=func.now() + retry_delay,
                        ),
                        lease_owner=null(),
                        lease_expires_at=null(),
                    )
                )
                rows = (
                    await session.execute(
                        update(_ITEMS)
                        .where(_ITEMS.c.id.in_(candidate_ids))
                        .values(
                            status="PROCESSING",
                            lease_owner=owner_token,
                            lease_expires_at=func.now()
                            + _interval(max(1, lease_seconds)),
                            next_retry_at=null(),
                            attempt_count=_ITEMS.c.attempt_count + 1,
                        )
                        .returning(*_ITEM_ALIASED_FULL)
                    )
                ).mappings().all()
        finally:
            await session.close()
        items = [dict(row) for row in rows]
        # UPDATE ... RETURNING 无顺序保证，按旧 SQL 的
        # ``ORDER BY order_index_snapshot ASC, id ASC`` 语义稳定重排
        items.sort(key=lambda item: (int(item["order_index"]), int(item["id"])))
        return items

    async def mark_item_ready(
        self,
        item_id: int,
        owner_token: str,
        embedding: list[float],
        embedding_dim: int,
    ) -> bool:
        """仅允许当前租约 owner 将条目标记为 READY。"""
        session = _open_session()
        try:
            async with session.begin():
                result = cast(
                    "CursorResult[Any]",
                    await session.execute(
                        update(_ITEMS)
                        .where(
                            _ITEMS.c.id == item_id,
                            _ITEMS.c.status == "PROCESSING",
                            _ITEMS.c.lease_owner == owner_token,
                        )
                        .values(
                            embedding=embedding,
                            embedding_dim=embedding_dim,
                            status="READY",
                            error_message=null(),
                            last_error_code=null(),
                            next_retry_at=null(),
                            lease_owner=null(),
                            lease_expires_at=null(),
                            embedded_at=func.now(),
                        )
                    ),
                )
        finally:
            await session.close()
        return int(result.rowcount or 0) > 0

    async def complete_item_failure(
        self,
        item_id: int,
        *,
        owner_token: str,
        error_code: str,
        error_message: str,
        max_attempts: int,
        retry_base_seconds: int,
    ) -> str:
        """按尝试次数将当前 owner 的失败条目退避重试或转为 FAILED。"""
        max_attempts = max(1, max_attempts)
        exhausted = _ITEMS.c.attempt_count >= max_attempts
        retry_delay = _interval(1) * func.least(
            max(1, retry_base_seconds)
            * func.power(2, func.greatest(_ITEMS.c.attempt_count - 1, 0)),
            3600,
        )
        session = _open_session()
        try:
            async with session.begin():
                status = (
                    await session.execute(
                        update(_ITEMS)
                        .where(
                            _ITEMS.c.id == item_id,
                            _ITEMS.c.status == "PROCESSING",
                            _ITEMS.c.lease_owner == owner_token,
                        )
                        .values(
                            status=case((exhausted, "FAILED"), else_="PENDING"),
                            error_message=error_message,
                            last_error_code=error_code,
                            next_retry_at=case(
                                (exhausted, null()),
                                else_=func.now() + retry_delay,
                            ),
                            lease_owner=null(),
                            lease_expires_at=null(),
                        )
                        .returning(_ITEMS.c.status)
                    )
                ).scalar_one_or_none()
        finally:
            await session.close()
        return str(status).lower() if status is not None else "stale"

    async def refresh_set_progress(self, set_id: int) -> dict[str, Any]:
        """锁住 set 后重算计数，并原子收敛 BUILDING 状态。"""
        session = _open_session()
        try:
            async with session.begin():
                set_row = (
                    await session.execute(
                        select(_SETS.c.id, _SETS.c.status)
                        .where(_SETS.c.id == set_id)
                        .with_for_update()
                    )
                ).mappings().one_or_none()
                if set_row is None:
                    msg = f"scene set 不存在: {set_id}"
                    raise ValueError(msg)

                counts = (
                    await session.execute(
                        select(
                            func.count().label("total"),
                            func.count()
                            .filter(_ITEMS.c.status == "READY")
                            .label("ready_count"),
                            func.count()
                            .filter(_ITEMS.c.status == "FAILED")
                            .label("failed_count"),
                        ).where(_ITEMS.c.set_id == set_id)
                    )
                ).mappings().one()
                total = int(counts["total"])
                ready = int(counts["ready_count"])
                failed = int(counts["failed_count"])
                previous_status = str(set_row["status"])
                next_status = previous_status
                if previous_status == "BUILDING" and total > 0 and ready + failed == total:
                    next_status = "FAILED" if failed > 0 else "READY"

                # 旧 SQL 的 ``CASE WHEN $5 = ...`` 分支基于本次写入的
                # next_status 值，Python 侧等价计算即可，避免向 SQL 传
                # 布尔字面量条件
                match next_status:
                    case "FAILED":
                        error_message_value: Any = "scene embedding 存在最终失败条目"
                        ready_at_value: Any = _SETS.c.ready_at
                    case "READY":
                        error_message_value = null()
                        ready_at_value = func.coalesce(_SETS.c.ready_at, func.now())
                    case _:
                        error_message_value = _SETS.c.error_message
                        ready_at_value = (
                            null() if next_status == "BUILDING" else _SETS.c.ready_at
                        )

                row = (
                    await session.execute(
                        update(_SETS)
                        .where(_SETS.c.id == set_id)
                        .values(
                            item_total=total,
                            item_ready=ready,
                            item_failed=failed,
                            status=next_status,
                            error_message=error_message_value,
                            ready_at=ready_at_value,
                        )
                        .returning(*_SET_COLUMNS)
                    )
                ).mappings().one_or_none()
                if row is None:
                    msg = f"scene set 进度刷新失败: {set_id}"
                    raise RuntimeError(msg)
        finally:
            await session.close()

        result = dict(row)
        result["previous_status"] = previous_status
        return result

    async def reopen_failed_set(self, set_id: int) -> int:
        """将 FAILED set 重置为 BUILDING，并将 FAILED item 置回 PENDING。"""
        session = _open_session()
        try:
            async with session.begin():
                set_row = (
                    await session.execute(
                        select(_SETS.c.status)
                        .where(_SETS.c.id == set_id)
                        .with_for_update()
                    )
                ).mappings().one_or_none()
                if set_row is None:
                    msg = f"scene set 不存在: {set_id}"
                    raise ValueError(msg)
                if str(set_row["status"]) != "FAILED":
                    msg = f"仅允许重试 FAILED set: id={set_id} status={set_row['status']}"
                    raise ValueError(msg)

                await session.execute(
                    update(_SETS)
                    .where(_SETS.c.id == set_id)
                    .values(
                        status="BUILDING",
                        error_message=null(),
                        item_failed=0,
                        ready_at=null(),
                    )
                )

                result = cast(
                    "CursorResult[Any]",
                    await session.execute(
                        update(_ITEMS)
                        .where(
                            _ITEMS.c.set_id == set_id,
                            _ITEMS.c.status == "FAILED",
                        )
                        .values(
                            status="PENDING",
                            error_message=null(),
                            last_error_code=null(),
                            attempt_count=0,
                            next_retry_at=func.now(),
                            lease_owner=null(),
                            lease_expires_at=null(),
                        )
                    ),
                )
                updated = int(result.rowcount or 0)
        finally:
            await session.close()

        logger.info(
            "[KomariDecision] 重试 scene set: id={} reset_failed_items={}",
            set_id,
            updated,
        )
        return updated

    async def delete_set(self, set_id: int) -> bool:
        """删除指定 set（级联删除 item）。"""
        session = _open_session()
        try:
            async with session.begin():
                result = cast(
                    "CursorResult[Any]",
                    await session.execute(
                        delete(_SETS).where(_SETS.c.id == set_id)
                    ),
                )
        finally:
            await session.close()
        affected = int(result.rowcount or 0)
        if affected > 0:
            logger.info("[KomariDecision] 删除 scene set: id={}", set_id)
            return True
        return False

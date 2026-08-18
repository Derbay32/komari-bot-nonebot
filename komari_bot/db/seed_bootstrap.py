"""版本化初始数据播种入口（TSK-189）。

统一、版本化且幂等的初始数据播种命令，供本地、容器 prestart 与 CI 复用：

    python -m komari_bot.db.seed_bootstrap [--seed-file PATH]

执行顺序与约束：

1. **seed 文件校验先于数据库访问**：默认读取
   ``komari_bot/db/initial_data/scenes.yaml``（可经 ``--seed-file`` 覆盖），
   校验 version、必需 fixed 场景、一般场景与 scene_key 唯一性，并在 YAML
   解析阶段拒绝 mapping 重复键（PyYAML 默认静默覆盖）；任何格式或约束不
   满足都以非零退出码结束并指明出错的 seed 文件，阻止冷启动。
2. **只插缺失、绝不覆盖**：按稳定 ``scene_key`` 执行
   ``INSERT ... ON CONFLICT DO NOTHING``，已有默认或管理员场景（含 disabled）
   保持原值，重复执行幂等。
3. **写入后验证最终数据库状态足以冷启动**：校验 PostgreSQL 中必需 fixed
   场景齐全且可用、至少存在一个启用的一般场景；不满足则命令失败，容器
   prestart 的 ``set -e`` 会随之中止应用启动（fail fast）。
4. **运行时从不读取本文件**：初始化数据只是 bootstrap seed，运行时的场景
   快照一律从 PostgreSQL 构建（见 ADR-0011）。

数据库访问统一走 nonebot-plugin-orm 共享连接边界
（``komari_bot.db.orm_connection.get_shared_orm_connection_pool``），
连接串唯一权威是 ``SQLALCHEMY_DATABASE_URL``。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from collections.abc import Hashable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from yaml.constructor import ConstructorError

from .orm_bootstrap import _bootstrap_nonebot

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_SEED_FILE = Path(__file__).resolve().parent / "initial_data" / "scenes.yaml"

#: 必需 fixed 场景键（seed 资产与数据库冷启动校验共用）。
_REQUIRED_FIXED_SCENE_KEYS: tuple[str, ...] = (
    "NOISE",
    "MEANINGFUL",
    "CALL_DIRECT",
    "CALL_MENTION",
)


#: 由 ``flatten_mapping`` 特殊消费、不构成真实 mapping 键的 YAML 标签。
_MAPPING_INTERNAL_TAGS: frozenset[str] = frozenset(
    {
        "tag:yaml.org,2002:merge",  # ``<<`` 合并键
        "tag:yaml.org,2002:value",  # ``=`` 键（构造时改写为 str）
    }
)


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """局部 SafeLoader：构造 mapping 时拒绝重复键。

    PyYAML 默认对 mapping 重复键静默覆盖（后出现的键胜出），校验依赖
    构造结果，必须在构造前独立检出；仅本模块使用，不修改全局 loader，
    正常资产（无重复键，含合法 merge 键）的行为与 ``yaml.safe_load``
    完全一致。
    """

    def construct_mapping(
        self,
        node: yaml.MappingNode,
        deep: bool = False,  # noqa: FBT001, FBT002  # 对齐 PyYAML 基类签名
    ) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _value_node in node.value:
            if key_node.tag in _MAPPING_INTERNAL_TAGS:
                # merge/value 键由 flatten_mapping 消费，不参与重复键比较
                continue
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, Hashable):
                # 非可哈希键由 PyYAML 下游按 unhashable key 拒绝，无需在此处理
                continue
            if key in seen:
                raise ConstructorError(  # noqa: TRY003  # 第三方异常类的消息参数
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


class SeedValidationError(RuntimeError):
    """初始数据文件校验失败。"""


class SeedVerificationError(RuntimeError):
    """播种后数据库最终状态不足以冷启动。"""


@dataclass(frozen=True)
class SceneSeedItem:
    """归一化后的待播种 scene 条目。"""

    scene_key: str
    scene_type: str  # fixed | general
    content_text: str
    content_hash: str
    enabled: bool
    order_index: int


@dataclass(frozen=True)
class SeedPayload:
    """校验通过的版本化初始数据载荷。"""

    version: str
    fixed_candidates: dict[str, str]
    general_scenes: list[dict[str, str]]
    items: list[SceneSeedItem]


@dataclass(frozen=True)
class SeedReport:
    """播种结果。"""

    inserted: int
    total: int
    fixed_count: int
    general_count: int


@dataclass(frozen=True)
class ColdStartState:
    """播种后数据库满足冷启动条件的状态统计。"""

    fixed_count: int
    general_count: int


def compute_text_hash(text: str) -> str:
    """计算场景内容哈希（与 SceneRepository 同一约定）。"""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _normalize_fixed_candidates(raw: object, path: Path) -> dict[str, str]:
    """标准化 fixed_candidates；空键或空内容一律视为非法。"""
    if not isinstance(raw, dict):
        msg = f"fixed_candidates 必须是对象: {path}"
        raise SeedValidationError(msg)
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        key_str = str(key).strip()
        value_str = str(value).strip()
        if not key_str:
            msg = f"固定场景键不能为空: {path}"
            raise SeedValidationError(msg)
        if not value_str:
            msg = f"固定场景内容不能为空: {key_str}（{path}）"
            raise SeedValidationError(msg)
        normalized[key_str] = value_str
    return normalized


def _normalize_general_scenes(raw: object, path: Path) -> list[dict[str, str]]:
    """标准化 general_scenes；条目缺 id/text 一律视为非法。"""
    if not isinstance(raw, list):
        msg = f"general_scenes 必须是数组: {path}"
        raise SeedValidationError(msg)
    normalized: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            msg = f"一般场景条目必须是对象: {path}"
            raise SeedValidationError(msg)
        scene_id = str(item.get("id") or "").strip()
        scene_text = str(item.get("text") or "").strip()
        if not scene_id:
            msg = f"一般场景缺少 id: {path}"
            raise SeedValidationError(msg)
        if not scene_text:
            msg = f"一般场景缺少 text: {scene_id}（{path}）"
            raise SeedValidationError(msg)
        normalized.append({"id": scene_id, "text": scene_text})
    if not normalized:
        msg = f"初始数据缺少一般场景（general_scenes 不能为空）: {path}"
        raise SeedValidationError(msg)
    return normalized


def _find_duplicate_scene_keys(
    fixed_candidates: dict[str, str],
    general_scenes: list[dict[str, str]],
) -> list[str]:
    """返回 fixed 与 general 之间、general 内部重复的 scene_key（稳定顺序）。"""
    seen: set[str] = set(fixed_candidates)
    duplicates: list[str] = []
    for scene in general_scenes:
        key = scene["id"]
        if key in seen:
            if key not in duplicates:
                duplicates.append(key)
        else:
            seen.add(key)
    return duplicates


def load_seed_file(path: Path) -> SeedPayload:
    """读取并校验版本化 seed 文件，返回归一化载荷。

    校验失败抛出 :class:`SeedValidationError`，消息始终指明出错的 seed 文件
    路径，并在缺键 / 重复键场景点名具体键。
    """
    path = path if path.is_absolute() else path.resolve()
    try:
        raw = yaml.load(
            path.read_text(encoding="utf-8"),
            Loader=_UniqueKeySafeLoader,
        ) or {}
    except OSError as exc:
        msg = f"读取初始数据文件失败: {path}（{exc}）"
        raise SeedValidationError(msg) from exc
    except yaml.YAMLError as exc:
        msg = f"初始数据 YAML 解析失败: {path}（{exc}）"
        raise SeedValidationError(msg) from exc

    if not isinstance(raw, dict):
        msg = f"初始数据根节点必须是对象: {path}"
        raise SeedValidationError(msg)

    version = str(raw.get("version") or "").strip()
    if not version:
        msg = f"初始数据缺少 version 标记: {path}"
        raise SeedValidationError(msg)

    fixed_candidates = _normalize_fixed_candidates(raw.get("fixed_candidates"), path)
    general_scenes = _normalize_general_scenes(raw.get("general_scenes"), path)

    missing = [
        key for key in _REQUIRED_FIXED_SCENE_KEYS if key not in fixed_candidates
    ]
    if missing:
        msg = (
            "初始数据缺少必需固定场景: "
            f"{', '.join(missing)}（{path}）"
        )
        raise SeedValidationError(msg)

    duplicates = _find_duplicate_scene_keys(fixed_candidates, general_scenes)
    if duplicates:
        msg = (
            "初始数据 scene_key 重复: "
            f"{', '.join(duplicates)}（{path}）"
        )
        raise SeedValidationError(msg)

    items: list[SceneSeedItem] = []
    order = 0
    for key, content in fixed_candidates.items():
        items.append(
            SceneSeedItem(
                scene_key=key,
                scene_type="fixed",
                content_text=content,
                content_hash=compute_text_hash(content),
                enabled=True,
                order_index=order,
            )
        )
        order += 1
    for scene in general_scenes:
        content = scene["text"]
        items.append(
            SceneSeedItem(
                scene_key=scene["id"],
                scene_type="general",
                content_text=content,
                content_hash=compute_text_hash(content),
                enabled=True,
                order_index=order,
            )
        )
        order += 1

    return SeedPayload(
        version=version,
        fixed_candidates=fixed_candidates,
        general_scenes=general_scenes,
        items=items,
    )


def verify_cold_start_state(rows: list[dict[str, Any]]) -> ColdStartState:
    """校验数据库最终状态足以冷启动。

    必需 fixed 场景必须全部存在、类型为 fixed、启用且内容非空；数据库至少
    存在一个启用且内容非空的一般场景。不满足则抛出
    :class:`SeedVerificationError`。
    """
    by_key = {str(row["scene_key"]): row for row in rows}
    missing = [key for key in _REQUIRED_FIXED_SCENE_KEYS if key not in by_key]
    unavailable: list[str] = []
    for key in _REQUIRED_FIXED_SCENE_KEYS:
        row = by_key.get(key)
        if row is None:
            continue
        valid = (
            str(row["scene_type"]) == "fixed"
            and bool(row["enabled"])
            and bool(str(row["content_text"] or "").strip())
        )
        if not valid:
            unavailable.append(key)

    general_enabled = 0
    for row in rows:
        if (
            str(row["scene_type"]) == "general"
            and bool(row["enabled"])
            and bool(str(row["content_text"] or "").strip())
        ):
            general_enabled += 1

    problems: list[str] = []
    if missing:
        problems.append(f"缺少必需固定场景: {', '.join(missing)}")
    if unavailable:
        problems.append(f"必需固定场景不可用: {', '.join(unavailable)}")
    if general_enabled <= 0:
        problems.append("至少需要一个启用的一般场景")
    if problems:
        msg = "；".join(problems)
        raise SeedVerificationError(msg)

    return ColdStartState(
        fixed_count=sum(
            1 for row in rows if str(row["scene_type"]) == "fixed"
        ),
        general_count=general_enabled,
    )


async def _seed_database(payload: SeedPayload) -> SeedReport:
    """写入缺失场景并在写入后校验冷启动充分性。

    数据库访问经 nonebot-plugin-orm 共享连接边界（shared engine pool）：
    只借还连接，不捕获也不 dispose 共享引擎（生命周期由 nonebot-plugin-orm
    托管）；查询为既有场景表的只读与幂等插入，不使用任何运行时 DDL。
    """
    from komari_bot.db.orm_connection import get_shared_orm_connection_pool

    pool = get_shared_orm_connection_pool()
    inserted = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for item in payload.items:
                inserted_id = await conn.fetchval(
                    """
                    INSERT INTO komari_decision_scenes
                        (scene_key, scene_type, content_text, content_hash,
                         enabled, order_index)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (scene_key) DO NOTHING
                    RETURNING id
                    """,
                    item.scene_key,
                    item.scene_type,
                    item.content_text,
                    item.content_hash,
                    item.enabled,
                    item.order_index,
                )
                if inserted_id is not None:
                    inserted += 1
        rows = await conn.fetch(
            """
            SELECT scene_key, scene_type, content_text, enabled
            FROM komari_decision_scenes
            """
        )

    state = verify_cold_start_state([dict(row) for row in rows])
    return SeedReport(
        inserted=inserted,
        total=len(rows),
        fixed_count=state.fixed_count,
        general_count=state.general_count,
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m komari_bot.db.seed_bootstrap",
        description=(
            "版本化初始数据播种：校验 seed 文件后按 scene_key 只补插缺失场景，"
            "并在写入后验证数据库最终状态足以冷启动。"
        ),
    )
    parser.add_argument(
        "--seed-file",
        type=Path,
        default=DEFAULT_SEED_FILE,
        help=f"版本化初始数据文件路径（默认: {DEFAULT_SEED_FILE}）",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """播种命令入口。

    顺序严格为：seed 文件校验 → 数据库播种 → 写入后冷启动校验；
    任一步失败均以非零退出码结束，配合 prestart 的 ``set -e`` 阻止冷启动。
    """
    args = _parse_args(argv)

    try:
        payload = load_seed_file(args.seed_file)
    except SeedValidationError as exc:
        print(f"初始数据校验失败: {exc}", file=sys.stderr)  # noqa: T201
        raise SystemExit(1) from exc

    # 校验通过后才初始化 NoneBot / 共享引擎，保证非法 seed 绝不触碰数据库。
    try:
        _bootstrap_nonebot()
    except Exception as exc:
        print(f"初始化失败: {exc}", file=sys.stderr)  # noqa: T201
        raise SystemExit(1) from exc

    try:
        report = asyncio.run(_seed_database(payload))
    except SeedVerificationError as exc:
        print(  # noqa: T201
            f"播种后数据库校验失败（seed 文件: {args.seed_file}）: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    except Exception as exc:
        print(f"数据库播种失败: {exc}", file=sys.stderr)  # noqa: T201
        raise SystemExit(1) from exc

    print(  # noqa: T201
        f"[seed_bootstrap] 场景初始数据播种完成: 新增 {report.inserted} 条，"
        f"共 {report.total} 条（fixed={report.fixed_count} "
        f"general={report.general_count}）"
    )


if __name__ == "__main__":
    main()

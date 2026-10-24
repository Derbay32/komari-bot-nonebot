"""版本化 Prompt 初始数据播种入口（TSK-190/191；场景初始化已退役）。

统一、版本化且幂等的初始数据播种命令，供本地、容器 prestart 与 CI 复用：

    python -m komari_bot.db.seed_bootstrap [--seed-file PATH]
        [--decision-seed-file PATH]

必需 manifest（``--seed-file``，默认
``komari_bot/db/initial_data/scenes.yaml``）只携带三个必需 Prompt 资源
（chat / memory summary / group history summary）的初始数据；
``--decision-seed-file``（默认
``komari_bot/db/initial_data/decision_prompt.yaml``）是**可选**的判定 Prompt
初始数据资产：它独立解析与校验，避免可选资产的格式错误影响必需资源的校验；
必需资源提交并通过校验后，才以自己的独立连接租约与独立事务写入判定 Prompt。
可选资产的读取/校验/取用/写入/归还失败只输出一条固定安全诊断（不含正文、
未知字段值、凭据、原始异常或回溯），不阻止角色服务启动，也绝不把运行时健康
状态判成失败（ADR-0011《判定 Prompt 的隔离初始化》）。

TSK-190 起判定场景初始化退役（依据 TSK-180 与 TSK-159 最终决议）：播种
不读取、不写入且绝不重建旧场景存储；旧 manifest 若残留场景键，按无关顶层
键忽略，校验层不因此新增拒绝规则。

执行顺序与约束：

1. **seed 文件校验先于数据库访问**：默认读取
   ``komari_bot/db/initial_data/scenes.yaml``（可经 ``--seed-file`` 覆盖），
   校验 version，并在 YAML 解析阶段拒绝 mapping 重复键（PyYAML 默认静默
   覆盖）；同时校验三个必需 Prompt 资源的初始数据块：字段集合与各自强类型
   Schema 一一对应、全部非空字符串且共享内容预算通过，缺块/缺字段/违规在
   数据库访问前即失败，任何格式或约束不满足都以非零退出码结束并指明出错的
   seed 文件。
2. **只插缺失、绝不覆盖**：三个 Prompt 资源只新建缺失单行（id=1）并补齐空
   字段（``None``、空字符串与纯空白字符串均视为空），非空自定义值绝不覆盖；
   重复执行幂等（TSK-190/191 验收标准 6）。
3. **写入后验证最终数据库状态足以冷启动**：校验 PostgreSQL 中三个 Prompt
   资源单行全部字段非空；不满足则命令失败，容器 prestart 的 ``set -e`` 会
   随之中止应用启动（fail fast）。
4. **运行时从不读取本文件**：初始化数据只是 bootstrap seed，运行时的 Prompt
   快照一律从 PostgreSQL 构建（见 ADR-0011）。
5. **可选判定 Prompt 的隔离初始化**（TSK-182）：独立的
   ``decision_prompt.yaml`` 只校验自己的严格固定形状，只在必需资源已提交并
   通过校验之后、以自己的独立连接租约与独立事务写入判定 Prompt；只新建
   缺失单行或补齐空字段，保留非空管理员文案。可选步骤的读取/校验/取用/
   写入/归还失败都只输出一条固定安全诊断，不影响必需资源，也不阻断容器
   启动；该诊断只陈述“未能确认完成”，不声称写入已回滚或运行时健康失败
   （提交之后才发生的归还失败不能撤销已经持久化的行）。运行时仍只读
   PostgreSQL（没有文件回退、Python 长文本默认值或模型探测）。

注：必需/可选资产共用的 YAML 读取函数仍以既有 legacy 载荷报错（可能
带有 PyYAML/OSError 的原始消息），只有可选判定资产的 CLI 入口边界会把
它们归约为固定安全诊断。

数据库访问统一走 nonebot-plugin-orm 共享连接边界
（``komari_bot.db.orm_connection.get_shared_orm_connection_pool``），
连接串唯一权威是 ``SQLALCHEMY_DATABASE_URL``。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Hashable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from yaml.constructor import ConstructorError

from komari_bot.config.typed_config import ensure_typed_prompt_model
from komari_bot.llm.content_budget import (
    CONTENT_TEXT_BUDGET,
    validate_text_budget,
)

from .orm_bootstrap import _bootstrap_nonebot

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_SEED_FILE = Path(__file__).resolve().parent / "initial_data" / "scenes.yaml"

#: TSK-182：判定 Prompt 的独立可选初始数据资产（与必需 manifest 隔离）。
DEFAULT_DECISION_SEED_FILE = (
    Path(__file__).resolve().parent / "initial_data" / "decision_prompt.yaml"
)

#: Prompt 强类型表继承自 TypedConfigModel 的存储专用字段。
_PROMPT_STORAGE_FIELDS: frozenset[str] = frozenset(
    {"id", "revision", "updated_at"}
)

#: TSK-191：seed 资产必须为三个 Prompt 资源全部提供完整初始数据块。
_PROMPT_RESOURCE_IDS: tuple[str, ...] = (
    "komari_chat",
    "komari_memory_summary",
    "group_history_summary",
)

#: TSK-182：可选的判定 Prompt 资源（独立资产、独立事务、非强制冷启动 gate）。
_DECISION_PROMPT_RESOURCE_ID = "komari_decision"

#: TSK-182：可选判定资产中的固定节键（只读显式键，不递归搜索可选块）。
_DECISION_PROMPT_SECTION_KEY = "decision_prompt"

#: TSK-182：可选判定资产允许的顶层键集合（严格固定形状，不多不少）。
_DECISION_SEED_ROOT_KEYS: frozenset[str] = frozenset(
    {"version", _DECISION_PROMPT_SECTION_KEY}
)

#: TSK-182：可选判定资产不可用或未能确认完成时唯一的固定安全诊断。
#:
#: 只陈述“未能确认完成”，绝不包含资产正文、未知字段值、路径细节、原始
#: 异常或回溯，也不据此宣称写入已回滚或运行时健康状态失败（事务提交之后
#: 才发生的连接归还失败不能撤销已经持久化的行）。
_DECISION_SEED_UNAVAILABLE_MESSAGE = "判定 Prompt 初始化未能确认完成。"


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
class SeedPayload:
    """校验通过的版本化初始数据载荷。"""

    version: str
    #: 三个 Prompt 资源（chat / memory summary / group history summary）
    #: 的完整初始值（TSK-191）；文件缺块时键缺失，由 CLI 层按冷启动
    #: 要求拒绝。
    prompts: dict[str, dict[str, str]]


@dataclass(frozen=True)
class SeedReport:
    """播种结果：三个必需 Prompt 资源新建行 / 补齐字段数。"""

    prompt_rows_inserted: int
    prompt_fields_filled: int


def _find_prompt_mapping(raw: object, resource_id: str) -> dict[str, Any] | None:
    """在 seed 文档中定位指定 Prompt 资源的初始数据块（与测试 oracle 一致）。

    判别键：komari_chat = 包含 ``system_prompt`` 且不含
    ``planning_system_prompt``；group_history_summary = 包含
    ``planning_system_prompt``；komari_memory_summary = 包含
    ``memory_summary_common_system``。遍历不修改原文档，找不到返回 ``None``。
    """
    if resource_id == "komari_chat":

        def predicate(node: dict[str, Any]) -> bool:
            return (
                "system_prompt" in node
                and "planning_system_prompt" not in node
            )

    elif resource_id == "group_history_summary":

        def predicate(node: dict[str, Any]) -> bool:
            return "planning_system_prompt" in node

    elif resource_id == "komari_memory_summary":

        def predicate(node: dict[str, Any]) -> bool:
            return "memory_summary_common_system" in node

    else:
        msg = f"未知 Prompt 资源: {resource_id}"
        raise SeedValidationError(msg)

    def walk(node: object) -> dict[str, Any] | None:
        if isinstance(node, dict):
            if predicate(node):
                return node
            for value in node.values():
                found = walk(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = walk(value)
                if found is not None:
                    return found
        return None

    return walk(raw)


def _prompt_model(resource_id: str) -> Any:
    """返回 Prompt 强类型表模型（校验/写入共用真源，可离线加载）。"""
    model_cls = ensure_typed_prompt_model(resource_id)
    if model_cls is None:
        msg = f"无法加载 Prompt 强类型 Schema（{resource_id}.prompt_schema）"
        raise SeedValidationError(msg)
    return model_cls


def _prompt_schema_fields(resource_id: str) -> set[str]:
    """指定 Prompt 资源强类型 Schema 的正文字段集（不含存储专用字段）。"""
    return set(_prompt_model(resource_id).model_fields) - _PROMPT_STORAGE_FIELDS


def _prompt_table_name(resource_id: str) -> str:
    """指定 Prompt 资源对应的强类型表名（来自 Schema，不硬编码）。"""
    return str(_prompt_model(resource_id).__tablename__)


def _normalize_prompt(
    raw: object,
    resource_id: str,
    path: Path,
) -> dict[str, str]:
    """校验并归一化指定 Prompt 资源的初始值；缺失块返回空 dict（CLI 裁决）。

    字段集合必须与 resource_id 对应强类型 Schema 一一对应，全部字段必须是
    非空字符串并通过共享内容预算；跨资源/未知字段自然被未知字段校验拒绝。
    错误消息始终指明 seed 文件路径与资源。
    """
    mapping = _find_prompt_mapping(raw, resource_id)
    if mapping is None:
        return {}
    return _validate_prompt_values(mapping, resource_id, path)


def _validate_prompt_values(
    mapping: object,
    resource_id: str,
    path: Path,
) -> dict[str, str]:
    """按 resource_id 的强类型 Schema 与共享内容预算校验一个 Prompt 初始块。

    必需资源与可选判定资产共用同一校验：字段集合一一对应、全部非空字符串、
    共享内容预算通过；错误消息只含资源名、字段名与文件路径。
    """
    if not isinstance(mapping, dict):
        msg = f"{resource_id} Prompt 初始数据必须是对象: {path}"
        raise SeedValidationError(msg)

    fields = _prompt_schema_fields(resource_id)
    provided = {key for key in mapping if isinstance(key, str)}
    missing = sorted(fields - provided)
    if missing:
        msg = (
            f"{resource_id} Prompt 初始数据缺少字段: "
            f"{', '.join(missing)}（{path}）"
        )
        raise SeedValidationError(msg)
    extra = sorted(str(key) for key in set(mapping) - fields)
    if extra:
        msg = (
            f"{resource_id} Prompt 初始数据包含未知字段: "
            f"{', '.join(extra)}（{path}）"
        )
        raise SeedValidationError(msg)

    normalized: dict[str, str] = {}
    for field in sorted(fields):
        value = mapping[field]
        if not isinstance(value, str) or not value.strip():
            msg = (
                f"{resource_id} Prompt 字段 {field} 的初始值必须是非空字符串"
                f"（{path}）"
            )
            raise SeedValidationError(msg)
        try:
            validate_text_budget(
                value,
                label=f"{resource_id} Prompt 初始数据字段 {field}",
                budget=CONTENT_TEXT_BUDGET,
            )
        except ValueError as exc:
            msg = f"{exc}（{path}）"
            raise SeedValidationError(msg) from exc
        normalized[field] = value
    return normalized


def load_decision_prompt_seed(path: Path) -> dict[str, str]:
    """读取并校验独立的可选判定 Prompt 初始数据资产。

    固定形状为 ``{version: 非空字符串, decision_prompt: {四个正文字段}}``，
    并且是**严格**的：只读显式 ``decision_prompt`` 键（不递归搜索任意嵌套
    可选块）；``version`` 必须是非空字符串（整数/布尔/序列/映射一律拒绝，
    绝不做 ``str()`` 强转）；顶层键集合恰为 ``{version, decision_prompt}``；
    字段集合与 ``komari_decision`` 强类型 Schema 一一对应，全部非空字符串
    并通过共享内容预算。

    失败抛出 :class:`SeedValidationError`。解析器错误可能包含资产片段，
    CLI 层只输出固定安全诊断，不展示原始错误。
    """
    path = path if path.is_absolute() else path.resolve()
    raw = _load_yaml_document(path)

    version = raw.get("version")
    if not isinstance(version, str) or not version.strip():
        msg = f"判定 Prompt 初始数据的 version 必须是非空字符串: {path}"
        raise SeedValidationError(msg)

    section = raw.get(_DECISION_PROMPT_SECTION_KEY)
    if section is None:
        msg = (
            "判定 Prompt 初始数据缺少 "
            f"{_DECISION_PROMPT_SECTION_KEY} 块: {path}"
        )
        raise SeedValidationError(msg)
    if not isinstance(section, dict):
        msg = (
            "判定 Prompt 初始数据的 "
            f"{_DECISION_PROMPT_SECTION_KEY} 必须是对象: {path}"
        )
        raise SeedValidationError(msg)

    unexpected_root = sorted(
        str(key) for key in set(raw) - _DECISION_SEED_ROOT_KEYS
    )
    if unexpected_root:
        msg = (
            "判定 Prompt 初始数据包含未知顶层键: "
            f"{', '.join(unexpected_root)}（{path}）"
        )
        raise SeedValidationError(msg)

    return _validate_prompt_values(section, _DECISION_PROMPT_RESOURCE_ID, path)


def _load_yaml_document(path: Path) -> dict[str, Any]:
    """读取 YAML 文档并拒绝 mapping 重复键（必需与可选资产共用同一严格解析）。

    失败一律归约为 :class:`SeedValidationError`：读取失败（含非法 UTF-8）、
    YAML 解析失败（含重复键）、根节点非对象。消息始终指明出错文件。

    注意：这里的失败消息是**既有 legacy 载荷**，会带上 ``OSError`` 或 PyYAML
    的原始消息（后者可能内含出错文档片段），本函数不做脱敏；只有可选判定
    资产的 CLI 入口边界会把它们归约为固定安全诊断。
    """
    try:
        raw = yaml.load(
            path.read_text(encoding="utf-8"),
            Loader=_UniqueKeySafeLoader,
        ) or {}
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"读取初始数据文件失败: {path}（{exc}）"
        raise SeedValidationError(msg) from exc
    except yaml.YAMLError as exc:
        msg = f"初始数据 YAML 解析失败: {path}（{exc}）"
        raise SeedValidationError(msg) from exc

    if not isinstance(raw, dict):
        msg = f"初始数据根节点必须是对象: {path}"
        raise SeedValidationError(msg)
    return raw


def load_seed_file(path: Path) -> SeedPayload:
    """读取并校验版本化 seed 文件，返回归一化 Prompt 载荷。

    只校验 version 与三个必需 Prompt 资源的初始数据块；旧场景键属于无关
    顶层键，既不校验也不据此新增拒绝规则。校验失败抛出
    :class:`SeedValidationError`，消息始终指明出错的 seed 文件路径，并在
    缺键 / 重复键情形点名具体键。
    """
    path = path if path.is_absolute() else path.resolve()
    raw = _load_yaml_document(path)

    version = str(raw.get("version") or "").strip()
    if not version:
        msg = f"初始数据缺少 version 标记: {path}"
        raise SeedValidationError(msg)

    prompts: dict[str, dict[str, str]] = {}
    for resource_id in _PROMPT_RESOURCE_IDS:
        normalized = _normalize_prompt(raw, resource_id, path)
        if normalized:
            prompts[resource_id] = normalized

    return SeedPayload(version=version, prompts=prompts)


async def _seed_prompt_row(
    conn: Any,
    resource_id: str,
    prompt_values: dict[str, str],
) -> tuple[int, int]:
    """新建缺失 Prompt 单行并只补齐空字段（TSK-190/191 AC6）。

    表名与字段集都来自 resource_id 对应强类型 Schema；空字段定义为
    ``None``、空字符串或纯空白字符串；非空自定义值绝不覆盖。返回
    ``(新建行数, 补齐字段数)``：已存在且全字段非空时零写入；只有实际
    补齐才递增 revision/updated_at，重复执行幂等。
    """
    table = _prompt_table_name(resource_id)
    fields = sorted(prompt_values)
    row = await conn.fetchrow(f"SELECT * FROM {table} WHERE id = 1")
    if row is None:
        columns = ", ".join(fields)
        placeholders = ", ".join(f"${index}" for index in range(2, 2 + len(fields)))
        await conn.execute(
            f"INSERT INTO {table}"
            " (id, revision, updated_at, "
            + columns
            + ") VALUES (1, 1, $1, "
            + placeholders
            + ")",
            datetime.now(UTC),
            *(prompt_values[field] for field in fields),
        )
        return 1, 0

    updates = {
        field: prompt_values[field]
        for field in fields
        if row[field] is None or not str(row[field]).strip()
    }
    if not updates:
        return 0, 0
    ordered = sorted(updates)
    set_clause = ", ".join(
        f'"{field}" = ${index}' for index, field in enumerate(ordered, start=2)
    )
    await conn.execute(
        f"UPDATE {table} SET "
        + set_clause
        + ", revision = revision + 1, updated_at = $1 WHERE id = 1",
        datetime.now(UTC),
        *(updates[field] for field in ordered),
    )
    return 0, len(updates)


async def _verify_prompt_cold_start(conn: Any, resource_id: str) -> None:
    """写入后验证数据库 Prompt 单行全字段非空（统一冷启动 gate 的一部分）。"""
    fields = _prompt_schema_fields(resource_id)
    table = _prompt_table_name(resource_id)
    row = await conn.fetchrow(f"SELECT * FROM {table} WHERE id = 1")
    if row is None:
        msg = f"{resource_id} Prompt 初始数据未写入数据库（{table} 缺行）"
        raise SeedVerificationError(msg)
    empty = [
        field
        for field in sorted(fields)
        if row[field] is None or not str(row[field]).strip()
    ]
    if empty:
        msg = f"{resource_id} Prompt 初始数据字段为空: {', '.join(empty)}"
        raise SeedVerificationError(msg)


async def _seed_optional_decision_prompt(
    pool: Any,
    prompt_values: dict[str, str],
) -> tuple[int, int] | None:
    """在独立连接租约与独立事务中初始化可选判定 Prompt。

    连接从事务外的共享 pool **重新借出**（不是复用已经归还的驱动连接），
    因此可选阶段的取用、写入、写入后校验与归还失败全部落在同一个可选
    错误边界内：必需资源早已提交并通过校验，可选阶段的任何失败都不得
    把必需初始化变成失败。

    只重用既有 helper 的新建/补齐空字段与写入后校验语义。诊断只有固定
    文案（不含正文、未知字段值、原始异常或回溯），也不据此宣称写入回滚
    或运行时健康失败：事务提交之后才发生的归还失败不能撤销已持久化的
    行。只有整个可选作用域成功完成才返回计数。
    """
    counts: tuple[int, int] | None = None
    try:
        async with pool.acquire() as conn, conn.transaction():
            counts = await _seed_prompt_row(
                conn,
                _DECISION_PROMPT_RESOURCE_ID,
                prompt_values,
            )
            await _verify_prompt_cold_start(
                conn,
                _DECISION_PROMPT_RESOURCE_ID,
            )
    except Exception:
        # 可选资源隔离：这里不捕获 BaseException/CancelledError，取消与
        # 进程级信号不得被当作“可选资产不可用”的正常分支。
        print(_DECISION_SEED_UNAVAILABLE_MESSAGE, file=sys.stderr)  # noqa: T201
        return None
    return counts


async def _seed_database(
    payload: SeedPayload,
    *,
    decision_prompt: dict[str, str] | None = None,
) -> SeedReport:
    """写入缺失 Prompt 初始数据，并在写入后校验冷启动充分性。

    数据库访问经 nonebot-plugin-orm 共享连接边界（shared engine pool）：
    只借还连接，不捕获也不 dispose 共享引擎（生命周期由 nonebot-plugin-orm
    托管）；查询为既有 Prompt 表的只读与幂等写入，不使用任何运行时 DDL，
    也不读写已退役的场景存储。

    ``decision_prompt`` 为 TSK-182 的可选判定 Prompt 正文：``None`` 明确
    表示可选资产未提供或不可用（运行时没有回退正文），不表示已写入任何
    默认值。提供的正文只在必需资源已提交并通过校验后，以自己的独立连接
    租约与独立事务写入与校验（取用/写入/校验/归还失败只落入可选错误边界）。
    """
    from komari_bot.db.orm_connection import get_shared_orm_connection_pool

    pool = get_shared_orm_connection_pool()
    prompt_rows_inserted = 0
    prompt_fields_filled = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for resource_id, prompt_values in payload.prompts.items():
                rows_added, fields_filled = await _seed_prompt_row(
                    conn,
                    resource_id,
                    prompt_values,
                )
                prompt_rows_inserted += rows_added
                prompt_fields_filled += fields_filled
        for resource_id in payload.prompts:
            await _verify_prompt_cold_start(conn, resource_id)
    # 必需租约到这里完整结束：取用、写入、提交、写入后校验与归还同属
    # 必需资源的错误边界（其中的失败仍然判主失败）。

    if decision_prompt is not None:
        # TSK-182：必需资源已提交且通过校验，才从同一个共享 pool 借出**独立**
        # 的第二个租约写可选判定 Prompt；报告的 Prompt 计数只统计成功完成
        # 的可选新建/补齐，失败/跳过不增加计数。
        seeded = await _seed_optional_decision_prompt(pool, decision_prompt)
        if seeded is not None:
            prompt_rows_inserted += seeded[0]
            prompt_fields_filled += seeded[1]

    return SeedReport(
        prompt_rows_inserted=prompt_rows_inserted,
        prompt_fields_filled=prompt_fields_filled,
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m komari_bot.db.seed_bootstrap",
        description=(
            "版本化初始数据播种：校验 seed 文件后只为三个必需 Prompt 资源"
            "补插缺失单行或补齐空字段，并在写入后验证数据库最终状态足以冷启动。"
        ),
    )
    parser.add_argument(
        "--seed-file",
        type=Path,
        default=DEFAULT_SEED_FILE,
        help=f"版本化 Prompt 初始数据文件路径（默认: {DEFAULT_SEED_FILE}）",
    )
    parser.add_argument(
        "--decision-seed-file",
        type=Path,
        default=DEFAULT_DECISION_SEED_FILE,
        help=(
            "判定 Prompt 初始数据文件路径（可选资源；"
            f"默认: {DEFAULT_DECISION_SEED_FILE}）"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """播种命令入口。

    顺序严格为：必需 seed 文件校验（含缺块门禁）→ 可选判定资产独立校验
    → 数据库播种 → 写入后冷启动校验。必需步骤失败以非零退出码结束，
    配合 prestart 的 ``set -e`` 阻止冷启动；可选判定资产不可用或未能
    确认完成（含可选租约的取用/归还失败）时只输出一条固定安全诊断并以
    ``decision_prompt=None`` 继续。
    """
    args = _parse_args(argv)

    try:
        payload = load_seed_file(args.seed_file)
    except SeedValidationError as exc:
        print(f"初始数据校验失败: {exc}", file=sys.stderr)  # noqa: T201
        raise SystemExit(1) from exc

    missing_prompts = [
        resource_id
        for resource_id in _PROMPT_RESOURCE_IDS
        if resource_id not in payload.prompts
    ]
    if missing_prompts:
        # TSK-191：任一 Prompt 资源缺块视同校验失败，必须在数据库访问前
        # 中止（统一冷启动 gate；错误指明出错的 seed 文件与资源）。
        print(  # noqa: T201
            "初始数据校验失败: 缺少 Prompt 初始数据块: "
            f"{', '.join(missing_prompts)}（{args.seed_file}）",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # TSK-182：可选判定资产独立读取/校验。失败只输出固定安全诊断（不含
    # 资产正文、未知字段值、路径细节、原始异常或回溯），也不据此宣称
    # 写入回滚或运行时健康失败；以 None 继续表示“未提供可选初始值”，运行时
    # 没有回退正文，仍只读 PostgreSQL。
    decision_prompt: dict[str, str] | None
    try:
        decision_prompt = load_decision_prompt_seed(args.decision_seed_file)
    except SeedValidationError:
        print(_DECISION_SEED_UNAVAILABLE_MESSAGE, file=sys.stderr)  # noqa: T201
        decision_prompt = None

    # 必需校验通过后才初始化 NoneBot / 共享引擎，保证非法 seed 绝不触碰数据库。
    try:
        _bootstrap_nonebot()
    except Exception as exc:
        print(f"初始化失败: {exc}", file=sys.stderr)  # noqa: T201
        raise SystemExit(1) from exc

    try:
        report = asyncio.run(
            _seed_database(payload, decision_prompt=decision_prompt)
        )
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
        f"[seed_bootstrap] Prompt 初始数据播种完成: "
        f"新建 {report.prompt_rows_inserted} 行，"
        f"补齐 {report.prompt_fields_filled} 个字段"
    )


if __name__ == "__main__":
    main()

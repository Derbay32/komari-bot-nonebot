"""group admission 强类型单行准入表展开（ADR-0012 锚点①）

迁移 ID: 0010
父迁移: 0009
创建时间: 2026-08-22 00:00:00

ADR-0012 第一阶段 expand：建立统一群聊准入的强类型单行存储
``komari_group_admission_config``（结构真源为
``group_admission/config_schema.py`` 的 ``GroupAdmissionConfigSchema``，
列结构与 ``migrations/env.py`` 合并的 SQLModel 元数据零漂移）：

- 单行表主键 ``id`` 恒为 1；
- ``revision``：跨进程 CAS 修订号（写操作原子自增）；
- ``updated_at``：最后写入时间（带时区）；
- ``policy`` JSONB NOT NULL：不可变策略修订快照的持久存储。

TSK-232 轮 B 扩展：另建三张 migration-only 账本表（不进 ORM metadata，
由 0013 coordinated contract 收尾清理——存量非 fresh 库物理删除；fresh
库保留空表供审计底册）：

- ``komari_group_admission_gate``：cutover 相位门单行表（phase 只准前进
  的 CAS 状态机、策略指纹/证据摘要/认证摘要与 fresh 标记）；
- ``komari_reply_reservation_cleanup_ledger``：回复预占身份释放底册；
- ``komari_admission_quarantine_ledger``：legacy Redis 键隔离底册。

全新空库（fresh）在 upgrade 时以 ON CONFLICT DO NOTHING 幂等播种缺省策略
``{"mode": "blacklist", "group_ids": []}`` 并插入 gate 行
``(id=1, phase='EXPANDED', is_fresh=true)``；非 fresh（已带 pre-admission
schema、无策略数据）的库不得被静默当作 fresh 放行——gate 行以
``is_fresh=false`` 落地，统一策略必须由 operator 显式提交并经 0012
backfill barrier 检验后收敛到 0013 cutover。
禁止 alias / 双读 / 双写 / fallback，不读取或合并任何旧 JSONB 白名单。

本 revision 自包含，不导入 ``komari_bot`` 运行时代码。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "komari_group_admission_config"

_GATE_TABLE = "komari_group_admission_gate"
_CLEANUP_LEDGER_TABLE = "komari_reply_reservation_cleanup_ledger"
_QUARANTINE_LEDGER_TABLE = "komari_admission_quarantine_ledger"

_CREATE_TABLE_SQL = """
CREATE TABLE komari_group_admission_config (
    id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    policy JSONB NOT NULL,
    PRIMARY KEY (id)
)
"""

_CREATE_GATE_SQL = """
CREATE TABLE komari_group_admission_gate (
    id INTEGER NOT NULL,
    phase TEXT NOT NULL,
    is_fresh BOOLEAN NOT NULL,
    policy_revision INTEGER,
    policy_fingerprint TEXT,
    backup_checkpoint TEXT,
    redis_evidence_digest TEXT,
    redis_finalizer_digest TEXT,
    phase_updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id)
)
"""

_CREATE_CLEANUP_LEDGER_SQL = """
CREATE TABLE komari_reply_reservation_cleanup_ledger (
    reservation_id TEXT NOT NULL,
    group_id BIGINT NOT NULL,
    release_finished_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (reservation_id)
)
"""

_CREATE_QUARANTINE_LEDGER_SQL = """
CREATE TABLE komari_admission_quarantine_ledger (
    entry_id BIGSERIAL PRIMARY KEY,
    key_family TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
)
"""

#: fresh 空库缺省策略（blacklist + 空群名单）。
_DEFAULT_POLICY = '{"mode": "blacklist", "group_ids": []}'


#: 会话级 fresh 安装标记（由 0001 fresh_marker 在同一次 upgrade 的同一
#: 连接上设置；分步升级/既有库续升的新会话不携带，见 0001 注释）。
_FRESH_GUC = "komari_tsk232.fresh_install"


def upgrade(name: str = "") -> None:
    if name:
        return

    op.execute(_CREATE_TABLE_SQL)
    op.execute(_CREATE_GATE_SQL)
    op.execute(_CREATE_CLEANUP_LEDGER_SQL)
    op.execute(_CREATE_QUARANTINE_LEDGER_SQL)

    # 仅 fresh 全新安装（同一次 upgrade 运行、0001 已设会话标记）幂等播种
    # 缺省策略；分步升级或既有库续升不得静默播种——统一策略必须由 operator
    # 显式提交并经 0012 backfill barrier 检验（缺策略时 0012 拒绝升级）。
    # gate 行无论 fresh 与否都幂等落单行：fresh=true 直通相位机起点，
    # 非 fresh 以 is_fresh=false 进入存量 cutover 流程。
    fresh_install = op.get_bind().execute(
        text(f"SELECT current_setting('{_FRESH_GUC}', true)")
    ).scalar_one_or_none()
    if fresh_install == "1":
        op.get_bind().execute(
            text(
                f"INSERT INTO {_TABLE} (id, revision, updated_at, policy) "
                "VALUES (1, 1, NOW(), CAST(:policy AS JSONB)) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"policy": _DEFAULT_POLICY},
        )
    op.get_bind().execute(
        text(
            f"INSERT INTO {_GATE_TABLE} (id, phase, is_fresh) "
            "VALUES (1, 'EXPANDED', :is_fresh) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"is_fresh": fresh_install == "1"},
    )


def downgrade(name: str = "") -> None:
    if name:
        return
    op.execute(f"DROP TABLE IF EXISTS {_QUARANTINE_LEDGER_TABLE}")
    op.execute(f"DROP TABLE IF EXISTS {_CLEANUP_LEDGER_TABLE}")
    op.execute(f"DROP TABLE IF EXISTS {_GATE_TABLE}")
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")

# TSK-280 测试契约：角色绑定受权运维修复（统一 /api/v2 REST）

本文件固定 TSK-280 的最窄可观察公共接缝。测试只通过这些接缝观察真实
FastAPI 路由、公开修复服务、真实 PostgreSQL 事务与 TSK-276 共享组锁/槽位
协作；不实现生产算法，不新增 debug 修复入口，不提供直接改指身份，不把
个人解绑扩权为身份删除。

最终依据：TSK-269 Resolution（评论 6a9ed8f71af305fd1c073f04）第六节、
TSK-280 描述与 TSK-268 验收矩阵「运营修复」行。

## 公共 seam

### 修复服务（新增模块 `komari_bot.plugins.character_binding.repair`）

测试允许模块内 import，不强制跨插件顶层重导出；但 `character_binding`
顶层 `__all__` 必须暴露 `BindingRepairService` 与 `get_binding_repair_service`，
供管理装配与其它插件经既有顶层暴露面引用。

```python
class BindingRepairService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        clock: Callable[[], datetime],
        manager: CharacterBindingManager | None = None,
    ) -> None: ...

    async def diagnose(
        self, *, app_id: str, group_openid: str,
    ) -> BindingDiagnosis: ...

    async def preview(
        self,
        *,
        app_id: str,
        group_openid: str,
        operator_id: str,
        member_openid: str | None = None,
        reason: str,
    ) -> RepairPreview: ...

    async def confirm(
        self,
        *,
        app_id: str,
        group_openid: str,
        token: str,
        operator_id: str,
        request_id: str,
        reason: str,
    ) -> RepairConfirmResult: ...

def set_binding_repair_service(service: BindingRepairService | None) -> None: ...
def get_binding_repair_service() -> BindingRepairService | None: ...
```

值对象（keyword 构造，frozen dataclass）：

```text
RepairScope = Literal["member", "group"]

MemberBindingView(app_id, group_openid, member_openid, member_qq, character_name)
BindingDiagnosis(app_id, group_openid, group_id, members, game_present, game_lifecycle)
RepairPreview(token, scope, app_id, group_openid, member_openid,
              affected_count, cleared_names, version, expires_at)
RepairConfirmResult(scope, app_id, group_openid, member_openid,
                    cleared_count, cleared_names)
```

DTO 字段集合以本清单为唯一真源；`RepairPreview` **没有** `game_present`
字段，测试不得访问未定义的属性。

异常（公开、稳定）：

```python
class RepairTokenError(RuntimeError)               # 无效/过期/已用/操作者不符/目标不符
class RepairTargetNotFoundError(RuntimeError)     # 未找到群映射或成员关联
class RepairDependencyChangedError(RuntimeError)   # 目标版本或依赖集合已变化
class RepairBlockedByGameError(RuntimeError)       # 群内存在 waiting/active 对局
```

存储/基础设施失败继续使用既有 `BindingPersistenceError`，不得伪装成「无目标」。

### 服务语义

- `diagnose`：只读诊断；必须直接读取 PostgreSQL（`BindingTransaction.resolve_group`
  / `resolve_member` 与 `PostgresRouletteStorage.load_current` 只读路径），
  禁止用 manager 缓存快照代替实时数据。返回群映射、全部成员视图（含
  已解绑、`character_name` 为空的成员）与是否存在 waiting/active 对局
  （`game_lifecycle` 取快照 `lifecycle`）。
- 目标选择：`member_openid` 为空 → `scope="group"`，目标为该 `(app_id,
  group_openid)` 群映射及依赖它的全部本群成员关联；`member_openid` 非空 →
  `scope="member"`，目标仅为该成员关联。范围固定在所选应用与群，绝不波及其
  他应用/群；不提供把甲关系改指向乙的任何操作。
- `preview`：目标存在 waiting/active 对局时抛 `RepairBlockedByGameError`
  （**预览阶段就拒绝**，不签发令牌）；否则计算 `affected_count`
  （member=1；group=成员数）与 `cleared_names`（将被清除的角色名列表，按
  member_openid 排序）并签发一次性令牌。
- 令牌：绑定 `(operator_id, app_id, group_openid, member_openid|None,
  scope, version)`；`version` 是目标行（群行 + 受影响成员行）与当前游戏快照
  （lifecycle/game_id/state_revision）的规范化 SHA-256 指纹；`expires_at =
  clock() + 10 分钟`，绝对过期、不自动续期；令牌仅存进程内，重启即失效；
  单次使用，重复/并发 confirm 不得重复清除。
- `confirm`：先校验令牌（存在、未过期、未使用、`operator_id` 相符、目标
  app/group 相符——篡改任一目标都只能得到 `RepairTokenError`），再在同一
  事务内获取 `lock_group_scope`；**持锁后必须同时复核**：槽位
  （`PostgresRouletteStorage(session).load_current(group, for_update=True)`）、
  依赖集合（重新读取目标行计算 `version`）与**令牌是否仍在绝对 TTL 内**
  （锁等待跨过 TTL 后在提交前失效，不得清除任何行）。出现对局 →
  `RepairBlockedByGameError`，目标/依赖变化 → `RepairDependencyChangedError`，
  令牌过期/目标不符 → `RepairTokenError`；校验通过才删除 member 行（member
  级）或 group 行（group 级，FK CASCADE 依赖成员），单事务提交，失败保留原
  记录；提交成功后（且仅成功后）才允许刷新 manager 快照。令牌在每次 confirm
  尝试开始时原子消耗：成功、依赖变化或对局阻断后均不可再使用。
- 修复绝不：强制终局/推进到期、修改历史结果/胜场/排行榜、修改准入策略、
  发送任何通知、替用户重建绑定或写回任何角色名。普通用户解绑
  （`clear_character_name`）只清角色名并保留身份关系，只有修复入口才删除
  身份关系。

### 真实 FastAPI 路由（新增模块 `komari_bot.plugins.character_binding.management_api`）

```python
API_PREFIX = "/api/v2/character-bindings/repair"

def register_character_binding_repair_api(
    app: FastAPI,
    *,
    api_token: ManagementTokenSource,
    allowed_origins: Sequence[str],
    service_getter: Callable[[], BindingRepairService] | None = None,
    audit_recorder: ManagementAuditRecorder | None = None,
) -> None: ...
```

路由闭集（恰好三个，幂等注册，复用共享鉴权/CORS/审计工具）：

| 方法/路径 | 权限 | 请求 | 说明 |
| --- | --- | --- | --- |
| `GET  {API_PREFIX}/diagnose?app_id=&group_openid=` | `character_binding:read` | — | 只读诊断 |
| `POST {API_PREFIX}/preview` | `character_binding:manage` | body `{app_id, group_openid, member_openid?}` + `X-Komari-Change-Reason` + `X-Request-ID` | 影响预览 → 令牌 |
| `POST {API_PREFIX}/confirm` | `character_binding:manage` | body `{app_id, group_openid, token}` + `X-Komari-Change-Reason` + `X-Request-ID` | 确认清除 |

- 路由注册不得依赖 group_admission 运行时、轮盘 `plugin_enable` 或任何准入
  裁决：受限群、轮盘关闭时受权控制面仍可达。
- `character_binding:read` 必须加入共享权限蕴含表（manage 蕴含 read，与既有
  `_READ_PERMISSION_IMPLICATIONS` 约定一致）；read 不蕴含 manage。
- 凭据可含 `revoked_at`；已撤销凭据对三个端点一律 401，即使带 manage 权限。
- 错误码（固定 detail 文本，白名单）：400 缺 reason/request-id（复用共享
  依赖）；404 `RepairTargetNotFoundError`（「未找到目标群映射」/「未找到目标
  成员关联」）；409 `RepairDependencyChangedError`（「绑定状态已变化，请重新
  预览」）；409 `RepairBlockedByGameError`（「群内存在进行中的对局，无法修
  复」）；422 `RepairTokenError`（「确认令牌无效、已过期或已使用」）；503
  `BindingPersistenceError`（「绑定修复存储暂不可用」）。
- 审计：复用 `management_audit_span`；action 固定为
  `character_binding.repair.preview` / `character_binding.repair.confirm`（每
  次尝试各记一条 started + 一条 succeeded/failed）；`target_hash =
  hash_management_target(app_id, group_openid, member_openid|"<group>")`；
  metadata 记录 scope、version 指纹、预期/实际数量与结果码（preview 成功
  `result_code="preview_issued"`，confirm 成功 `result_code="cleared"`，失败
  由 span 记录异常）；绝不写入原始
  app_id/group_openid/member_openid/member_qq/角色名正文。
- 审计失败安全性（与既有共享 span 语义一致）：**started 事件失败 → 请求
  失败且业务不执行**（不签发令牌/不删除）；**final 事件失败 → 操作照常成功**，
  由共享 `_record_final_event` 吞掉并记 critical 日志。
- 测试注入缝：服务构造的 `session_factory` 可由测试替换为产出确定性
  commit 失败 session 的工厂；confirm 方法不接受额外 session 参数。
  提交失败注入使用**真实 SQLAlchemy `Session.before_commit` 事件**（在真正
  COMMIT 之前抛 `OperationalError`），不依赖实现方的提交写法，也不跳过
  session 关闭。

### 真实装配（root 验收接缝）

`test_tsk280_assembly.py` 走完整真实链：`set_binding_repair_service(真实服务)`
→ `register_character_binding_repair_api(service_getter=get_binding_repair_service)`
→ 真实 FastAPI 路由 → 真实 PostgreSQL 会话工厂，端到端
diagnose → preview → confirm，并断言数据库不变量与真实审计事件。禁止仅注入
FakeService 后宣称功能可用。

### TSK-276 协作（复用真实接口，不新增抽象）

服务在自持的同一 `AsyncSession` 内使用：

```python
from komari_bot.db.group_transaction_locks import lock_group_scope
from komari_bot.plugins.komari_roulette import (
    GroupRef,
    PostgresRouletteStorage,
)

await lock_group_scope(session, app_id=app_id, group_openid=group_openid)
snapshot = await PostgresRouletteStorage(session).load_current(
    GroupRef(app_id=app_id, group_openid=group_openid), for_update=True,
)
```

锁序与绑定写入/轮盘命令一致（先取共享组锁再读行），持锁后必须重读目标行，
禁止先查缓存再提交。诊断路径不取锁、只读已提交行。

## AC → 测试映射

| 验收点 | 测试节点 |
| --- | --- |
| 统一 /api/v2 REST；read 仅诊断、manage 才可预览/确认 | `test_tsk280_rest_api.py::test_diagnose_route_requires_read_permission_and_returns_diagnosis`、`test_read_only_credential_cannot_preview_or_confirm`、`test_manage_credential_can_preview_confirm_and_read`、`test_route_set_is_fixed_without_identity_repoint` |
| 不新增 debug 修复入口/直接改指身份 | `test_tsk280_surface_guards.py::test_debug_bind_subcommands_are_not_extended`、`test_character_binding_matchers_stay_at_frozen_census`、`test_tsk280_rest_api.py::test_route_set_is_fixed_without_identity_repoint` |
| 成员级清除所选错误关联及本群角色名 | `test_tsk280_pg.py::test_member_scope_preview_and_confirm_with_isolation`（含跨应用同 openid/同应用其他群不变） |
| 群级清除映射及依赖、跨群/应用隔离 | `test_tsk280_pg.py::test_group_scope_preview_and_confirm_clear_all`（群映射与成员全部清除，其他应用/群不变） |
| 预览范围/数量明确、reason 必需 | `test_tsk280_rest_api.py::test_preview_reports_scope_count_names_and_token`、`test_preview_and_confirm_require_reason_and_request_id` |
| 预览阶段拒绝 waiting/active 对局 | `test_tsk280_pg.py::test_preview_refused_while_waiting_game_exists`、`test_preview_refused_while_active_game_exists` |
| 令牌绑定操作者/对象/目标/版本/依赖集合 | `test_tsk280_pg.py::test_confirm_rejects_operator_mismatch`、`test_confirm_rejects_tampered_app_or_group_target`、`test_confirm_rejects_when_dependency_changed_between_preview_and_confirm`、`test_confirm_rejects_when_member_added_after_preview`、`test_confirm_rejects_when_member_deleted_after_preview`、`test_confirm_rejects_when_target_unbound_after_preview` |
| 10 分钟绝对 TTL、单次、重启失效 | `test_tsk280_pg.py::test_token_is_single_use`、`test_same_token_concurrent_confirm_clears_exactly_once`、`test_token_ttl_boundary_at_nine_minutes_fifty_nine`、`test_token_expires_at_exactly_ten_minutes`、`test_token_store_is_in_memory_and_restart_invalidates` |
| 锁等待跨 TTL 后提交前失效 | `test_tsk280_pg.py::test_confirm_rechecks_token_expiry_after_lock_wait` |
| 确认重鉴权、依赖变化重新预览 | `test_tsk280_rest_api.py::test_manage_credential_can_preview_confirm_and_read`、`test_tsk280_pg.py::test_confirm_rejects_when_dependency_changed_between_preview_and_confirm` |
| 未授权/已撤销凭据不得读写身份 | `test_tsk280_rest_api.py::test_diagnose_route_requires_read_permission_and_returns_diagnosis`、`test_read_only_credential_cannot_preview_or_confirm`、`test_revoked_manage_credential_cannot_access_repair` |
| 事务失败原子保留（同实例 + 真实 before_commit 注入） | `test_tsk280_pg.py::test_confirm_commit_failure_preserves_original_rows`、`test_confirm_commit_failure_closes_failed_session`；注入健康验证见 `test_tsk280_fixture_probe.py::test_commit_failure_switch_health_check` |
| waiting/active 拒绝修复 | `test_tsk280_pg.py::test_confirm_refused_while_waiting_game_exists`、`test_confirm_refused_while_active_game_exists`、`test_confirm_rechecks_slot_after_lock_wait` |
| 并发 bind/rename/unbind/open 与修复共用 PG 锁、不能缓存先查 | `test_tsk280_pg.py::test_confirm_rechecks_slot_after_lock_wait`、`test_concurrent_confirm_and_bind_share_group_lock`、`test_concurrent_confirm_and_roulette_open_share_group_lock`、`test_diagnose_reads_postgres_not_manager_cache` |
| 不损坏冻结名/历史/胜场；不强制终局/改准入/通知/替用户重绑 | `test_tsk280_pg.py::test_completed_game_history_and_wins_preserved_after_repair`、`test_repair_does_not_rebind_notify_or_change_admission` |
| 普通用户解绑保留身份关系回归 | `test_tsk280_pg.py::test_normal_user_unbind_keeps_identity_relationship` |
| 审计安全 ID、权限/理由/请求 ID/版本/数量/结果；审计失败安全性 | `test_tsk280_rest_api.py::test_audit_records_safe_fields_only`、`test_audit_start_failure_aborts_without_operation`、`test_audit_final_failure_does_not_break_operation` |
| 受限群/轮盘关闭 REST 控制面仍可达 | `test_tsk280_rest_api.py::test_registration_does_not_require_group_admission_or_roulette_runtime` |
| 真实装配：register → service get → 真实服务 → 真实 PG | `test_tsk280_assembly.py::test_register_to_service_get_real_assembly` |
| 夹具独立正确（不依赖缺失业务模块） | `test_tsk280_fixture_probe.py`（终局 seam/统计/清理 SQL/锁等待/提交失败注入） |

## 真实 PostgreSQL 门控

`test_tsk280_pg.py` / `test_tsk280_fixture_probe.py` / `test_tsk280_assembly.py`
使用既有门控语义（`KOMARI_TEST_POSTGRES_URL` + `SQLALCHEMY_DATABASE_URL`
同库守卫，见 `tests/character_binding/conftest.py` 与 `tsk280_support.py`）。
无环境变量时跳过；TSK-280 与其它代理共享门控库期间不同时并跑，隔离库由根
后续提供。当前基线运行阶段生产模块尚未实现，PG 测试与无服务测试同样以
「缺失业务模块」为预期 RED；夹具探针在无 repair 模块时即可独立验证。

# TSK-279 测试契约（Stage-A：配置 + 冻结文案/权重 + 旧 API 探针）

> 本文件固定 TSK-279 全票 AC 到可验证行为的映射、拟议的最窄公共接缝，以及
> Stage-A 实际落地的测试范围。**本阶段只写配置业务验证与冻结 RED + 真实旧 API
> 探针，不宣称整票完成，也不实现任何生产接缝。**
>
> 依据：TSK-279 描述、TSK-269 最终 Resolution（评论
> `6a9ed8f71af305fd1c073f04`）、TSK-266 最终索引与终局修订（评论
> `6a9ec4831af305fd1c073eac`、`6a9ece961af305fd1c073eb4`）、
> `tests/komari_roulette/TSK-278-contract.md`、当前 canonical 模型
> （`domain.py` / `command_service.py` / `storage.py` / `mapper.py` /
> `qq/renderer.py`）。
>
> 生产实现由 TSK-279 本体负责；本文件的接缝名是**拟议**，测试按此 RED 基线
> 观察行为。Stage-A 之后若实现采用等价接缝，以行为断言为准。

## 1. 全票 AC 映射

TSK-279 描述与 TSK-269 Resolution §1–§7 的验收面逐条落到可观察行为：

| # | AC（来源） | 可观察行为 | 本阶段 |
|---|-----------|-----------|--------|
| A1 | 强类型配置：`plugin_enable` 默认 `false`、动态生效（§1） | 配置表默认值；通用管理注册 | ✅ 本阶段 RED |
| A2 | 道具权重四项默认各 1、非负、总和 > 0（§1） | 配置默认/校验；`item_weights()` 映射 | ✅ 本阶段 RED |
| A3 | 权重在 `waiting→active` 冻结，改配置不影响已开始对局（§1） | 真实 PG：start 写入的 `item_weights` 快照 | ✅ 本阶段 RED（经拟议 provider） |
| A4 | 成功动作 + 终局按闭集结果类型给非空文案 list（§1） | 配置闭集键、每键非空 | ✅ 本阶段 RED |
| A5 | 等候取消/最后退出/等候超时保持固定模板，不进配置（§1、1G） | 固定键不在可配置闭集 | ✅ 本阶段 RED |
| A6 | 模板只允许规定占位符；拒绝属性/索引/format 绕过（§1） | `copy_pool.validate_template` | ✅ 本阶段 RED |
| A7 | 配置不能注入第二个原生 mention（§1） | 含 `<qqbot-at-user>` 的模板被拒 | ✅ 本阶段 RED |
| A8 | 终局任意有效配置模板必须表达胜者、事件、累计胜场，普通单段（§1、1F） | 终局必含 `{winner}`/`{event}`/`{wins}`；普通单段拒绝 `**`/`>`/多行 | ✅ 本阶段 RED |
| A9 | 复用 `CONTENT_TEXT_BUDGET`，不新增猜测的 QQ 长度门槛（§1、引用 12 条） | 超预算模板被拒；中等长度不额外设限 | ✅ 本阶段 RED |
| A10 | 动作展示随机独立于领域随机；首次结果+模板随收据冻结；更新/重启/同 msg 不重抽（§1、1A） | 真实 PG：收据 body 冻结，replay 不重渲染 | ✅ 本阶段 RED（经拟议 projector 工厂） |
| A11 | 真实姓名含 markup 逃逸；终局 winner/event/wins（§1、1F） | 真实 PG 服务 → 真实 renderer | ✅ 本阶段**绿**探针 |
| A12 | 启动/周期/惰性推进共用 PG 期限与幂等事务；重启不续期（§2、§3） | 恢复 worker | ⏭ 后续阶段 |
| A13 | 收据/履约 7 天、非胜终态 30 天清理；completed/胜场长期保留（§3） | 清理 worker | ⏭ 后续阶段 |
| A14 | 关闭不销毁共享 ORM 引擎；不新增轮盘 Redis 正确性依赖（§2） | lifecycle | ⏭ 后续阶段 |
| A15 | `PENDING_CONFIRMATION` 仅观测，不重发/不猜（§2、§4） | 发送侧 | ⏭ 后续阶段 |
| A16 | ready/disabled/failed、最近扫描/清理、待确认计数、原因码（§4） | 观测 | ⏭ 后续阶段 |
| A17 | REST：通用配置 + `roulette:read` 状态/校验、`roulette:manage` 按 completed 重建（§4） | 管理 API | ⏭ 后续阶段 |
| A18 | 首次未映射群受限核验例外（§5） | group_admission | ⏭ TSK-274/后续 |
| A19 | 绑定预览/确认清除（§6） | character_binding | ⏭ TSK-280 |

Stage-A **只实际写 A1–A11**（其中 A11 为绿探针，A1–A10 为 RED）。

## 2. 拟议最窄公共接缝（供实现承接）

### S1. 强类型配置资源 —— `komari_bot.plugins.komari_roulette.config_schema`

```python
class DynamicConfigSchema(TypedConfigModel, table=True):
    plugin_name: ClassVar[str] = "komari_roulette"
    __tablename__ = "komari_roulette_config"

    plugin_enable: bool = False                 # default_apply_mode: immediate
    item_weight_magnifier: int = 1              # ge=0
    item_weight_beer: int = 1                   # ge=0
    item_weight_burst: int = 1                  # ge=0
    item_weight_lock: int = 1                   # ge=0
    action_copy_pool: dict[str, list[str]]      # JSONB，闭集键 → 非空模板 list
    final_copy_pool: dict[str, list[str]]       # JSONB，闭集键 → 非空模板 list

    def item_weights(self) -> dict[ItemType, int]: ...
```

- 四项权重：默认各 1、`ge=0`，模型级校验总和 `> 0`；数值约束与
  `domain._normalize_weights` 一致（禁止布尔/负数/零和，不额外加码）。
- `plugin_enable` 默认 `false`，`json_schema_extra={"apply_mode": "immediate"}`。
- 模板校验委托 S2；非法模板在构造期抛 `ValidationError`。
- `action_copy_pool` / `final_copy_pool` 使用 JSONB，便于管理 API 与迁移。

### S2. 纯文案快照/编译深模块 —— `komari_bot.plugins.komari_roulette.copy_pool`

无 NoneBot / 无 DB / 无全局可变状态；`config_schema` 与 `qq` 层都只依赖它。

```python
ACTION_COPY_KEYS: frozenset[str]          # 闭集：可配置的成功动作结果句
FINAL_COPY_KEYS: frozenset[str]           # 闭集：completed 终局原因（shot/forfeit/timeout）
ACTION_TEMPLATE_PLACEHOLDERS: Mapping[str, frozenset[str]]
FINAL_ALLOWED_PLACEHOLDERS: frozenset[str]   # {"winner","event","wins"}
FINAL_REQUIRED_PLACEHOLDERS: frozenset[str]  # {"winner","event","wins"}（每条终局模板都必须含）
DEFAULT_ACTION_COPY_POOL: Mapping[str, tuple[str, ...]]
DEFAULT_FINAL_COPY_POOL: Mapping[str, tuple[str, ...]]

class CopyPoolValidationError(ValueError): ...
class CopyPoolSnapshot:                    # frozen；action/final 各键均为非空 tuple
    action_templates: Mapping[str, tuple[str, ...]]
    final_templates: Mapping[str, tuple[str, ...]]

def validate_template(template: str, *, allowed_placeholders: frozenset[str], final: bool = False) -> str: ...
def compile_copy_pool(action_copy_pool, final_copy_pool) -> CopyPoolSnapshot: ...
def default_copy_snapshot() -> CopyPoolSnapshot: ...
```

模板规则（`validate_template`，对动作与终局一致，终局额外要求必含三占位符）：

1. 非空、去首尾空白后非空；长度与 `komari_bot.llm.content_budget.CONTENT_TEXT_BUDGET`
   对齐（复用 `validate_text_budget` / `normalize_required_text`，**不新增 QQ 长度**）。
2. 经 `string.Formatter().parse` 解析；每个替换字段：
   - `field_name` 必须∈ `allowed_placeholders`，且必须是纯标识符；
   - 拒绝 `{}`（自动编号）、`{0}`（位置）、`{a.b}`（属性）、`{a[b]}`（索引）；
   - `conversion` 必须为 `None`（拒绝 `{a!r}` / `{a!s}` / `{a!a}`）；
   - `format_spec` 必须为空（拒绝 `{a:>5}` 与嵌套 `{a:{w}}`）。
3. 单行普通文本：拒绝换行/制表符、`**`/`***`、行首 `>`（块引用）、列表前缀。
4. 拒绝任何原生提及结构（`<qqbot-at-user` / `<@` / 裸 `<`/`>`）；真实提及只能由
   renderer 注入，配置不可伪造。
5. 终局（`final=True`）每条模板必须同时含 `{winner}`、`{event}`、`{wins}`，保证
   任意有效配置都表达胜者、终局事件、结算后累计胜场；渲染为普通单段。

### S3. 文案投影工厂 —— `komari_bot.plugins.komari_roulette.qq.renderer`

现状：模块级 `_sentence_pool: dict[str, str]` + `set_sentence_pool(Mapping[str,str])`
（可从外部替换的全局可变单值池）。TSK-279 须改为**每动作非空 list + 隔离随机快照**：

```python
class CopyRandomSource(Protocol):          # 建议定义在 copy_pool
    def choice(self, options: Sequence[str]) -> str: ...

def build_reply_projector(
    *,
    snapshot: CopyPoolSnapshot,
    random_source: CopyRandomSource,
) -> Callable[[ReplyProjectionContext], ReplyProjection]: ...
```

- `build_reply_projector` 返回一个闭包 projector，注入
  `RouletteCommandService(reply_projector=...)` 的既有接缝；每次投影用
  **隔离的** `random_source` 从非空 list 抽一条并写入收据，domain 随机源互不影响。
- `render_reply(context)` 保留为默认快照投影（既有 TSK-278 测试的公共接缝），
  不得新增“配置缺失 → 回退全局可变池”的兼容 fallback。
- **无参全局 mutable token / 无参全局配置**不作为 TSK-279 的文案注入路径。

### S4. 权重冻结装配 —— `RouletteCommandService`

现状：`CanonicalCommand.start()` → `Action.start(player)` **不带权重**；
`Action.start` 已支持 `item_weights: Mapping[ItemType,int] | None`（已核验）。
拟议最小改动：

```python
RouletteCommandService(
    *,
    session_factory,
    reply_projector,
    random_source=None,
    item_weights_provider: Callable[[], Mapping[ItemType, int]] | None = None,
)
```

- `start` 命令在领域事务内调用 `item_weights_provider()`，以
  `Action.start(player, item_weights=...)` 写入并冻结为 `komari_roulette_games.item_weights`。
- provider 为 `None` 时沿用领域默认各 1（现有行为不变）。
- 权重只在 `waiting→active` 读取一次；配置更新只影响之后新建的对局。

> 说明：S4 是 Stage-A 为“真实 PG 冻结权重”提出的最窄装配点；runtime composition
> 仍只在未来装配阶段实现（不在本阶段）。

## 3. Stage-A 测试文件

| 文件 | 性质 | 覆盖 |
|------|------|------|
| `test_tsk279_config.py` | 纯函数（无 PG/Redis） | 旧 API 绿探针（domain 默认权重、`Action.start` 权重冻结、非法权重拒绝、权重归一化）+ S4 ctor 接缝 RED + S1/S2 RED 配置与模板校验 |
| `test_tsk279_configuration_pg.py` | 真实 PG | 旧 API 绿探针（seed 冻结默认权重、终局 winner/event/wins 单段、markup 逃逸、非终局不多 @）+ S4 权重冻结/换局 RED + S3 收据文案冻结/隔离随机 RED |
| `tsk279_support.py` | 支持 | 每例真实 scope 追踪与清理、缺失接缝清晰报错、隔离文案随机源、有界任务释放 |

**RED 与绿必须可区分**：绿探针只使用当前已存在的符号；RED 通过
`importlib.import_module` 动态加载缺失模块或直接构造尚未接受新参数的 service，
失败为 `ModuleNotFoundError` / `AttributeError` / `TypeError`，绝不把整票做成全
`ImportError`。

**清理边界**：旧 TSK-278/276 harness 的 `delete_scope(scope("fixture"))` 与实际用例
scope 不匹配，会积累残留。本票 `tsk279_support.Tsk279Harness.scope()` 在
`finally` 中删除该用例真实 scope 的轮盘行与 character_binding 群/成员行；并发任务
经 `cancel_and_join()` 有界释放。

## 4. 后续阶段待补测试（本阶段不写）

- **runtime/生命周期**：启动依赖顺序、`plugin_enable=false` 拒绝新业务、关闭不 dispose
  共享引擎、打开/关闭竞态、受限群不推进。
- **worker/调度**：60s/100 局扫描、PG 期限惰性推进、跨多期限重启恢复、只补结算旧当前
  玩家一次、04:00 清理边界（7 天/30 天/completed 长期）、PG/Redis 故障降级。
- **发送/观测**：`PENDING_CONFIRMATION` 仅观测、后台无 msg_id 不建 outbox、ready/
  disabled/failed、低基数计数与原因码、日志脱敏。
- **REST/管理**：通用配置注册、`roulette:read` 状态/校验、`roulette:manage` 按
  completed 重建与并发终局协调、审计。
- **迁移**：`komari_roulette_config` 的 Alembic revision + `check` 零 diff。

## 5. Stage-A 执行记录（命令日志）

环境：worktree `/Users/derbay32/project/komari-bot/.agents/worktrees/tsk-279`，
HEAD `bc4b1cce5e1f3042784021c3055ea999b7027347`，root venv
`/Users/derbay32/project/komari-bot/.venv/bin/python`（3.13.11）。
PG/Redis 门控：

```
SQLALCHEMY_DATABASE_URL=postgresql+asyncpg://komari_test@127.0.0.1:55458/komari_tsk279_resume
KOMARI_TEST_POSTGRES_URL=postgresql+asyncpg://komari_test@127.0.0.1:55458/komari_tsk279_resume
redis://127.0.0.1:56358/15
```

| 命令 | 结果 |
|------|------|
| `ruff check tests/komari_roulette/{tsk279_support.py,test_tsk279_config.py,test_tsk279_configuration_pg.py}` | ✅ All checks passed |
| `pytest tests/komari_roulette/test_tsk279_config.py -q` | 4 passed（旧 API 绿）/ 38 failed（RED） |
| `pytest tests/komari_roulette/test_tsk279_configuration_pg.py -q`（带 PG 门控） | 4 passed（真实 PG 绿）/ 3 failed（RED） |
| 两文件合并 `-q --tb=line` | 8 passed / 41 failed，`AssertionError` 计数 = 0 |

RED 失败原因分类（`--tb=line`）：

- `ModuleNotFoundError: komari_bot.plugins.komari_roulette.config_schema` — 16 例
- `ModuleNotFoundError: komari_bot.plugins.komari_roulette.copy_pool` — 22 例
- `AttributeError: ...qq.renderer.build_reply_projector is not implemented yet` — 2 例
- `TypeError: RouletteCommandService.__init__() got an unexpected keyword argument 'item_weights_provider'` — 1 例

绿探针覆盖（全部真实符号，无 TSK-279 新模块）：

- 领域：`DEFAULT_ITEM_WEIGHTS` 四项各 1；`Action.start` 快照冻结且拒绝负数/零和/布尔；
  缺键归一化为 0。
- 真实 PG：创建/加入/开始后 `item_weights` 落库为 `{magnifier:1,beer:1,burst:1,lock:1}`；
  弃权终局经真实 `render_reply` 得到普通单段（无换行/`> ` /`**`/`***`/`- `），
  表达唯一胜者 + 事件（`弃权出局`）+ `累计胜场 1`，且恰好一条原生提及；
  姓名 `红<&*>` 转义为 `红&lt;&amp;\*&gt;`；等待局主回合的回复不含多余 @。

清理核验：用例结束后按前缀 `tsk276-app-tsk279-%` 统计
`komari_roulette_games` / `command_receipts` / `character_binding_groups` /
`character_binding_members` 均为 0，绑定成员总数保持 8（未累积残留）。

类型检查：`pyright`（root venv 调用，无法解析 venv site-packages）在本仓任何测试
文件都报 `reportMissingImports`（包括既有 `test_command_service.py`），属环境差异；
新增的真实类型错误仅 3 处 `reportCallIssue: No parameter named "item_weights_provider"`
，即 S4 尚未实现的红，与 pytest RED 一致。

仍未覆盖：第 4 节列出的 runtime/worker/发送观测/REST/迁移面。

## 6. Stage-A 验收强化记录（TSK-279 runtime-recovery 回归）

本阶段把 Stage-A 的功能性验收钉在**真实** service / domain / renderer / 强类型
config manager 上，避免用假单测上下文「猜终局」而假绿。

新增/更新的真实服务用例：

| 文件 | 覆盖 |
|------|------|
| `test_tsk279_configuration_pg.py` | 自定义合法快照：`created` 闭集内加入脚本文案（且非首项），断言复制随机源确实被投喂该键的编译列表、域随机源完全不被触碰、改池/重建 service/重放收据都不重抽 |
| `test_tsk279_stage_a_pg.py` | 真实 `ConfigManager("komari_roulette", DynamicConfigSchema)` 初始化写 JSONB 默认值 + 字段 CAS 更新 + 非法池拒绝且 PG 行逐字节不变；三种终局（forfeit / 自定义先实弹枪膛 shot / PG 期限超时）分别命中 `shot`/`forfeit`/`timeout` 键并断言 winner + 真实事件 + `累计胜场 1` + 唯一提及，收据重放不重选；合法 action 分支 created/joined/host_transferred/shoot/lock；固定 cancel/lastleave/waitexpiry 文案不受自定义池影响；markup 名字转义 |
| `test_tsk279_terminal_key_fallback.py` | 纯函数记录并钉住**不合规范的 fallback** |

### `_final_copy_key` 缺 reason 回退（不合规范，仅供直接单测投影）

`komari_bot/plugins/komari_roulette/qq/renderer.py::_final_copy_key` 在
`details` 同时缺少 `eliminated_reason` 与 `completion_reason` 时回退
`DEFAULT_FINAL_COPY_KEY == "shot"`（docstring 也承认面向 direct unit
projection）。真实领域 `_eliminate_current` 必定在 reply details 盖入原因
（shot/forfeit 为 `eliminated_reason`，timeout 为 `eliminated_reason="timeout"`，
forfeit 另带 `completion_reason`），因此该回退**不是**合法终局语义。
`test_tsk279_terminal_key_fallback.py` 显式断言该回退选中 `shot` 槽、且原因存在
时不再回退；真实三分支由 `test_tsk279_stage_a_pg.py` 的三条真实终局负责，二者
不可互相替代。若后续要求删除该回退，应先更新此记录与用例。

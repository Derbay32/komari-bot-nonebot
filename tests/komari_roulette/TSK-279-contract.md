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
| `test_tsk279_terminal_key_fallback.py` | 纯函数 RED：missing / unknown / 互相矛盾终局 reason 必须拒绝投影；三种真实 reason 仍各自命中闭集键 |

### `_final_copy_key` 终局 reason 契约（收尾返工：拒绝猜测）

`komari_bot/plugins/komari_roulette/qq/renderer.py::_final_copy_key` 必须把
`details` 的 `eliminated_reason` / `completion_reason` 解析为**唯一**闭集终局
原因 `shot` / `forfeit` / `timeout`。以下输入一律在投影前以 `ValueError`
（允许专门子类，如 `ValueError` 子类）拒绝，**不抽任何文案**、不产生捏造
「出局」事件或 `shot` 终局模板，且异常消息安全、不回显原始 `details`：

- 两个字段都缺失（`{}`）；
- 任一存在的字段值不在闭集内（unknown，含「一个合法 + 一个非法」）；
- 两个字段都存在且为不同闭集键（互相矛盾）。

真实的三个原因仍各自命中自身键：`shot` 只在 `completion_reason` 盖入，
`forfeit` / `timeout` 由 `_eliminate_current` 把两字段盖成同一键。真实三分支
由 `test_tsk279_stage_a_pg.py` 的三条真实终局负责；纯契约由
`test_tsk279_terminal_key_fallback.py` 负责（**RED** 基线，见 §7）。禁止再用
`DEFAULT_FINAL_COPY_KEY` 之类实现常量作为断言输入。

### 278 遗留伪 context（Stage-A 收尾已补真实 facts，见 §8）

下述 TSK-278 用例直接构造 `lifecycle="completed"` 的投影 context 却未提供任何
终局原因字段；`_final_copy_key` 收尾返工（缺失/未知/矛盾即拒绝）落地后，这些
用例会随之由绿转红，需要在**各自文件**补入真实闭合原因，而不是改动本票测试
来规避：

| 文件:行 | 用例 | 现状 | 建议 |
|---------|------|------|------|
| `tests/komari_roulette/test_tsk278_renderer.py:402` | `test_final_mentions_winner_once_with_metadata_pair` | ✅ 已补 `details={"completion_reason": "shot", "winner_seq": 1}` | 补 `details={"completion_reason": "shot", "winner_seq": 1}`（真实实弹终局同形） |
| `tests/komari_roulette/test_tsk278_renderer.py:862` | `test_render_escapes_xml_injection_in_frozen_name` | ✅ 已补 `details={"completion_reason": "shot", "winner_seq": 1}` | 同上，补 `completion_reason="shot"` |
| `tests/komari_roulette/test_tsk278_keyboard.py:374` | `test_final_has_no_buttons` | ✅ 未受影响：该用例不调投影，仅调 `build_keyboard`，未改且实测仍绿 | 仅当返工把校验上移到共享 context / `build_keyboard` 路径时受影响；否则保持不动 |

## 7. Stage-A 收尾返工记录（终局 reason 拒绝猜测，RED）

依据验收意见：把已标注 non-conforming 的「缺 reason → `shot`」绿断言改为
**RED**——missing / unknown / 互相矛盾终局 reason 必须明确拒绝投影；三种真实
reason 仍正确；不再 import `DEFAULT_FINAL_COPY_KEY` 断言实现常量。本轮**只改**
`test_tsk279_terminal_key_fallback.py` 与本合同；生产 `_final_copy_key` 由
TSK-279 本体修复，修复前该文件保持 RED。

环境：worktree `/Users/derbay32/project/komari-bot/.agents/worktrees/tsk-279`，
HEAD `acc4215`，root venv `/Users/derbay32/project/komari-bot/.venv/bin/python`
（3.13.11）；定向纯函数用例不需要 PG/Redis 门控。

| 命令 | 结果 |
|------|------|
| `ruff check tests/komari_roulette/test_tsk279_terminal_key_fallback.py` | ✅ All checks passed |
| `pytest tests/komari_roulette/test_tsk279_terminal_key_fallback.py -q` | 3 passed（三种真实 reason）/ 6 failed（RED） |

RED 失败原因（`--tb=line`）：`Failed: DID NOT RAISE ValueError` ×6，分别对应
`missing` / `unknown` / `unknown-secondary` / `contradictory` 四个拒绝用例 +
`test_rejection_message_does_not_echo_raw_details` +
`test_default_projection_rejects_missing_reason`。即当前实现静默回退 `shot`，
正是本次要求消除的错误 fallback；不为全绿而断言该 fallback。

真实三分支真证仍由 `test_tsk279_stage_a_pg.py`（三条真实终局）承担；本返工
不得与其互相替代。

## 8. Stage-A 收尾：278 遗留伪 context 补真实事实（已完成）

§6 表列出的两条 TSK-278 投影用例是**用例自身的事实缺口**，按该表建议在各自文件
补齐真实业务事实；**不改写任何断言、不放宽校验、不全局 helper 猜原因**。

| 文件:行 | 用例 | 补入的 `details` |
|---------|------|------------------|
| `tests/komari_roulette/test_tsk278_renderer.py:402` | `test_final_mentions_winner_once_with_metadata_pair` | `{"completion_reason": "shot", "winner_seq": 1}` |
| `tests/komari_roulette/test_tsk278_renderer.py:862` | `test_render_escapes_xml_injection_in_frozen_name` | 同上 |

事实依据：`domain._eliminate_current(completion_reason="shot")` 对实弹终局只在
`terminal_reply` 盖入 `completion_reason` 与 `winner_seq`（**不含**
`eliminated_reason`；后者仅 `forfeit`/`timeout` 与 `completion_reason` 同键盖入），
且 `winner_seq` 等于存活座位编号，与 fixture 的 `winner=player(1, ...)` 一致。两处
只新增 `details`，原有唯一提及 / `mention_*` metadata 对 / 无按钮 / XML 逃逸 /
不泄漏 openid 断言逐条保留。

`test_tsk278_keyboard.py:374`（`test_final_has_no_buttons`）不调投影，未改，实测仍绿。

环境：worktree `/Users/derbay32/project/komari-bot/.agents/worktrees/tsk-279`，
补 facts 前 HEAD `5aafd0c`，root venv
`/Users/derbay32/project/komari-bot/.venv/bin/python`（3.13.11）；本 pane 无
PG/Redis 门控（PG 门控目标本轮未跑，仅改两条非 PG 纯投影 fixture）。

| 命令 | 结果 |
|------|------|
| `pytest .../test_tsk278_renderer.py::test_final_mentions_winner_once_with_metadata_pair ...::test_render_escapes_xml_injection_in_frozen_name -q`（补 facts 前） | 2 failed（`ValueError: 终局回复缺少唯一有效的出局原因`） |
| 同上（补 facts 后） | ✅ 2 passed |
| `pytest tests/komari_roulette/test_tsk278_renderer.py tests/komari_roulette/test_tsk279_terminal_key_fallback.py -q` | ✅ 81 passed（既有 renderer 72 + 新 9 契约） |
| `pytest tests/komari_roulette/test_tsk278_renderer.py tests/komari_roulette/test_tsk278_keyboard.py tests/komari_roulette/test_tsk279_terminal_key_fallback.py -q` | ✅ 102 passed |
| `pytest tests/komari_roulette/ -q`（无 PG/Redis 门控） | ✅ 369 passed, 121 skipped |
| `ruff check tests/komari_roulette/test_tsk278_renderer.py` | ✅ All checks passed |
| `pyright --pythonpath /Users/derbay32/project/komari-bot/.venv/bin/python`（worktree 根，`filesAnalyzed`=690） | ✅ 0 errors, 0 warnings |

Stage-B（runtime/worker/发送观测/REST/迁移面）未开始。

## 9. Stage-B：runtime / 恢复扫描 / 清理 / 观测（本阶段）

本阶段把 TSK-269 Resolution §2–§4 的生命周期、恢复、清理与观测面钉在**真实**
PG + 真实 `RouletteCommandService.advance_expired` 深入口上；文件：
`test_tsk279_runtime.py`、`test_tsk279_maintenance_pg.py`、
`test_tsk279_observability.py`。生产模块 `komari_bot.plugins.komari_roulette.
{runtime,maintenance,observability}` 尚不存在，RED 由 `load_symbol` 懒加载，失败为
`ModuleNotFoundError` / `AttributeError`（“缺失接缝”桶）；对既有 no-arg 回调
`send_gate` / `runtime_check` 的按调用演进用例失败为断言 / `TypeError`
（“断言”桶）。两者都是设计内 RED，不是 fixture 造假。

### 9.1 拟议最窄公共接缝

```python
# komari_bot/plugins/komari_roulette/runtime.py
class RouletteRuntimeStatus(StrEnum): READY; DISABLED; FAILED
RUNTIME_REASON_CODES: frozenset[str]      # 闭集：config_unavailable /
                                          # admission_unavailable / storage_unavailable /
                                          # recovery_failed / plugin_disabled / policy_restricted /
                                          # policy_admitted / not_ready
@dataclass(frozen=True) class RouletteRuntimeState:
    status; reason_code; recovery_completed: bool; plugin_enable: bool
@dataclass(frozen=True) class RouletteAuthority:
    allowed: bool; reason_code: str; scope: str
class RouletteRuntime:
    def __init__(self, *, config_manager, recovery, admission) -> None
    async def start(self) -> None          # config → recovery tick → READY/DISABLED/FAILED
    async def close(self) -> None          # blocks dispatch；不 dispose 共享引擎
    @property def accepting(self) -> bool
    def get_state(self) -> RouletteRuntimeState
    def authorize(self, *, scope: str, group_ids: Sequence[int], token=None) -> RouletteAuthority
    async def run_recovery_tick(self) -> object

# komari_bot/plugins/komari_roulette/maintenance.py
RECOVERY_INTERVAL_SECONDS = 60
RECOVERY_BATCH_SIZE = 100
CLEANUP_HOUR = 4
RECOVERY_JOB_ID; CLEANUP_JOB_ID
@dataclass(frozen=True) class RecoveryTickResult:
    scanned; advanced; skipped_restricted; failed; cursor
@dataclass(frozen=True) class CleanupResult:
    receipts_deleted; games_deleted; results_deleted; more_pending
class RouletteMaintenance:
    def __init__(self, *, session_factory, service, admission) -> None
    async def advance_due(self, *, batch_size: int = 100) -> RecoveryTickResult
    async def cleanup_retention(self, *, batch_size: int = 100) -> CleanupResult
def register_maintenance_jobs(scheduler, maintenance) -> None
def unregister_maintenance_jobs(scheduler) -> None

# komari_bot/plugins/komari_roulette/observability.py
OBSERVATION_REASON_CODES: frozenset[str]
@dataclass(frozen=True) class RouletteObservation:
    FIELDS: ClassVar[frozenset[str]]      # 固定键集合，随 as_dict() 一一对应
    runtime_status; runtime_reason; latest_scan; latest_cleanup;
    pending_receipts; fault_counts: tuple[tuple[str, int], ...]
    def as_dict(self) -> dict[str, object]
class RouletteObservability:
    def __init__(self, *, session_factory=None, clock=None) -> None
    async def refresh_pending(self) -> int
    def note_scan(self, result: RecoveryTickResult) -> None
    def note_cleanup(self, result: CleanupResult) -> None
    def note_fault(self, error: BaseException) -> None
    def snapshot(self) -> RouletteObservation
def safe_fault_projection(error: BaseException) -> dict[str, str]  # 仅 error_type + reason_code
def set_runtime_state(state: RouletteRuntimeState) -> None
```

`admission` 是**群级**门 `(app_id, group_openid) -> bool`，不携带成员身份；
maintenance 经真实 `advance_expired` 推进（waiting 超时与 active 回合超时同一深入口），
绝不发消息、绝不建收据/履约。

### 9.2 AC → 用例 → 未来真实装配位置

| AC（Resolution） | 用例（Stage-B） | 未来真实组合位置 |
|---|---|---|
| 启动顺序：ORM/配置/绑定准入/存储/恢复 | `runtime::test_start_reaches_ready_only_after_recovery_tick` | Stage-C：NoneBot `on_startup`，真实 `ConfigManager.initialize_async()` + `group_admission` + `RouletteCommandService` |
| 恢复完成前拒绝业务/发送 | `runtime::test_runtime_rejects_business_before_recovery_completes` | Stage-C：handler `business_gate` / delivery `runtime_check` 接 `authorize()` |
| `failed` ≠ 无对局 | `maintenance::test_completed_wins_project_once_and_recovery_does_not_resettle`（`no_active_game` 不改胜场） | Stage-C：`advance_expired` 结果码映射 |
| 动态关停只停新业务，维护继续 | `runtime::test_plugin_disabled_states_disabled_but_recovery_still_runs` | Stage-C：`plugin_enable` 即时重读 |
| 配置/DB 不可用 → FAILED；下一 tick → READY | `runtime::test_config_unavailable_reports_failed_without_running_recovery`、`::test_transient_recovery_failure_recovers_to_ready_next_tick` | Stage-C：真实异常分类，禁止静默 fallback |
| 跨多个错过 15min 期限只淘汰旧当前一次、新当前自 PG now 获满 15min | `maintenance::test_advance_expired_eliminates_once_and_grants_full_window`（**绿探针**） | 已用真实深入口；Stage-C 只做调度接线 |
| waiting 超时与回合超时共用深入口 | `maintenance::test_retention_fixture_has_real_terminal_lifecycles`（expired 分支）、`::test_recovery_skips_restricted_groups_and_creates_no_send` | Stage-C：`advance_due` 调 `advance_expired` |
| 60s / batch100 / coalesce / max_instances=1 仅节流 | `maintenance::test_maintenance_jobs_registered_with_throttle_and_deploy_timezone` | Stage-C：注册到 `nonebot_plugin_apscheduler` 单例 |
| 受限群不推进；再准入只推进旧当前一次 | `maintenance::test_recovery_skips_restricted_groups_and_creates_no_send` | Stage-C：`group_admission.adjudicate(intent=BUSINESS)` 按群 |
| 有界分页 2–3 tick 不饿死后续 allowed 群 | `maintenance::test_recovery_paginates_past_a_restricted_batch` | Stage-C：真实游标持久化 |
| 后台无合法 msgid：不建收据/履约、不发平台 | `maintenance::test_advance_expired_eliminates_once_and_grants_full_window`（计数不变）、`::test_recovery_skips_restricted_groups_and_creates_no_send` | Stage-C：maintenance 不持有 sender |
| PENDING 仅计数，不重发/不猜/不调平台 | `observability::test_pending_count_reflects_real_receipts` | Stage-C：`refresh_pending` 用真实 status 视图，绝不 claim |
| 超时终局发送失败不二次结算胜场 | 既有 TSK-278 delivery 用例 + `maintenance::test_completed_wins_project_once_and_recovery_does_not_resettle` | Stage-C：delivery `UNKNOWN` 保持 PENDING + 幂等终局 |
| NoRedis 正确性 | `maintenance::test_maintenance_does_not_require_redis` | Stage-C：确认维护路径无 Redis import/调用 |
| QQ 用户封禁保持真实群准入门 | `runtime::test_authority_is_per_call_isolated_between_groups` | Stage-C：真实 `user_ban` preprocessor 与群准入叠加 |
| 维护只群级准入、不凭空给 member | `maintenance::test_recovery_skips_restricted_groups_and_creates_no_send`（gate 仅 app/group） | Stage-C：真实群→member 映射在 handler 侧，不在 maintenance |
| 04:00 部署时区清理 | `maintenance::test_maintenance_jobs_registered_with_throttle_and_deploy_timezone`（`cleanup.trigger.timezone == 部署 tz`，不信 host TZ） | Stage-C：`nonebot_plugin_apscheduler` 默认 `Asia/Shanghai` |
| PG UTC 账龄：收据+履约 7d；cancelled/expired/failed 30d；waiting/active 永不删；completed+结果+座位+胜场长期 | `maintenance::test_cleanup_deletes_aged_receipts_and_keeps_recent`、`::test_cleanup_deletes_nonwin_terminals_keeps_completed_and_wins`、`::test_retention_fixture_has_real_terminal_lifecycles`（**绿探针**，`clock_timestamp()` 账龄） | Stage-C：真实保留策略常量与分组删除 |
| 真实表字段 + FK | `maintenance::test_retention_fixture_has_real_terminal_lifecycles`、`tsk279_support.seed_aged_receipt` / `insert_waiting_game` | Stage-C：迁移 0022+ 不得漂移 |
| 合法终态闭集 | `maintenance::test_retention_fixture_has_real_terminal_lifecycles`（completed/cancelled/expired/failed 各一） | Stage-C：清理只删终态，绝不删 waiting/active |
| 精确边界 7d/30d | `maintenance::test_cleanup_deletes_aged_receipts_and_keeps_recent`（7d+60s vs 6d）、`::test_cleanup_deletes_nonwin_terminals_keeps_completed_and_wins`（31d vs 40d completed） | Stage-C：边界常量 `> 7d` / `> 30d` |
| 批界 + 可重入清理，不丢证据 | `maintenance::test_cleanup_is_batch_bounded_and_reentrant` | Stage-C：真实批删除 + 游标 |
| protected 排首不饿死 eligible | `maintenance::test_cleanup_does_not_starve_eligible_behind_protected` | Stage-C：按表候选，不与 waiting/active 混队 |
| 清理不重建/不改胜场 | `maintenance::test_cleanup_deletes_nonwin_terminals_keeps_completed_and_wins` | Stage-C：清理绝不调 `rebuild_leaderboard` |
| shutdown：先停新派发/移除调度→有界 drain；不 dispose 共享 ORM 引擎；已发未确认保持 PENDING | `runtime::test_close_blocks_new_dispatch_and_never_disposes_shared_engine` | Stage-C：`on_shutdown` 按序；`nonebot_plugin_orm` 引擎共享 |
| 按事件/令牌隔离准入与发送上下文 | `runtime::test_authority_is_per_call_isolated_between_groups` | Stage-C：真实多事件并发，A 撤销不影响 B |
| 现网 no-arg `send_gate` / `runtime_check` 演进为按调用 | `runtime::test_handler_send_gate_receives_the_per_call_request`、`::test_delivery_runtime_check_receives_the_per_call_receipt` | Stage-C：`SendGate = Callable[[CommandRequest], bool]`、`RuntimeCheck = Callable[[CommandReceipt], bool]` |
| 观测 ready/disabled/failed、最近扫描/清理、待确认计数、固定原因码、低基数计数 | `observability::test_observability_seam_exposes_fixed_projection`、`::test_pending_count_reflects_real_receipts` | Stage-C：真实 runtime 状态源 + status 视图计数 |
| 恶意异常日志/投影不泄漏 identity/body/secret/chamber/future-reward | `observability::test_existing_reply_details_redaction_drops_hidden_and_secret_keys`（**绿探针**）、`::test_fault_projection_strips_a_malicious_exception` | Stage-C：真实 logger 输出走同一投影 |
| 不新增 /metrics、不主动 QQ 通知 | `observability::test_fault_projection_collapses_multiline_payload_to_one_record`（固定键、无 channel 字段） | Stage-C：合同约束，无新端点/通知 |
| 失败聚合不逐行重复 | `observability::test_fault_projection_collapses_multiline_payload_to_one_record` | Stage-C：按固定 reason 聚合并计次 |
| 正常回调不得接 always-true 假生产接线 | 全部 runtime 用例显式传测试端口 `RecordingConfig` / `RecordingRecovery` / `_admission_by_group`，且注释声明它们**不是**生产 authority | Stage-C：真实装配必须替换这些端口 |

### 9.3 Stage-B 绿探针（真实符号，无新模块）

- `advance_expired` 单次淘汰 + 新窗口满 15min（读 `clock_timestamp()`）。
- 终局 projection 幂等、胜场不二次结算、`no_active_game` 不碰胜场。
- 作用域 advisory lock 经 `backend_pid` / `wait_for_blocked` 真实阻塞可观。
- 四种真实终态 fixture（completed/cancelled/expired/failed）满足 CHECK/FK，
  `result_players ≥ 2`、运行时座位清空。
- 既有 `_safe_details` 允许清单丢弃 identity/body/secret/chamber/reward 键。

### 9.4 Stage-C 待验证链（本阶段不宣称）

真实 `on_startup`/`on_shutdown` 装配、真实 `group_admission` 群→member 解析、
真实 `user_ban` 叠加、真实调度单例注册、真实保留策略常量、`SendGate`/
`RuntimeCheck` 生产改签名、`nonebot_plugin_orm` 引擎共享断言的真实进程路径、
REST/管理面、Alembic 迁移与 `check` 零 diff。

### 9.5 Stage-B 执行记录（命令日志）

环境：worktree `/Users/derbay32/project/komari-bot/.agents/worktrees/tsk-279`，
HEAD `78bb94b48005213fb733334401bb83ed6286d6f6`（提交后 `a314ec6`），
root venv `/Users/derbay32/project/komari-bot/.venv/bin/python`（3.13.11）。
PG/Redis 门控：

```
SQLALCHEMY_DATABASE_URL=postgresql+asyncpg://komari_test@127.0.0.1:55458/komari_tsk279_resume
KOMARI_TEST_POSTGRES_URL=postgresql+asyncpg://komari_test@127.0.0.1:55458/komari_tsk279_resume
KOMARI_TEST_REDIS_URL=redis://127.0.0.1:56358/15
```

| 命令 | 结果 |
|------|------|
| `ruff check tests/komari_roulette/{tsk279_support.py,test_tsk279_runtime.py,test_tsk279_maintenance_pg.py,test_tsk279_observability.py}` | ✅ All checks passed |
| `pytest tests/komari_roulette/test_tsk279_runtime.py tests/komari_roulette/test_tsk279_maintenance_pg.py tests/komari_roulette/test_tsk279_observability.py -q`（带门控） | 6 passed / 23 failed |
| `pytest tests/komari_roulette/ -q`（带门控，全子套件） | ✅ 496 passed / 23 failed（失败全部为本阶段 RED） |

RED 失败原因分类（`--tb=line`）：

- `ModuleNotFoundError: ...komari_roulette.runtime` — 8 例
- `ModuleNotFoundError: ...komari_roulette.maintenance` — 9 例
- `ModuleNotFoundError: ...komari_roulette.observability` — 4 例
- `AssertionError`（现网 no-arg `send_gate` / `runtime_check` 未按调用） — 2 例

绿探针（全部真实符号，无 TSK-279 新模块）：

- `advance_expired` 跨两个错过窗口只淘汰旧当前一次、新窗口自 PG now 满 15min，
  且不新增收据/履约。
- 终局 projection 幂等、胜场不二次结算、`no_active_game` 不碰胜场。
- 作用域 advisory lock 经 `backend_pid`/`wait_for_blocked` 真实阻塞可观。
- 原始 SQL waiting fixture 被真实深入口推进（`waiting_game_expired`）。
- 四种真实终态（completed/cancelled/expired/failed）满足 CHECK/FK，
  `result_players ≥ 2`、运行时座位清空。
- 既有 `_safe_details` 允许清单丢弃 identity/body/secret/chamber/reward 键。

清理核验：用例结束后按前缀 `tsk279-%` 统计轮盘四表与 character_binding 群/成员
均为 0；门控库 `alembic_version` 仍为 `0021`，未降低 head。

## 10. Stage-B 返工：动态生效 / 关闭不 dispose / 并发准入隔离 / worker 并发 / 锁后复核

验收意见指出 Stage-B 的 6 绿 + 21 缺失接缝 RED + 2 无参回调断言 RED 虽成立，但若干
契约 AC 缺少**行为证据**。本返工**只加强既有 5 个 Stage-B 文件**的行为断言，并补一批
设计内 RED，把证据钉在真实 PG、真实 `RouletteCommandService.advance_expired`、真实
共享 `nonebot_plugin_orm` 引擎与真实 `asyncio` 并发交错上；仍不实现生产接缝。

### 10.1 新增/强化用例 → AC

| AC（Resolution） | 用例 | 性质 | 观察到的行为 |
|---|---|---|---|
| 动态关停即时重读（§2） | `runtime::test_dynamic_disable_flips_live_without_restart_or_tick` | RED（缺失模块） | 同一 runtime 实例在 `plugin_enable` True→False 后**当期**拒绝 business/send，无需重启、无需等 60s tick；恢复失败期间再打开不得 READY，直到一次成功 tick |
| 动态关停即时重读（真实 ConfigManager） | `runtime::test_real_config_manager_live_flip_denies_business_and_keeps_maintenance` | RED（缺失模块） | 经真实 `ConfigManager.update_field_async` 落库翻转后，下一次 `authorize` 立即 `plugin_disabled`；维护 tick 仍继续；既有 `waiting_expires_at` / `state_revision` 不变 |
| 真实 ConfigManager 读 API 探针 | `runtime::test_real_config_manager_read_api_supports_live_flip` | **绿探针** | `ConfigManager("komari_roulette", DynamicConfigSchema)` 的 `initialize_async` / `update_field_async` / `get` / `get_async` 确实支持 live 翻转（保证上一条 RED 的 fixture 不是臆测） |
| 关闭不 dispose 共享引擎（§2） | `runtime::test_close_blocks_new_dispatch_and_never_disposes_shared_engine` | RED（缺失模块） | 类级 `AsyncEngine.dispose` spy 证明首/次关闭都不 dispose 真实共享引擎（`orm.get_session()` 强制该引擎存在）；真实阻塞 recovery 被**有界取消**（`cancelled==1`、`finished==0`）；关闭后不再派发 |
| 已发未确认取消保持 PENDING 且不撤胜场（§2、§4） | `runtime::test_delivery_cancel_keeps_pending_and_wins_but_unsent_is_rejected` | **绿探针** | 真实终局收据：发送中途 `CancelledError` → 履约保持 `PENDING_CONFIRMATION`、已提交胜场保持 1；未触网即被 `runtime_check` 拒绝 → `NOT_DELIVERED` 且 0 次网络调用 |
| 按调用隔离准入与发送上下文（§5、§6） | `runtime::test_authority_is_per_call_isolated_between_concurrent_groups` | RED（缺失模块） | **同一实例**、`Barrier(2)` 交错的两组请求各自 101 允许 / 202 受限；撤销 101 只影响 101，无跨组 authority 泄漏 |
| 现网 no-arg `send_gate` 演进为按调用 | `runtime::test_handler_send_gate_receives_the_per_call_request` | RED（断言） | 真实 `RouletteQQHandler` 的 `SendGate` 必须收到本调用 `CommandRequest` |
| 同一实例并发 send gate 各自解析本请求 | `runtime::test_concurrent_send_gates_resolve_their_own_request` | RED（断言） | 真实 handler 两组并发经 `Barrier` 交错，一个撤销；gate 观测到各自 `request`，投递结果各归其组/msgid |
| 现网 no-arg `runtime_check` 演进为按调用 | `runtime::test_delivery_runtime_check_receives_the_per_call_receipt` | RED（断言） | 真实 delivery 的 `RuntimeCheck` 收到本调用 `CommandReceipt`；`Barrier` 交错下 DELIVERED 载荷属允许组、`msg_id == receipt.inbound_msg_id`、`msg_seq == 1`、正文为冻结体 |
| 两个 worker 竞争同一过期期限只推进一次（§2） | `maintenance::test_two_maintenance_instances_advance_one_turn_exactly_once` | RED（缺失模块） | 两个真实 `RouletteMaintenance` 实例经 `Barrier` 并发同一组；真实 PG 群锁下只淘汰旧当前一次、新当前自 PG now 满 15min、`advanced` 合计 1 |
| 终局胜负只结算一次（§2、§3） | `maintenance::test_two_maintenance_instances_settle_terminal_wins_once` | RED（缺失模块） | 两个 worker 并发结算同一终局：`wins==1`、results==1、座位==2，不双加胜场 |
| 期限优先：命令先持锁时过期的 forfeit 结算为 timeout（§2） | `maintenance::test_expired_deadline_beats_forfeit_when_command_runs_first` | **定向编排（非竞速）** | 已过期回合收到真实 `forfeit`：`turn_expired` + `reason=timeout`，绝不 `forfeited`；恰好 1 条 completed 结果、两座位、`wins==1` |
| 维护先结算后迟到命令见 `no_active_game`（§2） | `maintenance::test_maintenance_first_settles_timeout_then_command_sees_no_active_game` | **定向编排（非竞速）** | 同一过期期限先由真实 `advance_due` 结算 `timeout`；迟到命令只观察到 `no_active_game`（或观测边界 `state_conflict`），终局不被改写成 `forfeited` |
| 锁后复核准入：等待期间转受限即不推进（§2、§5） | `maintenance::test_maintenance_rechecks_admission_after_group_lock_wait` | RED（缺失模块） | 群锁被外部持有、worker 真实阻塞在锁上（`backend_pid`/`wait_for_blocked`）期间把该群转受限；放锁后不得推进，局保持 `waiting`、`advanced==0` |
| 锁后复核运行时关闭：关闭后不得推进（§2） | `maintenance::test_maintenance_rechecks_closed_runtime_after_group_lock_wait` | RED（缺失模块） | 阻塞期间真实 `RouletteRuntime.close()`；放锁后不得推进；maintenance 用真实 `orm.get_session` 工厂 |
| 两玩家超时终局确为胜场（fixture 探针） | `maintenance::test_two_player_expiry_settles_one_win` | **绿探针** | 真实 `advance_expired` 对两玩家过期回合 → `completed` + `wins==1`（保证并发结算用例的 fixture 成立） |

### 10.2 拟议追加接缝（返工新增）

```python
# 1) 每个调用重新读取实时配置，不缓存启动快照
class RouletteRuntime:
    def authorize(self, *, scope: str, group_ids: Sequence[int], token=None) -> RouletteAuthority:
        # 每次调用读取 ConfigManager.get()/get_async()（与真实 read API 同形）；
        # plugin_enable=False → plugin_disabled，当期生效，不依赖 60s tick。
        ...
    def get_state(self) -> RouletteRuntimeState:
        # 恢复失败期间即使 plugin_enable 翻回 True 也不得 READY；
        # 只有一次成功 recovery tick 后才能 READY。
        ...

# 2) 深服务锁后效果复核 seam（不提供 always-true 默认）
async def RouletteCommandService.advance_expired(
    self,
    group: GroupScope,
    *,
    observation: Observation | None = None,
    effect_check: Callable[[], bool] | None = None,   # 在取得群 advisory 锁之后、提交之前求值
) -> AdvanceResult: ...

# 3) 维护每个到期群把「群准入 × 运行时 accepting」组合成锁后复核，而非只在扫描前判一次
class RouletteMaintenance:
    async def advance_due(self, *, batch_size: int = 100) -> RecoveryTickResult:
        # 对每个候选群：先粗判 admission，进入真实 advance 后由 effect_check 在锁内再判；
        # 期间转受限 / runtime 关闭都必须放弃推进。
        # 注意：这里的「runtime accepting」只是 **shutdown / 依赖未就绪** 的可控端口，
        # 绝不是 business `plugin_enable`：业务开关为 False 时维护仍必须运行。
        ...

# 4) 现网回调按调用签名（destructive change）
SendGate = Callable[[CommandRequest], bool | Awaitable[bool]]
RuntimeCheck = Callable[[CommandReceipt], bool | Awaitable[bool]]
```

约束：`effect_check` 为 `None` 表示调用方**未要求**锁后复核（不是 always-true）；
需要准入的生产调用必须显式传入，实现不得内置「永远返回 True」的兜底。

> **维护准入端口澄清（Stage-C 检查组合）**：`test_tsk279_maintenance_pg.py::
> test_maintenance_rechecks_closed_runtime_after_group_lock_wait` 把 `admission`
> 写成 `runtime.accepting and (app, group)`，**只是该用例制造「worker 等锁期间
> runtime 关闭」这一 True→close 转移的可控端口**，绝不是生产 maintenance 接线
> 建议。生产维护只能受 **shutdown / 依赖 ready / 群准入** 控制，不得用 business
> `accepting`（即 `plugin_enable`）一刀切：`plugin_enable=false` 时维护仍必须继续
> 推进绝对期限。Stage-C 必须验证「shutdown × 依赖 ready × 群准入」的组合，禁止把
> 该用例的端口当成生产 authority。

### 10.3 返工后 RED / 绿分类（本阶段同一命令测得）

| 指标 | 修复前 | 返工后 |
|------|--------|--------|
| Stage-B 三文件合计 | 6 passed / 23 failed | **9 passed / 31 failed** |
| 绿探针 | 5 maintenance + 1 observability | 6 maintenance（含两玩家终局）+ 2 runtime（真实 ConfigManager 读 API、delivery 取消 PENDING）+ 1 observability |
| 缺失接缝 RED | 21（`ModuleNotFoundError`） | 28（runtime 10 / maintenance 14 / observability 4） |
| 断言 RED（按调用回调） | 2 | 3（handler gate、并发 handler gate、delivery runtime_check） |

新增的 8 个 RED 全部为设计内缺失接缝（`ModuleNotFoundError`），无「整文件 collection
error」，无用例自身 fixture 造假的断言失败。

### 10.4 返工执行记录（命令日志）

环境：worktree `/Users/derbay32/project/komari-bot/.agents/worktrees/tsk-279`，
branch `pi/TSK-279-runtime-recovery`，基线 HEAD `61b91b8`（base `bc4b1cc`），root
venv `/Users/derbay32/project/komari-bot/.venv/bin/python`（3.13.11）。PG/Redis 门控：

```
SQLALCHEMY_DATABASE_URL=postgresql+asyncpg://komari_test@127.0.0.1:55458/komari_tsk279_resume
KOMARI_TEST_POSTGRES_URL=postgresql+asyncpg://komari_test@127.0.0.1:55458/komari_tsk279_resume
KOMARI_TEST_REDIS_URL=redis://127.0.0.1:56358/15
```

| 命令 | 结果 |
|------|------|
| `ruff check tests/komari_roulette/` | ✅ All checks passed |
| `pytest .../test_tsk279_runtime.py -q`（带门控） | 2 passed / 13 failed |
| `pytest .../test_tsk279_maintenance_pg.py -q`（带门控） | 6 passed / 14 failed |
| `pytest .../test_tsk279_{runtime,maintenance_pg,observability}.py -q`（带门控） | 9 passed / 31 failed |

RED 失败原因（`--tb=line`）：

- `ModuleNotFoundError: ...komari_roulette.runtime` — 10 例
- `ModuleNotFoundError: ...komari_roulette.maintenance` — 14 例
- `ModuleNotFoundError: ...komari_roulette.observability` — 4 例
- 按调用回调断言（`AssertionError` / `DeliveryOutcome`） — 3 例

绿探针补充证据：真实 `ConfigManager` live 翻转读 API；真实终局发送中途取消 →
`PENDING_CONFIRMATION` + `wins` 不撤、未触网 → `NOT_DELIVERED`；两玩家过期回合 →
`completed` + `wins==1`。

仍未覆盖（Stage-C 或本票后续）：真实 `on_startup`/`on_shutdown` 装配、真实
`group_admission` 群→member 解析、真实 `user_ban` 叠加、真实调度单例注册、真实保留
策略常量、`SendGate`/`RuntimeCheck` 生产改签名、REST/管理面、Alembic 迁移与 `check`
零 diff。

### 10.5 B1 独立返工：测试自错误修复 / 分页加固 / 期限优先定向编排 / 锁后 effect_check

> §10.3 / §10.4 记录上一轮基线（当时 maintenance 接缝尚缺）。本轮基线 HEAD
> `39bf7ea` 上 maintenance 接缝已存在，维护侧用例全部转绿；剩余红见 (f)。

本轮只改 Stage-B 5 个文件中的测试（实际改动 `test_tsk279_maintenance_pg.py`、
`test_tsk279_runtime.py` 与本合同），不碰生产代码。

**（a）已证实的测试自错误（修复前必红，修复后转绿）**

| 自错误 | 位置 | 真实契约 | 修复 |
|---|---|---|---|
| `str(cleanup.trigger.fields[5])` 被断言为 `"hour='4'"` | `test_maintenance_jobs_registered_with_throttle_and_deploy_timezone` | APScheduler `CronTrigger.fields[5]` 的 `str()` 是裸值 `"4"` | 断言 `fields[5] == "4"`、`fields[6] == "0"` + `get_next_fire_time` 落在部署时区 04:00 + `trigger.timezone` |
| `ON r.result_id = rp.result_id` | `test_two_maintenance_instances_settle_terminal_wins_once` | 真实 ORM 结果表主键/FK 是 `game_id`（`komari_roulette_result_players(game_id, join_seq)`） | 改为 `r.game_id = rp.game_id` |
| 结果列写作 `completion_reason` | 旧 `test_command_action_and_maintenance_locked_race_commits_one_effect` | 结果真实列是 `reason` 与 `lifecycle` | 用例重写为 (c) 的定向编排，统一经 `_terminal_projection_state` 读 `lifecycle` / `reason` |

**（b）全表扫描用例的分页加固（TSK-278/280 残渣）**

门控库实测残留 264 条 tsk276/tsk280 终局与 724 条收据（`due now: 0`、无过期残渣），
因此 `test_recovery_*` / `test_cleanup_*` 不再假设本用例候选落在前 100 行：

- `_recovery_page_budget` / `_cleanup_round_budget` 以**真实全局 due/aged 计数**推导
  轮数上限（不删外部行、不把生产 SQL 收窄到本 scope）；
- `_drive_recovery_until_settled` 由生产 `cursor` / `scanned` 驱动，允许跨过整页受限群
  及无关行，直到本组 settle 或游标走空；
- `_drive_cleanup_until` 同理按真实 aged 计数有界重入 `cleanup_retention`。

**（c）期限优先：两个受控顺序，替代 Barrier 竞速**

源确认 `advance_expired` 顶部对 `active and _is_expired` 先走 `_expire_active` →
`_eliminate_current(completion_reason="timeout", code="turn_expired", ok=False)`；
`_load_current_rows` 只认 `lifecycle IN ('waiting','active')`。故对同一过期期限：

- 命令先持锁 → `turn_expired`、`reason=timeout`，**绝不** `forfeited`；
- 维护先结算 → 迟到命令只得到 `no_active_game`（或观测边界 `state_conflict`）。

两个用例各只跑一种真实顺序（无 `Barrier` 多次重跑），统一由
`_assert_single_timeout_terminal` 断言：恰好 1 条 completed 结果、`reason=timeout`、
`wins==1`、两座位、`game_lifecycle=completed`。

**（d）锁后 `effect_check` 真实常驻服务用例**

直接驱动真实 `RouletteCommandService.advance_expired(group, effect_check=...)`
（真实 PG 群锁 + 真实 storage），断言零副作用：

| 用例 | gate | 期望 |
|---|---|---|
| `test_advance_expired_rejects_sync_gate_with_zero_mutation` | `lambda: False` | `effect_check_rejected`、`changed=False`、`_game_snapshot` 前后逐字段相等 |
| `test_advance_expired_rejects_async_gate_with_zero_mutation` | `async def gate() -> False` | 同上（awaitable 拒绝同样 fail-closed） |
| `test_advance_expired_gate_exception_fails_closed_zero_mutation` | `def gate(): raise` | 同上（`_run_effect_check` 捕获后返回 False，绝不当作放行） |
| `test_advance_expired_accepts_true_gate_and_advances` | `lambda: True` | gate 恰好调用 1 次、`changed=True`、`waiting_game_expired`、局转 `expired` |
| `test_advance_expired_runs_gate_only_after_group_lock_is_held` | 真实阻塞在群锁上 | 外部持锁、worker 真阻塞（`backend_pid`/`wait_for_blocked`）期间 `gate_calls==0`；放锁后 `==1` 并成功推进——证明复核确在锁后 |

**（e）runtime RED：`failed>0` 的 tick 不得 READY**

`RecordingRecovery` / `BlockingRecovery`（runtime）与 `_NoopRecovery`（maintenance）
现在返回真实 `RecoveryTickResult`（不再 `object` / 裸 `SimpleNamespace`），并新增
`runtime::test_recovery_tick_with_failures_does_not_report_ready`：tick 返回 `failed=1`
时 `status=FAILED`、`reason_code="recovery_failed"`、`recovery_completed=False`、
`accepting=False`；紧随的失败-free tick 才恢复 READY。既有 init /
disable-doesn't-pause / close AC 不变。

**（f）本轮执行记录**

| 命令 | 结果 |
|------|------|
| `ruff check tests/komari_roulette/` | ✅ All checks passed |
| `pytest .../test_tsk279_maintenance_pg.py -q`（带门控） | 25 passed / 1 failed |
| `pytest .../test_tsk279_runtime.py -q`（带门控） | 2 passed / 14 failed |
| `pytest .../test_tsk279_observability.py -q`（带门控） | 5 passed |
| `pytest` 三文件合计 | 32 passed / 15 failed |
| `pyright --pythonpath <root venv python>` | 4 errors（全部为既有按调用回调的签名不匹配 RED） |

剩余 15 红：12 例 `ModuleNotFoundError: ...roulette.runtime`（Stage-C runtime 接缝
未实现，含本轮新增 `failed>0` 用例）+ 3 例既有按调用回调断言 RED（`SendGate` /
`RuntimeCheck` 生产签名未演进）。维护侧（恢复分页、清理边界、双实例并发、锁后复核、
期限优先定向编排、`effect_check`）**全绿**。

## 11. Stage-B 最后一组审查 RED（B2 实现前的整合缺口）

依据验收意见（observability F4/F5/F6 已证生产缺陷 + B1 单批函数正确但每日 cron
无 drain 的实际整合缺口），本轮**只改 Stage-B 测试与合同**，新增 6 个以真实行为断言
失败的 RED，把 B2 必须修的生产行为钉死；不实现任何生产接缝，不碰生产/其它分支。

### 11.1 新增用例 → 义务

| # | 义务 | 用例 | 性质 |
|---|------|------|------|
| 1 | `set_runtime_state` 的未知 `reason_code`（含身份/正文哨兵）必须归一为**固定闭集**成员，绝不原样进入 `runtime_reason`/`as_dict()`；合法 `ready/disabled/failed` 状态与 `plugin_disabled` / `policy_restricted` / `policy_admitted` / `not_ready` 原因保持有效语义且同属**共用**闭集（`OBSERVATION_REASON_CODES`），不新增会漂移的第二套定义 | `observability::test_runtime_reason_is_normalized_into_the_shared_closed_set` | RED（断言） |
| 2 | `refresh_pending` 的真实 sessionFactory 读取异常：`pending_receipts` 保持**未知 None**（不是伪 `0`，也不是上次成功值），故障按 `pending_unavailable` 聚合，绝不误分类为 `recovery_failed`；原始 `RuntimeError` 正文/身份哨兵不得进入投影 | `observability::test_pending_read_failure_is_unknown_pending_unavailable_and_never_leaks` | RED（断言） |
| 3 | `snapshot()` 的 `latest_scan` / `latest_cleanup` 与 `as_dict()` 不外泄内部可变字典：改返回投影或改传给 `note_*` 的记录都不能篡改后续 snapshot（只读 `TypeError` 或独立 copy 均可，不钉具体 mapping 类型） | `observability::test_snapshot_projection_isolates_internal_mutable_dicts` | RED（断言） |
| 4a | 清理公平（7d 收据）：两个真实 scope、同一 app；排序靠前忙组每轮补满 ≥batch 的 7d 合格收据，后组一条合格；按真实候选组数有界轮次内后组必须被清理，预算不得每轮全给首组而永久饥饿 | `maintenance::test_cleanup_fairness_reaches_later_receipt_group` | RED（断言） |
| 4b | 清理公平（30d 终局）同机制，且 `completed` 长期留存；`waiting`/`active` 与 completed 结果/座位/胜场绝不删 | `maintenance::test_cleanup_fairness_reaches_later_terminal_group_and_keeps_completed` | RED（断言） |
| 5 | 真实每日 cron 回调**一次运行**消费 >1 个 batch 的积压（201 条合格收据），而单个 `cleanup_retention(batch=100)` 仍有界；从 `scheduler.get_job(CLEANUP_JOB_ID).func` 取真实回调并调用（黑盒行为，非函数名断言），业务上验证 7d 清理 | `maintenance::test_daily_cleanup_callback_drains_backlog_beyond_one_batch` | RED（断言） |

### 11.2 B2 义务（本轮不实现）

- **F4 归一**：observability 的共用闭集须同时容纳故障码与运行时原因码
  （`plugin_disabled` / `policy_restricted` / `policy_admitted` / `not_ready`）；
  未知 reason 归一到该闭集固定成员，禁止 raw passthrough。
- **F5 故障归因**：`refresh_pending` 读失败置 `pending_receipts=None` 并聚合
  `pending_unavailable`，不得落回 `RuntimeError → recovery_failed` 的通用映射。
- **F6 不可变投影**：`snapshot()` 字段与 `as_dict()` 的嵌套计数 dict 必须拷贝或
  只读；`as_dict()` 仍须 JSON-safe（既有 `json.dumps` 用例不得回归）。
- **清理公平**：`cleanup_retention(batch=N)` 单轮有界，但不得让排序首组吃满全部
  预算；多轮内必须推进到后续合格组（7d 与 30d 分组同机制）。
- **每日 drain**：04:00 注册的回调必须在单次运行内循环消费 `more_pending` 的分批
  积压（具体 drain 方法名由 B2 自定），且 `cleanup_retention(batch=N)` 保持单批有界。
- **可取消 / close 停止**：drain 回调必须可被取消、runtime close 后停止；此生命周期
  断言**复用 B2 runtime 生命周期用例**（见 §10.2 第 1/3 点），本阶段不重复。

### 11.3 根裁决遵守

- **F3（failed 计数吞异常）**：保留 maintenance 的 per-group 聚合与
  “一个坏组不 abort 整页”；只要求 `failed>0` 的 tick 不得 READY（§10.5(e) 既有 RED）
  且原因观测正确。不新增“worker 必须整页 abort”的义务。
- 不新增无意义的 `batch_size<=0` 游标限制。
- 根接受 service 锁后重读 PG 时间的既有深逻辑；不把未证的 stale observation 猜测
  写成强制改动。

### 11.4 失败证据（本轮实测，均为业务断言，非缺 module）

| 用例 | 实测首断言失败 |
|------|----------------|
| `test_runtime_reason_is_normalized_into_the_shared_closed_set` | `AssertionError: assert 'plugin_disabled' in frozenset({'admission_unavailable', 'cleanup_failed', 'config_unavailable', 'pending_unavailable', 'recovery_failed', 'runtime_failed', ...})` |
| `test_pending_read_failure_is_unknown_pending_unavailable_and_never_leaks` | `assert None == 1` where `{'recovery_failed': 1}.get('pending_unavailable')` |
| `test_snapshot_projection_isolates_internal_mutable_dicts` | `{'scanned': 999, 'advanced': 999, ...} != {'scanned': 3, 'advanced': 1, ...}` |
| `test_cleanup_fairness_reaches_later_receipt_group` | `AssertionError: the later group starved ...`（`cleaned is False`） |
| `test_cleanup_fairness_reaches_later_terminal_group_and_keeps_completed` | `AssertionError: the later terminal group starved ...`（`cleaned is False`） |
| `test_daily_cleanup_callback_drains_backlog_beyond_one_batch` | `assert 1 == 0`（cron 一次仅清一批，留 1 条） |

新增 RED 全部为设计内生产行为缺口；无整文件 collection error，无用例自身 fixture
造假。既有 `test_maintenance_rechecks_closed_runtime_after_group_lock_wait`（缺
`runtime.py`）保持原 RED，不计入本轮。

### 11.5 执行记录（命令日志）

环境：worktree `/Users/derbay32/project/komari-bot/.agents/worktrees/tsk-279`，
branch `pi/TSK-279-runtime-recovery`，HEAD `254c4cb`（base `bc4b1cc`），root venv
`/Users/derbay32/project/komari-bot/.venv/bin/python`（3.13.11）。PG/Redis 门控：

```
SQLALCHEMY_DATABASE_URL=postgresql+asyncpg://komari_test@127.0.0.1:55458/komari_tsk279_resume
KOMARI_TEST_POSTGRES_URL=postgresql+asyncpg://komari_test@127.0.0.1:55458/komari_tsk279_resume
KOMARI_TEST_REDIS_URL=redis://127.0.0.1:56358/15
```

| 命令 | 结果 |
|------|------|
| `ruff check tests/komari_roulette/` | ✅ All checks passed |
| `pytest .../test_tsk279_observability.py -q`（带门控，改前基线） | 5 passed |
| `pytest .../test_tsk279_observability.py -q`（带门控，改后） | 5 passed / 3 failed（新增 RED） |
| `pytest .../test_tsk279_maintenance_pg.py -q`（带门控，改前基线） | 25 passed / 1 failed |
| `pytest .../test_tsk279_maintenance_pg.py -q`（带门控，改后） | 25 passed / 4 failed（3 新增 RED + 原 runtime 缺模块 RED） |
| `pytest` 三文件合计（改前 → 改后） | 32 passed / 15 failed → 32 passed / 21 failed（+6 设计内 RED） |
| `pytest tests/komari_roulette/ -q`（带门控） | 522 passed / 21 failed（既有 522 全绿无回归） |
| `pyright --pythonpath /Users/derbay32/project/komari-bot/.venv/bin/python` | 4 errors，全部为既有 `SendGate`/`RuntimeCheck` 按调用签名 RED；新增用例 0 报错 |

清理核验：用例结束后 `app_id like '%tsk279%'` 的收据/履约/对局均为 0，7d/30d 合格
残渣计数均为 0；门控库 `alembic_version` 仍为 `0021`。库内其余 `tsk276`/`tsk280`
行属其它套件既有残留，未手删、未被本票用例依赖为首屏。

## 12. Stage-B 验收卫生收尾：per-case scope 追踪 / 锁后准入门禁抗污染

依据验收意见：Stage-B 维护锁用例的「自身污染」不是预存在问题，必须在本票内修掉；
旧 TSK-278 两个 harness 的 `delete_scope(scope("fixture"))` 与实际用例 scope 不匹配，
会让每例真实的 waiting/active/terminal 行残留到共享门控库。本轮**只改测试与
fixture 清理**：不改业务断言、不给生产加 tests 专用 app 过滤、不删外来数据、
不新增产品 AC。

### 12.1 旧 TSK-278 harness：按真实用例 scope 逐个清理

`test_tsk278_delivery_pg.py` / `test_tsk278_fixture_probe.py` 的 `harness` fixture
不再删除一个从未被使用的共享 scope，而是以 fixture-scoped `monkeypatch` 替换本
模块的 `scope` 函数：每次真实创建都记录返回值，`finally` 对每个记录到的 scope
调用既有 `delete_scope`（本 case 轮盘行）与既有 `delete_binding_scope`（本 case
的 character_binding 群/成员行）。只删本 case 记录到的 scope：不做「测试前全库
清零」、不按前缀批量删除、不清其它用例数据；`harness` yield 的
`(engine, session_factory, manager)` 业务接口与跨文件默认 `scope` 都不变。

### 12.2 锁后准入用例：只授权 own scope + 持久 foreign due fixture

`test_maintenance_rechecks_admission_after_group_lock_wait`：

- `admission` 端口从「对所有 app/group 返回 True」改为
  `gate["allowed"] and (app, group) == (own app, own group)`：外来 scope 一律明确
  `False`，本用例只可能推进/撤销自己这一组，绝不推进外来 group。
- 用 `harness.scope("post-lock-foreign")` 显式创建一个由本 case 最终删除的
  foreign due scope（deadline 1 小时前，早于 own 的 60 秒前），作为**持久回归
  fixture**：worker 必须经真实 keyset 分页跨过它才能到达被锁的 own 组。
- 以 `batch_size=1` 真实分页（首页 foreign、次页 own），真实
  `wait_for_blocked` 证明 worker 确已阻塞在 own 群锁上；阻塞期间撤销 own 门禁，
  放锁后 own 仍 `waiting`、整局快照逐字段不变；foreign 快照逐字段不变；
  `drive.advanced == 0`、`drive.skipped_restricted >= 1`、`drive.pages >= 2`。
  不 sleep 15 分钟，直接以 PG 写入 past deadline。

close-runtime 锁用例既有的 own scope 匹配保持不变。

### 12.3 执行记录（命令日志）

环境：worktree `/Users/derbay32/project/komari-bot/.agents/worktrees/tsk-279`，
branch `pi/TSK-279-runtime-recovery`，基线 HEAD `77045d2`，root venv
`/Users/derbay32/project/komari-bot/.venv/bin/python`（3.13.11）。PG/Redis 门控：

```
SQLALCHEMY_DATABASE_URL=postgresql+asyncpg://komari_test@127.0.0.1:55458/komari_tsk279_resume
KOMARI_TEST_POSTGRES_URL=postgresql+asyncpg://komari_test@127.0.0.1:55458/komari_tsk279_resume
KOMARI_TEST_REDIS_URL=redis://127.0.0.1:56358/15
```

| 命令 | 结果 |
|------|------|
| `ruff check tests/komari_roulette/` | ✅ All checks passed |
| `ruff check .` | ✅ All checks passed |
| `pytest .../test_tsk278_delivery_pg.py .../test_tsk278_fixture_probe.py -q`（带门控） | ✅ 23 passed |
| `pytest .../test_tsk279_maintenance_pg.py -q`（带门控，连跑 3 次） | ✅ 29 passed ×3 |
| `pytest tests/komari_roulette/ -q`（带门控，正常顺序、不 deselect） | ✅ 552 passed |
| `pyright --pythonpath /Users/derbay32/project/komari-bot/.venv/bin/python` | ✅ 0 errors, 0 warnings, 0 informations |

清理核验：先记录 UTC mark，再跑两个旧 file，随后按
`created_at > mark` / `updated_at > mark` 统计本轮 `tsk276-app-tsk278-%`：轮盘
games / command_receipts / fulfillments / results / leaderboard 与
character_binding members / groups 全部为 0；`members_total` / `groups_total`
前后不变（未动其它套件的历史残留）。全 roulette 套件结束后
`lifecycle IN ('waiting','active')` 为 0，`tsk279-post-lock-foreign` /
`post-lock-admission` 行均为 0；`alembic_version` 仍为 `0021`。

## 13. Stage-C1：真实组合根（lifecycle）+ 锁后复核 + 发出最终裁决（本阶段）

本阶段把 TSK-269 Resolution §2–§5 的**真实装配**钉在真实 Driver lifespan、真实
`nonebot_plugin_orm` 连接、真实 `ConfigManager` 注册表、真实
`RouletteCommandService`/`RouletteDelivery` 上。新增文件：

- `test_tsk279_lifecycle.py`（seam / 真实 driver hook / QQ 安装 / 定时任务 /
  start-stop 幂等 / 不 dispose 共享引擎）；
- `test_tsk279_lifecycle_pg.py`（真实配置注入到已安装服务 + 冻结事实 + 已安装
  cron 多批 drain + 维护准入 canonical 数字群 + maintenance close 有界）；
- `test_tsk279_effect_recheck_pg.py`（`execute_group_command` 锁后 `effect_check`
  + handler 逐调用 closure + delivery claim 后最终裁决）；
- `tsk279_lifecycle_support.py`（真实 Driver lifespan + `FakeScheduler` + 录制
  `get_config_manager`）。

生产模块 `komari_bot.plugins.komari_roulette.lifecycle`、`app` 对象、锁后
`effect_check` 与 `EffectCheckRejectedError` 尚不存在，RED 由 `application_api`
懒加载（`ModuleNotFoundError`/`AttributeError`）或生产签名缺参
（`TypeError`/`reportCallIssue`）触发；`maintenance.close` 缺界为真实的业务断言
RED（生产模块已存在）。两者都是设计内 RED，不是 fixture 造假。

### 13.1 拟议最窄公共接缝

```python
# komari_bot/plugins/komari_roulette/lifecycle.py
APPLICATION_FUNCTIONS = (
    "start_roulette_application", "stop_roulette_application",
    "get_roulette_application",
)
# 包 __init__ 必须 `from . import lifecycle`，使包 reload 恰注册
# 一个 driver.on_startup(_startup) + 一个 driver.on_shutdown(_shutdown)；
# 两个 hook 的 __module__ 前缀必须是 komari_bot.plugins.komari_roulette。

@dataclass(frozen=True, slots=True)
class RouletteApplication:
    runtime: RouletteRuntime            # 必暴露
    service: RouletteCommandService     # 真实冻结/投影装配
    maintenance: RouletteMaintenance
    config_manager: ConfigManager        # 顶层注册表 getter 返回值 is 同一实例

async def start_roulette_application() -> RouletteApplication   # 单飞；重复调用不再取 manager/不再登记 job
async def stop_roulette_application() -> None                   # 幂等
def get_roulette_application() -> RouletteApplication | None
```

装配顺序（AC1，故障关闭）：共享 ORM 已托管 → 顶层唯一
`get_config_manager("komari_roulette", DynamicConfigSchema)` 恰一次 →
binding/admission → maintenance（真实 session factory + 真实 service + canonical
群准入）→ `runtime.start()`（config → recovery tick → READY/DISABLED/FAILED）→
`install_roulette_qq_runtime(service=…, business_gate=…, runtime_check=…,
send_gate=…)` → `scheduler.add_job` 恰 `RECOVERY_JOB_ID` + `CLEANUP_JOB_ID`。
运行时不新建私有 engine、不执行 DDL。

```python
# komari_bot/plugins/komari_roulette/command_service.py
class EffectCheckRejectedError(RuntimeError): ...   # 专用安全拒绝控制流

# 语义：锁已持有、幂等重放已排除、任何写入之前评估；False / gate 抛错 →
# EffectCheckRejectedError，零 receipt / 零状态变更 / 零 fulfillment；
# storage / domain 错误照旧上抛（`_run_effect_check` 只把 gate 自身失败读作拒绝）。
async def execute_group_command(
    self, request: CommandRequest, *,
    observation: Observation | None = None,
    effect_check: EffectCheck | None = None,   # 无 always-true 默认
) -> CommandReceipt

# komari_bot/plugins/komari_roulette/qq/handler.py
# 每次 handle 构造一个 closure，闭包捕获**原始** (bot, event, token)；
# 它同时传给执行前和 claim 后两处，不出现进程级 current event、不从 receipt
# 重新铸造凭证、不跨消息复用。
await self._service.execute_group_command(request, observation=…, effect_check=closure)
await self._delivery.deliver(receipt, bot, effect_check=closure)
# EffectCheckRejectedError 被 handler 静默吸收（不发送、不报错、不建 receipt）。

# komari_bot/plugins/komari_roulette/qq/delivery.py
async def deliver(
    self, receipt: CommandReceipt, sender: QQMessageSender,
    *, effect_check: EffectCheck | None = None,
) -> DeliveryOutcome
# claim 之后、网络之前评估；False/抛错 → NOT_DELIVERED + mark_not_delivered，
# 零网络；CancelledError 仍按既有语义传播（PENDING，不重发）；
# 原有 runtime_check(receipt) 与 DB 时钟窗口重核仍是必经裁决。

# komari_bot/plugins/komari_roulette/maintenance.py
async def close(self) -> None
# 置 _stopped 后必须（有界）等待在途 advance_due / cleanup_retention 轮次结束，
# 不能在轮次仍持组锁运行时立即返回。
```

### 13.2 AC → 用例 → 冻结断言

| AC | 用例 | 断言性质 |
|---|---|---|
| 真实 driver 恰一个 startup/shutdown hook | `lifecycle::test_package_import_registers_single_driver_hooks` | 缺 seam RED |
| seam 暴露三个 application 函数 | `lifecycle::test_lifecycle_seam_exposes_application_functions` | 缺 seam RED |
| 启动装 QQ 运行时 + 只登记 owned job + 幂等 | `lifecycle::test_start_installs_qq_runtime_and_registers_owned_jobs_once` | 缺 seam RED |
| 60s recovery job 回调走 `Runtime.run_recovery_tick` | `lifecycle::test_recovery_job_func_reaches_runtime_state_machine` | 缺 seam RED |
| `plugin_enable=false` 不动维护、不授权业务 | `lifecycle::test_business_disabled_keeps_maintenance_and_never_allows` | 缺 seam RED |
| shutdown 先移 owned job + 清 QQ 分发、幂等 | `lifecycle::test_stop_removes_owned_jobs_and_clears_qq_runtime` | 缺 seam RED |
| shutdown 不 dispose 共享 ORM 引擎 | `lifecycle::test_stop_never_disposes_shared_orm_engine` | 缺 seam RED |
| shutdown 不误删其它插件 job | `lifecycle::test_stop_does_not_remove_foreign_jobs` | 缺 seam RED |
| 配置获取失败 → 安全 failed/notready、不装 QQ、注册受控恢复入口；依赖恢复后经周期入口转 READY 并装 QQ | `lifecycle::test_start_config_acquisition_failure_is_safe_and_recovers` | 缺 seam RED |
| owned recovery 阻塞在组锁时 stop → 有界收尾、移除 owned job、foreign job 不动、零推进 | `lifecycle::test_stop_while_owned_recovery_waits_on_group_lock_settles` | 缺 seam RED |
| 启动阻塞于 config.initialize 时 stop → 不后置安装 QQ/不留 owned job | `lifecycle::test_stop_during_blocking_config_initialize_never_installs_later` | 缺 seam RED |
| 已安装 service 读**实时**真实配置权重 + 开局冻结 | `lifecycle_pg::test_installed_service_reads_live_real_item_weights_and_freezes_game` | 缺 seam RED |
| 已安装 projector 读**实时**真实文案池 + receipt 冻结 | `lifecycle_pg::test_installed_service_reads_live_real_copy_pool_and_freezes_receipt` | 缺 seam RED |
| 已安装 04:00 cron 一次 run 消费多批积压 | `lifecycle_pg::test_installed_cron_callback_drains_multi_batch_backlog` | 缺 seam RED |
| 维护准入 canonical app/group→数字群 + 真实 admission，绝不用业务开关 | `lifecycle_pg::test_maintenance_admission_is_canonical_and_ignores_business_switch` | 缺 seam RED（驱动真实 `advance_due`，不再读私有 `_admission`） |
| `maintenance.close` 有界等待在途清理轮次 | `lifecycle_pg::test_maintenance_close_waits_for_the_in_flight_cleanup_round` | **业务断言 RED** |
| stop 后无 roulette job | `lifecycle_pg::test_stop_after_start_leaves_no_roulette_jobs` | 缺 seam RED |
| `execute_group_command` 接受 per-call `effect_check` | `effect::test_execute_group_command_exposes_effect_check_keyword` | 缺签名 RED |
| gate False → 零 receipt/状态/fulfillment，专用安全拒绝 | `effect::test_effect_check_rejection_leaves_zero_effects` | 缺签名 RED |
| gate 仅在组锁持有后评估 | `effect::test_effect_check_is_evaluated_only_after_the_group_lock` | 缺签名 RED |
| gate True 不吞 storage 错误 | `effect::test_effect_check_true_still_propagates_storage_errors` | 缺签名 RED |
| handler 把原始 token closure 传给 service + delivery | `effect::test_handler_passes_original_token_closure_to_service_and_delivery` | 业务断言 RED |
| handler 静默吸收 effect 拒绝 | `effect::test_handler_silently_absorbs_effect_rejection` | 缺符号 RED |
| 两并发组不互借 token | `effect::test_concurrent_handlers_keep_their_own_token_in_the_closure` | 业务断言 RED |
| `deliver` 接受 per-call `effect_check` | `effect::test_delivery_exposes_per_call_effect_check_keyword` | 缺签名 RED |
| claim 后 False → NOT_DELIVERED + 零网络 | `effect::test_delivery_post_claim_check_blocks_network_as_not_delivered` | 缺签名 RED |
| 锁内等待期间撤权 → 拒绝且不推进 | `effect::test_delivery_variant_effect_rejected_after_lock_wait` | 缺签名 RED |
| 旧 API 权威链 mint/verify/ban/remap/policy 同一 token 放行后逐一拒绝 | `effect::test_old_api_authority_chain_allows_then_rejects_the_same_token` | **GREEN 探针（不依赖 lifecycle）** |
| 真实 delivery `runtime_check` 通过但 PG 窗口过期 → NOT_DELIVERED 零网络 | `effect::test_real_delivery_true_runtime_check_with_expired_pg_window_never_sends` | **GREEN 探针（不依赖 lifecycle）** |
| 已接受的 claim 后 `effect_check` 不得绕过 PG 窗口 | `effect::test_real_delivery_accepted_effect_check_still_honours_expired_window` | 缺签名 RED |
| 已安装真实 handler/service 提交有效 token 并产出真实 SDK 载荷 | `effect::test_installed_matcher_commits_valid_token_and_captures_real_sdk_payload` | **GREEN 探针（不依赖 lifecycle）** |
| 已安装真实 handler 的 business gate 对 ban/remap/policy 逐一拒绝同一 token | `effect::test_installed_matcher_business_gate_reads_live_authority_for_same_token` | **GREEN 探针（不依赖 lifecycle）** |
| 已安装真实 handler 在同一 runtime 下两组互不借 token（A token 不能授权 B 事件） | `effect::test_installed_matcher_never_borrows_a_token_across_groups` | **GREEN 探针（不依赖 lifecycle）** |
| C1 装配次序 probe：`prepare` 后恢复真实 `get_config_storage` → 已启动 Admission 仍真工作 + 之后 RouletteManager 真实 PG | `lifecycle_pg::test_restoring_config_storage_factory_keeps_admission_fake_live_and_roulette_real_pg` | **GREEN 探针（不依赖 lifecycle）** |
| 已安装真实 handler 在组锁内复核撤权 → 零 receipt/零发送 | `effect::test_installed_handler_rechecks_authority_inside_the_group_lock` | **业务断言 RED（缺锁后 effect 复核）** |
| **真实 lifecycle 安装**（非手装 runtime）下有效 token 提交并录得真实 SDK 载荷 | `lifecycle_pg::test_lifecycle_installed_qq_handler_commits_valid_token_and_captures_real_sdk_payload` | 缺 seam RED（驱动真实 driver startup hook + 已安装 `handle_roulette_qq`） |
| **真实 lifecycle 安装**状态、执行前 pluginFalse / ban / policy 撤销 → 至少拒绝（0 receipt/game/send） | `lifecycle_pg::test_lifecycle_installed_qq_handler_rejects_revoked_authority_before_execution[plugin_disabled\|user_banned\|policy_restricted]` | 缺 seam RED（参数化 3 例） |
| **真实 lifecycle 安装**下发 claim 已提交后（Event 挂起，真实 PG pending）撤 ban / canonical 重映射再释放 → 0 网络 / `NOT_DELIVERED` 但已提交 domain/receipt 事实保留 | `lifecycle_pg::test_lifecycle_installed_delivery_post_claim_revocation_blocks_network_but_keeps_committed_facts[user_banned\|canonical_remapped]` | 缺 seam RED（参数化 2 例；屏障包裹真实 `app.service.claim_fulfillment`，不猜 resolver 调用） |
| **真实 lifecycle 安装**下真实组锁等待后 pluginFalse → 至少一例零 receipt/game（不靠手装 gateway 闭包） | `lifecycle_pg::test_lifecycle_installed_qq_handler_group_lock_wait_then_plugin_false_leaves_zero_receipt_game` | 缺 seam RED（真实 advisory 锁 + 真实 `backend_pid`/`wait_for_blocked`） |

### 13.3 RED / GREEN 分类（Stage-C1 返工 + 装配次序修正后实测：36 failed / 6 passed）

| 桶 | 用例 | 实测首错 |
|---|---|---|
| 缺 seam（lifecycle 模块不存在） | 11 个 `lifecycle::*` + **12** 个 `lifecycle_pg::*`（5 个结构 + 7 个 lifecycle→QQ 业务接线） | `AssertionError: 生产未注册恰一个 komari_roulette driver startup hook，实际 0` / `ModuleNotFoundError`/`AttributeError` |
| 缺签名（`effect_check`） | `effect::…exposes_effect_check_keyword`、`…is_evaluated_only_after…`、`…propagates_storage_errors`、`delivery::…exposes…`、`delivery::…blocks_network…`、`delivery_variant…`、`installed::…accepted_effect_check_still_honours_expired_window` | `TypeError: … got an unexpected keyword argument 'effect_check'` / `AssertionError: 'effect_check' in mappingproxy(...)` |
| 缺符号（`EffectCheckRejectedError`） | `…leaves_zero_effects`、`handler::…silently_absorbs…` | `ImportError: cannot import name 'EffectCheckRejectedError'` |
| 业务断言（handler 未传 per-call closure） | `handler::…original_token_closure…`、`concurrent_handlers…` | `AssertionError: the handler must pass the per-call effect_check to the service / each handler call must pass its own post-lock closure` |
| 业务断言（已安装 handler 缺锁后复核） | `installed::test_installed_handler_rechecks_authority_inside_the_group_lock` | `AssertionError: an authority revoked while the command queued must leave no domain receipt`（receipt 被提交） |
| 业务断言（`maintenance.close` 立即返回） | `lifecycle_pg::test_maintenance_close_waits_for_the_in_flight_cleanup_round` | `AssertionError: maintenance.close() must wait for the in-flight cleanup round instead of returning immediately`（`assert not True`） |
| **GREEN（不依赖 lifecycle 的真实旧 API 探针）** | `effect::test_old_api_authority_chain…`、`effect::test_real_delivery_true_runtime_check_with_expired_pg_window_never_sends`、`effect::test_installed_matcher_commits_valid_token_and_captures_real_sdk_payload`、`effect::test_installed_matcher_business_gate_reads_live_authority_for_same_token`、`effect::test_installed_matcher_never_borrows_a_token_across_groups`、`lifecycle_pg::test_restoring_config_storage_factory_keeps_admission_fake_live_and_roulette_real_pg` | ✅ 6 passed（前 5 条为真实 `AdmissionRuntime` / `BindingTransaction` / `user_ban` / `RouletteCommandService`/`RouletteDelivery`/真实 QQ adapter 传输录制；第 6 条为装配次序 fake/real 分界 probe） |

未使用 `fakeSender`/import error 单独宣称任一 AC 通过；缺 seam 用例在被测符号存在
后才会执行真实业务断言。六条 GREEN 均不依赖 `lifecycle` 模块，证明 C1 不是靠
「`QQruntime != None` + 全 true 门」才能通过；新增的装配次序 probe 另行证明
`prepare_control_plane` 之后恢复真实 `get_config_storage` 同时保持 fake watcher 下
的 Admission 与真实 PG 的 RouletteManager。

### 13.4 本阶段未验证（保留给后续修复轮）

- 真实 `group_admission` READY 时的端到端「维护准入放行 → 到期对局推进」在
  **已安装 lifecycle** 上的完整闭环仍未验证：`test_maintenance_admission_is_canonical_and_ignores_business_switch`
  已改为驱动真实 `app.maintenance.advance_due` + 真实 admission worker（删除私有
  `_admission` 与 `adjudicate` monkeypatch），但当前首错仍是缺 `lifecycle` seam；
  该 AC 精确态为「已冻结、待实现后真实运行」，不得据本阶段任何一轮宣称通过。
  （真实 admission READY 的旧 API 链已由不依赖 lifecycle 的 GREEN 探针单独覆盖。）
- `runtime` 在 `failed>0` tick 上 FAILED 的装配层证据（Stage-B 已在端口层覆盖，
  本阶段不重复）。
- 真实进程 `on_startup`/`on_shutdown` 被 NoneBot Driver 调用的顺序（本阶段直接
  调用 driver lifespan 注册的真实 hook，证明注册与行为，不启动完整 gunicorn）。
- REST/管理面、OBS/get_status 公开面（延后到 C2）。

### 13.5 执行记录（命令日志）

环境：worktree `/Users/derbay32/project/komari-bot/.agents/worktrees/tsk-279`，
branch `pi/TSK-279-runtime-recovery`，root venv `/Users/derbay32/project/komari-bot/.venv/bin/python`
（3.13.11）。PG/Redis 门控：

```
SQLALCHEMY_DATABASE_URL=postgresql+asyncpg://komari_test@127.0.0.1:55458/komari_tsk279_resume
KOMARI_TEST_POSTGRES_URL=postgresql+asyncpg://komari_test@127.0.0.1:55458/komari_tsk279_resume
KOMARI_TEST_REDIS_URL=redis://127.0.0.1:56358/15
```

| 命令 | 结果 |
|------|------|
| `ruff check tests/komari_roulette/` | ✅ All checks passed |
| `pytest .../test_tsk279_lifecycle.py .../test_tsk279_lifecycle_pg.py .../test_tsk279_effect_recheck_pg.py -q`（带门控，C1 初版：baseline `8a4483b` + 当时**未提交**的 C1 测试 diff，后提交为 `9592c74`） | 1 passed / 24 failed（全部设计内 RED；1 passed 为后来删除的 tautological `test_lifecycle_module_path_is_frozen`） |
| `pytest .../test_tsk279_runtime.py .../test_tsk279_maintenance_pg.py .../test_tsk279_observability.py -q`（带门控，改后回归） | ✅ 62 passed / 0 failed |
| `pytest .../test_tsk279_lifecycle.py .../test_tsk279_lifecycle_pg.py .../test_tsk279_effect_recheck_pg.py -q`（带门控，C1 返工后） | 5 passed / 29 failed（5 GREEN 旧 API 探针；29 缺 seam/签名/符号/业务断言 RED，详见 §13.6） |
| `ruff check tests/komari_roulette/`（C1 返工后） | ✅ All checks passed |
| `pyright --pythonpath /Users/derbay32/project/komari-bot/.venv/bin/python`（C1 初版） | 7 errors，全部为设计内 `effect_check`/`EffectCheckRejectedError` 缺签名 RED；lifecycle 文件 0 报错 |

> 证据口径：本阶段生产基线是 `8a4483b`；C1 测试文件在 `8a4483b` 时只是工作区
> **未提交** diff，随后才提交为 `9592c7`（`9592c74`）。因此不能把裸 `8a4483b`
> 当“含 C1 代码”的证据，也不能把 `9592c7` 当生产实现证据——`9592c7` 仅新增
> 测试文件，生产代码仍停在 `8a4483b`。

### 13.6 C1 测试基线返工（2026-09-11，审查响应）

触发：C1 初版存在四个可被“全 true 门”蒙混的漏洞——(1) 唯一通过的用例是
常量自比较 `test_lifecycle_module_path_is_frozen`；(2) 工作区残留一次性探针
`.pyc`；(3) 持久 GREEN 全部依赖 lifecycle seam，无法证明已交付的旧 API 权威链
真的可用；(4) 维护准入用例读取私有 `app.maintenance._admission` 并 monkeypatch
`adjudicate`；(5) 核心 RED 全部由 `_FakeService`/`_RecordingService` 闭包捕获，
只要 `QQruntime != None` 且三个门恒真即可通过。

本轮只改测试（生产代码未动，仍停在 `8a4483b`）：

1. **删除 tautology**：移除 `test_lifecycle_module_path_is_frozen`（常量自比较，
   零信息）及其未使用的 `LIFECYCLE_MODULE` 导入。
2. **删除工作区残留探针**：`tests/komari_roulette/__pycache__/test_tsk279_c1_probe_tmp.cpython-313-pytest-9.1.1.pyc`
   是 Scratch-then-delete 探针的产物（无对应 `.py` 源，已确认）。以后所有探针
   直接写进允许的 C1 测试文件，不再走临时文件。
3. **新增 4 条不依赖 lifecycle 的持久 GREEN 探针**（真实服务/PG/协议，禁
   monkeypatch `recheck`/`adjudicate`/`is_banned` 返回布尔）：
   - `test_old_api_authority_chain_allows_then_rejects_the_same_token`：真实
     `AdmissionRuntime` + `qualify_qq_event`/`recheck_qq_effect` + 真实
     `BindingTransaction.resolve_group/resolve_member` + 真实 `UserBanService`；
     同一原始 token 依次：放行 → ban 拒绝（`user_banned`）→ 解封放行 →
     canonical 群重映射拒绝（`scope_mismatch`）→ policy 撤销拒绝
     （`policy_restricted`）。回调在 `finally` 显式 `register_*(None)` 复位
     （`registry_isolation_context` 不保存 `group_admission._callbacks`）。
   - `test_real_delivery_true_runtime_check_with_expired_pg_window_never_sends`：
     真实 `RouletteDelivery`，`runtime_check` 接受、PG 窗口过期 → `NOT_DELIVERED`、
     零网络、落库状态 `NOT_DELIVERED`。
   - `test_installed_matcher_commits_valid_token_and_captures_real_sdk_payload`：
     真实 `install_roulette_qq_runtime` + 真实 `handle_roulette_qq` + 真实
     `RouletteCommandService` + 真实 QQ `Bot`（`RecordingQQBot.call_api` 录制
     `post_group_messages`，零网络）；断言 receipt 提交且 SDK 载荷
     `msg_id`/`msg_seq`/markdown 正文正确。
   - `test_installed_matcher_business_gate_reads_live_authority_for_same_token`：
     已安装 handler 的 business gate 对同一 token 在 ban / canonical 重映射 /
     policy 撤销后均拒绝：零 receipt、零发送。
4. **新增 1 条已安装路径 RED**：`test_installed_handler_rechecks_authority_inside_the_group_lock`
   ——真实 handler/service/admission/binding/ban，成员在命令排队等组锁时被封禁；
   要求组锁内复核后零 receipt、零发送。当前失败于 receipt 被提交（缺锁后复核）。
   另新增 `test_installed_matcher_never_borrows_a_token_across_groups`：两组同一
   runtime 下 A 的 token 不得授权 B 的事件（GREEN）。
5. **旧 API＋PG 窗口 RED**：`test_real_delivery_accepted_effect_check_still_honours_expired_window`
   ——即使 claim 后 `effect_check` 接受，仍必须遵守 PG 窗口（缺 `effect_check` 签名即 RED）。
6. **依赖获取/生命周期竞态 RED（lifecycle）**：
   - `test_start_config_acquisition_failure_is_safe_and_recovers`：配置获取失败 →
     不装 QQ、保留安全 failed/disabled gate 与受控周期恢复入口；依赖恢复后经该
     入口（非手动重装）转 READY 并安装 QQ。不错误断言“失败即无 job/runtime”。
   - `test_stop_while_owned_recovery_waits_on_group_lock_settles`：owned recovery
     回调阻塞在组锁时 stop → 有界收尾、移除 owned job、清 QQ 分发、foreign job
     不动、被排队效果零推进。
   - `test_stop_during_blocking_config_initialize_never_installs_later`：startup
     阻塞在 `initialize_async` 时 stop → 释放后不后置安装 QQ、不留 owned job。
   - 新增 `BlockingConfig`（`TogglableConfig` 子类，`initialize_async` 以
     `asyncio.Event` 阻塞）。
7. **维护准入用例去私有化**：`test_maintenance_admission_is_canonical_and_ignores_business_switch`
   改为驱动真实 `app.maintenance.advance_due()`（真实 `group_admission` +
   `prepare_control_plane` + 真实 canonical 绑定）：业务开关关闭时仍放行推进，
   policy 撤销后不推进；删除 `_admission` 私有属性访问与 `adjudicate` fake。
8. **共享引擎 spy 收敛**：`test_stop_never_disposes_shared_orm_engine` 只断言
   `id(shared_engine) not in dispose_calls`，避免把测试自身 engine 清理误判为
   生产 dispose。

边界与风险：`group_admission` 的注册回调是进程级模块全局，用例在 `finally`
显式复位；`prepare_control_plane` 的 watcher 任务随 monkeypatch 撤销，测试内不做
手动 `runtime.close()`（沿用既有 group_admission 测试约定）。维护准入用例仍以缺
`lifecycle` seam 为首错，属“已冻结、待实现后真实运行”，本轮不宣称该 AC 通过。

### 13.7 Stage-C1 补强：真实 lifecycle 安装 → QQ 业务接线（2026-09-11）

触发：C1 的持久 GREEN（`effect::test_installed_matcher_*`）虽然使用真实
`AdmissionRuntime` / `BindingTransaction` / `UserBanService`，但它们是**手动**调用
`install_roulette_qq_runtime` 并传入测试自造的 `business_gate` / `runtime_check` /
`send_gate`，因此**不能**证明未来的真实生命周期会安装“读取真实权威”的门。
函数名里的 `installed` 不等于真实装配已完成。

本轮**只改** C1 原始 5 文件中的 `test_tsk279_lifecycle_pg.py` 与本合同（不碰生产、
不新建文件、不做临时探针），新增 7 个参数化 lifecycle→handler 用例；它们：
**绝不调用 `install_roulette_qq_runtime`，也绝不替换已安装的三个门**——先跑真实
`require_single_startup_hook` + `invoke_hook` 完成组合根装配，再直接驱动全局已安装的
`handle_roulette_qq`。

装配次序（`_installed_lifecycle_qq_authority`，dependency-first，模仿生产 fail-closed）：

1. 删除 `komari_roulette_config` 单行；
2. **先** `prepare_control_plane(monkeypatch, storage)`：真实 `_AdmissionRuntime` 以
   可控 policy 存储（`AdmissionStorageFake`）`start` 至 READY，并在此时把 manager
   快照 watcher 注册到 fake；
3. **立即恢复** `manager_module.get_config_storage` 为真实工厂（`prepare_control_plane`
   只 patch 了 `manager` 模块的全局工厂；已启动的 AdmissionRuntime 在 `start` 时已捕获
   fake 的 watcher 回调，而之后创建的 RouletteManager 必须走真实 PG）；
4. 注册真实 `BindingTransaction` 群/成员 resolver 与真实 `UserBanService` ban checker，
   并绑定 canonical 数字群（全部在任何已安装门读取之前）；
5. **再**进入真实 `lifecycle_context`（真实 Driver lifespan + 录制
   `get_config_manager` 注册表）并 `invoke_hook` 真实 startup hook → 组合根用**真实**
   `ConfigManager` 读真实 PG（默认值）；
6. 经该真实 manager `update_field_async("plugin_enable", …)` 写入用例所需 live 开关，
   pluginTrue 时 `app.runtime.run_recovery_tick()` 转 READY，并断言
   `app.runtime.get_state().plugin_enable`；
7. yield 后直接驱动全局已安装的 `handle_roulette_qq`。

旧的「先 startup、后 prepare」次序已废弃：它让 startup 早于准入依赖 READY，逼迫
实现忽略依赖 ready，因此不再作为契约。

**装配次序分界 probe（不依赖 lifecycle，持久 GREEN）**：

`lifecycle_pg::test_restoring_config_storage_factory_keeps_admission_fake_live_and_roulette_real_pg`
独立证明第 2–3 步分界正确：(a) `prepare` 后恢复真实工厂，fake 捕获的 watcher 仍
驱动已启动的 AdmissionRuntime（`storage.deliver` 撤销策略后 `adjudicate` 真的拒绝，
再放行真的恢复，`effective_revision` 随 fake 快照前进）；(b) 恢复后新建的真实
`ConfigManager("komari_roulette", DynamicConfigSchema)` `initialize_async` +
`update_field_async("plugin_enable", …)` 落库到真实 PostgreSQL `komari_roulette_config`
（raw SQL 复核），从不落入 admission fake。`finally` 显式复位全局
`get_config_storage` 并删除 `komari_roulette_config` 单行，不泄露其它 suite。

新增用例（全部为 **缺 seam RED**，首错
`AssertionError: 生产未注册恰一个 komari_roulette driver startup hook，实际 0`）：

| # | 用例 | 契约 |
|---|------|------|
| 1 | `test_lifecycle_installed_qq_handler_commits_valid_token_and_captures_real_sdk_payload` | 已安装 `get_roulette_qq_runtime() is not None`；有效 token → 恰一条 `post_group_messages`，`msg_id`/`msg_seq` 正确，且 SDK markdown 正文 == 落库冻结 `reply_projection.body` |
| 2 | `…rejects_revoked_authority_before_execution[plugin_disabled / user_banned / policy_restricted]` | 执行前 live pluginFalse / 真实 ban / policy 撤销：0 发送、0 receipt、全部 `scope_counts` 为 0 |
| 3 | `…delivery_post_claim_revocation_blocks_network_but_keeps_committed_facts[user_banned / canonical_remapped]` | 在真实 `app.service.claim_fulfillment` 提交后（`komari_roulette_fulfillments.state == PENDING_CONFIRMATION` 可见，非伪造）由 `_PostClaimClaimBarrier` 挂起，撤 ban / 改 canonical 映射再释放：0 网络、`NOT_DELIVERED`，但已提交 receipt（1 条）与 game（`lifecycle == waiting`）事实保留 |
| 4 | `…group_lock_wait_then_plugin_false_leaves_zero_receipt_game` | 真实群 advisory 锁下 `backend_pid`/`wait_for_blocked` 证明 handler 真阻塞，期间 pluginFalse，放锁后：0 发送、0 receipt、全部 `scope_counts` 为 0（不靠手装 gateway 闭包） |

post-claim 屏障语义：`_PostClaimClaimBarrier` 只在**真实**已安装 service 实例上包裹
真实 `claim_fulfillment(receipt_id)`：`await` 原方法完成 → 确认该 receipt 的 PG
fulfillment 为 `PENDING_CONFIRMATION` → `entered` 事件 + 等待 `release` → 返回原 claim。
封禁/改 canonical 映射发生在该方法返回后、`RouletteDelivery` 的所有最终 authority
读取之前，因此不再依赖/猜测实现是否调用群 resolver（合法 binding_session 读取路径
可能不调用它），也不逼迫生产在 resolver 读旧值后做无意义双重读取。屏障只针对本
用例 receipt_id，foreign 调用直通；它**不**替换任何已安装 QQ 门。真正的“claim 后
复核”缺口由生产 lifecycle/`effect_check` 承担。

**old-API green 与 lifecycle RED 分栏（不混淆函数名）**

| 分栏 | 断言对象 | 依赖 lifecycle? | 本轮状态 |
|---|---|---|---|
| old-API green（5） | 真实 `AdmissionRuntime`+`BindingTransaction`+`user_ban`+`RouletteCommandService`/`RouletteDelivery`+真实 QQ adapter；**手动** `install_roulette_qq_runtime` + 测试自造门（另含纯旧 API 权威链、真实 delivery PG 窗口两条） | 否 | ✅ 5 passed |
| 装配次序 probe（1） | `prepare_control_plane` + 真实工厂恢复：fake watcher 后的 AdmissionRuntime + 恢复后真实 PG RouletteManager（无 lifecycle） | 否 | ✅ 1 passed |
| lifecycle RED（7） | 真实 driver startup hook 装配出的组合根 + 全局已安装 `handle_roulette_qq`；**不**手装 runtime、**不**替换三个门 | 是（模块缺失） | ❌ 7 failed（缺 seam，待实现） |

因此 `installed_matcher` 的 GREEN **不**被当作“lifecycle 已完成接线”的证据；只有
lifecycle RED 全绿才证明未来装配安装正确的门。

**未覆盖内容（明确列出）**

- 本轮只覆盖 startup → 安装 → handler 业务接线；真实 `on_startup`/`on_shutdown`
  被完整 NoneBot Driver 调用的顺序（gunicorn 进程级）仍未覆盖（直接调用注册的真实
  hook，同 §13.4）。
- post-claim 分支本轮覆盖 ban 与 canonical 重映射；**pluginFalse 的 post-claim** 由
  执行前（用例 2）与组锁等待（用例 4）覆盖，不在 post-claim 参数化内。
- owner close 使在途请求 task 收到 `CancelledError` 的分支未纳入本轮：按既有协议允许
  `PENDING`/`UNKNOWN` 保留且不重试，不机械要求 `NOT_DELIVERED`；既有模拟关闭用例
  保持更窄范围，不作为本装配的替代。
- 维护准入在已安装 lifecycle 上的“放行 → 到期推进”端到端闭环仍未验证（同 §13.4）。
- `komari_roulette.lifecycle` 模块及其 hook body **仍不存在**：7 条 lifecycle→QQ
  用例与 5 条结构用例的实测首错全部是 `AssertionError: 生产未注册恰一个
  komari_roulette driver startup hook，实际 0`（缺 seam），因此装配后的真实
  body（startup 次序、三个门的具体实现、post-claim 复核）**仍未验证**，不得
  据本轮任何结果宣称这些 AC 通过。
- REST/管理面与 OBS/get_status 公开面延后到 C2。

**执行记录（命令日志）**

环境：worktree `/Users/derbay32/project/komari-bot/.agents/worktrees/tsk-279`，
branch `pi/TSK-279-runtime-recovery`，本阶段基于 `debfb8a`（返工 diff 为本次提交），
root venv `/Users/derbay32/project/komari-bot/.venv/bin/python`（3.13.11）；
pyright 同用 `/Users/derbay32/project/komari-bot/.venv/bin/pyright --pythonpath
/Users/derbay32/project/komari-bot/.venv/bin/python`。PG/Redis 门控（两 DSN 同库、
head `0021`）：

```
SQLALCHEMY_DATABASE_URL=postgresql+asyncpg://komari_test@127.0.0.1:55458/komari_tsk279_resume
KOMARI_TEST_POSTGRES_URL=postgresql+asyncpg://komari_test@127.0.0.1:55458/komari_tsk279_resume
KOMARI_TEST_REDIS_URL=redis://127.0.0.1:56358/15
```

| 命令 | 结果 |
|------|------|
| `pytest tests/komari_roulette/test_tsk279_effect_recheck_pg.py -q -p no:cacheprovider`（带门控） | 5 passed / 12 failed（5 条 old-API green 不变） |
| `pytest tests/komari_roulette/test_tsk279_lifecycle_pg.py -q -p no:cacheprovider`（带门控） | 1 passed / 13 failed（新增装配次序 probe green；5 结构 + 7 lifecycle→QQ 缺 seam RED，`maintenance.close` 业务断言 RED） |
| `pytest tests/komari_roulette/test_tsk279_lifecycle_pg.py -q -k restoring_config_storage -p no:cacheprovider`（带门控） | ✅ 1 passed（装配次序 fake/real 分界 probe） |
| `pytest tests/komari_roulette/test_tsk279_lifecycle_pg.py -q -k lifecycle_installed -p no:cacheprovider`（带门控） | 0 passed / 7 failed（全部缺 seam RED） |
| `pytest tests/komari_roulette/test_tsk279_{effect_recheck_pg,lifecycle,lifecycle_pg}.py -q -p no:cacheprovider`（带门控，C1 全量） | 6 passed / **36** failed（5 旧 API green + 1 装配次序 probe green；36 缺 seam/签名/符号/业务断言 RED） |
| `ruff check tests/komari_roulette/` | ✅ All checks passed |
| `ruff check .` | ✅ All checks passed |
| `pyright --pythonpath /Users/derbay32/project/komari-bot/.venv/bin/python tests/komari_roulette/test_tsk279_lifecycle_pg.py` | ✅ 0 errors, 0 warnings, 0 informations |
| `pyright --pythonpath /Users/derbay32/project/komari-bot/.venv/bin/python`（全仓） | 8 errors，全部为设计内 `effect_check`/`EffectCheckRejectedError` 缺接口 RED（`test_tsk279_effect_recheck_pg.py`） |

清理核验：`_installed_lifecycle_qq_authority` 的 `finally` 显式恢复全局
`manager.get_config_storage`、复位 `register_qq_group_resolver(None)` /
`register_qq_ban_checker(None)`、`ban_service.close()`、`delete_roulette_config`；
装配次序 probe 同样在 `finally` 恢复 `get_config_storage` 并删除
`komari_roulette_config` 单行。C1 全量运行后实测：`komari_roulette_config` 0 行，
本 scope 绑定行/轮盘 receipt/game/fulfillment 均为 0。

### 13.8 C1 测试收尾：真实脏工作区跑基线 → 提交（2026-09-11，独立审计响应）

关系声明（base ↔ dirty → commit）：本轮不是在干净 base 上写测试，而是**先在实际
工作区**（base `bc4b1cc`，HEAD `a2a3416`，7 个 C1 测试文件处于上一轮 dirty 状态）
上跑出真实基线（11 failed / 101 passed），再补 4 条审计缺口 RED，最后把
**base + dirty + 本轮新增**作为一次提交落盘（commit message 说明 what/why）。因此
下表 15 failed / 101 passed 是“真实 base+dirty”的实测，而不是干净 base 的推测；
提交后 `git status` 干净。

独立审计给出的 3 个 shutdown 缺口 + 1 个根缺口，上一轮 11 RED 均未覆盖，本轮各加
1 条最小 RED（不新增/不扩散文件，全部落在允许的 7 文件内）：

| 缺口 | 新增用例（文件） | 冻结断言 | 实测首错（RED 证据） |
|---|---|---|---|
| A 失败启动越权安装 | `test_stop_then_failed_config_initialize_never_installs_bootstrap`（`test_tsk279_lifecycle.py`，新增 `BlockingFailingConfig`） | startup 阻塞在 `initialize_async` → stop 先完成 → 释放后 `initialize` 抛错：`get_roulette_application() is None`、`get_roulette_qq_runtime() is None`、`RECOVERY_JOB_ID`/`CLEANUP_JOB_ID` 均未注册 | `AssertionError: a failed startup that raced a completed shutdown must not install the fail-closed bootstrap application`（装上了 `_LazyConfigPort` bootstrap app） |
| B stop 起点旧门仍放行 | `test_lifecycle_stop_rejects_the_old_installed_gate_before_maintenance_drains`（`_pg`，捕获真实 `business_gate`） | 用 Event 门控 `app.maintenance.close`，stop 进行中调用旧已安装 gate：必须 `False`；释放 stop 后 app/QQ 仍为 None | `AssertionError: the old installed gate must reject as soon as stop begins, not only after maintenance drains` |
| C stop 不取消 owned job | `test_stop_bounded_joins_the_registered_cleanup_blocked_on_the_group_lock`（`_pg`） | monkeypatch 真实 `CLOSE_ROUND_TIMEOUT_SECONDS=0.5`，真实注册的 `CLEANUP_JOB_ID` 回调阻塞在真实群 advisory 锁上，stop 返回后放锁：aged receipt 必须保留（`== 1`），即 stop 返回后不得 DELETE | `AssertionError: the registered cleanup must not DELETE after stop returned`（`assert 0 == 1`） |
| 根：门 await 后不复核本地 | `test_lifecycle_business_gate_rechecks_local_switch_after_the_recheck`（`_pg`，捕获真实 `business_gate`） | 只包 `group_admission.recheck_qq_effect` 计时（Event），仍委托真实 helper；await 期间经真实 manager 翻 `plugin_enable=False`：0 发送、0 receipt、全部 `scope_counts` 为 0 | `AssertionError: a live switch turned off during the recheck must leave no receipt`（`assert 1 == 0`） |

根缺口语义：`_business_gate` 只在 await **前**读 `app.runtime.accepting`，await 后直接
`return decision.allowed`，不重读本地 runtime/owner 状态。包装器只控制时序、仍委托真实
`recheck_qq_effect`，因此不伪造远端裁决；证据是“本地动态开关不是实时的”。既有
after-claim（用例 3）与 after-lock（用例 4）保持不动，不重复覆盖。

装配门捕获（不改写生产门）：`_installed_lifecycle_qq_authority` 用 `monkeypatch.setattr`
把 `lifecycle.install_roulette_qq_runtime` 换成记录包装器，**委托真实安装**并抄下
`business_gate`，存进 `_InstalledQQAuthority.business_gate`；被测试的仍是组合根安装的
那一个门，而非测试自造门。

验收卫生：`_installed_lifecycle_qq_authority` 改为**每用例新随机 QQ**
（`8_500_000_000 + uuid4() % 1e9`），并**删除**“用前先解封该身份”的 pre-clean；
`finally` 只精确解封本用例自己创建的 `member_qq`。因此不再触碰任何 foreign ban，
也不再需要 `_REVOKE_MEMBER_QQ` / `_POST_CLAIM_MEMBER_QQ` 每 mutation 一个固定号。
根允许新增 readiness 属性；真实 manager 失败时 `is_ready is False` 的
`test_character_binding_manager_is_ready_is_false_after_failed_initialize` 保留且仍
GREEN（不在 15 RED 内）。

实测（本工作区，base `bc4b1cc` + dirty，门控 DSN 同 §13.7，命令均带
`-p no:cacheprovider`、不禁用 randomly）：

| 命令 | 结果 |
|------|------|
| `pytest tests/komari_roulette/test_tsk278_handler.py test_tsk279_effect_recheck_pg.py test_tsk279_lifecycle.py test_tsk279_lifecycle_pg.py test_tsk279_runtime.py -q --tb=no`（带门控） | **15 failed / 101 passed**（11 上轮 RED + 4 本轮 RED；101 passed 与基线相同，无 GREEN 被破坏） |
| `pytest` 仅本轮 4 条（`-q --tb=line`，带门控） | 4 failed，首错逐条为 A/B/C/根（同上表） |
| `ruff check .` | ✅ All checks passed |
| `pyright --pythonpath /Users/derbay32/project/komari-bot/.venv/bin/python`（全仓） | ✅ 0 errors, 0 warnings, 0 informations |

未覆盖/风险：本轮只加 RED，不修生产；四个缺口在实现修复前必须保持 RED。Gap B/C
用“在途/阻塞”制造窗口，未覆盖真实 gunicorn 进程级 on_shutdown 顺序（同 §13.4）；
真实远端原子性未断言，只断言本地动态开关/owner 状态的实时性。

## 14. Stage-C2：REST status / 排行榜核查（inspect）/ 重投影（rebuild）+ 正式管理装配（本阶段）

本阶段把「控制面」钉死在真实 FastAPI（ASGI 传输）+ 真实
``komari_bot.management.management_api`` 鉴权/权限 + 真实
``management_audit_span`` + 真实 ``PostgresRouletteStorage`` 上，并把
``komari_roulette`` 接入正式管理装配（``ManagementApiComponents`` /
``register_management_api_for_driver``）。新增文件（均为测试专用）：

| 文件 | 行数 | 作用 |
|---|---|---|
| `tsk279_management_support.py` | 685 | 冻结常量（前缀/三条路由/错误闭集/差异闭集/响应字段集/四凭据）、DTO 构造、路由遍历（fastapi 0.139 `_IncludedRouter` 递归）、真实 ASGI 客户端、fake 存储/会话、录制审计、控制面装配 |
| `test_tsk279_management_api.py` | 744 | 无 PG 的 HTTP 契约 + 装配守卫（23 个 `def`，参数化后 26 例） |
| `test_tsk279_leaderboard_management_pg.py` | 700 | 真实 PostgreSQL 存储接缝 + 两种加锁顺序竞态 + HTTP e2e（12 个 `def`，参数化后 16 例） |

生产 ``komari_bot.plugins.komari_roulette.management_api``、
``PostgresRouletteStorage.inspect_leaderboard``、顶层
``get_roulette_observation`` 暴露面与 ``ManagementApiComponents`` 两个新字段
尚不存在，RED 由 ``ModuleNotFoundError`` / ``AttributeError`` / 顶层
``__all__`` 缺符号 / 生产 dataclass 缺字段触发。**两者都是设计内 RED，不是
fixture 造假**：无 PG 文件里真实装配的是真 DI 骨架，有 PG 文件里真实跑完的
是种子数据与原始 SQL 变异（见 §14.5 首错证据）。

### 14.1 拟议最窄公共接缝

```python
# komari_bot/plugins/komari_roulette/management_api.py（新模块）
API_PREFIX = "/api/v2/komari-roulette"
ROULETTE_MANAGEMENT_ERROR_CODES = frozenset({
    "roulette_status_unavailable", "roulette_storage_unavailable",
    "roulette_aggregate_corrupt",
})

def register_roulette_management_api(
    app: FastAPI, *, api_token: Sequence[Mapping[str, object]],
    allowed_origins: Sequence[str],
    observation_getter: Callable[[], RouletteObservation | None] | None = None,
    session_factory: Callable[[], AsyncContextManager[AsyncSession]] | None = None,
    storage_factory: Callable[[AsyncSession], RouletteStorage] | None = None,
    audit_recorder: Callable[[ManagementAuditEvent], None] | None = None,
) -> None                                  # 幂等（app.state.<flag>），镜像 config/prompt 模式
# 默认值：observation_getter=顶层 get_roulette_observation，
#        session_factory=共享 ORM get_session，storage_factory=PostgresRouletteStorage
# 路由面**精确**三条，无 reset / 改分 / 预览确认令牌入口：
#   GET  /status                              roulette:read
#   POST /leaderboards/inspect                roulette:read   （POST 让身份不进 URL 访问日志）
#   POST /leaderboards/rebuild                roulette:manage
#                                              + X-Komari-Change-Reason + X-Request-ID
# 路由模块不得 import group_admission / komari_roulette.config_schema /
# komari_management（控制面不是业务门）

# komari_bot/management/management_api.py
_READ_PERMISSION_IMPLICATIONS["roulette:read"] = frozenset({"roulette:manage"})
# manage 蕴含 read（与 character_binding / reply_fulfillment 同形）；
# 表内容本身无数值断言，由「MANAGER_TOKEN 只带 roulette:manage 而 status/inspect 返回 200」行为覆盖

# komari_bot/plugins/komari_roulette/storage.py
async def inspect_leaderboard(self, group: GroupRef) -> LeaderboardInspection
# 一致快照：同一事务内读 completed 证明（results）与缓存投影（leaderboard），
# 逐 member_openid 对照；缺行/多行/错 wins/显示名错配/时间错配都给精确 code。
# completed 证明损坏（如 winner_display_name 为 NULL）→ AggregateCorruptError
# （与 rebuild_leaderboard 同一 fail-safe 判定），绝不静默伪 0。

@dataclass(frozen=True, slots=True)
class LeaderboardInspection:            # 具体宿主模块不指定，只要求顶层可 import
    app_id: str; group_openid: str; consistent: bool
    cached_entry_count: int; completed_entry_count: int
    cached_total_wins: int; completed_total_wins: int
    discrepancy_codes: tuple[str, ...]   # <= DISCREPANCY_CODES
    entries: tuple[LeaderboardEntry, ...]  # 安全面：无 member_openid

DISCREPANCY_CODES = {  # 冻结闭集（生产不必导出同名常量）
    "missing_cached_row", "extra_cached_row", "wins_mismatch",
    "display_name_mismatch", "last_won_at_mismatch",
}

# komari_bot/plugins/komari_roulette/__init__.py（ADR-0006 顶层暴露面）
__all__ += ["get_roulette_observation", "register_roulette_management_api",
            "LeaderboardInspection"]
```

固定错误外壳（三条路由共用）：``{"detail": {"code", "message"}}``，``code`` 属
``ROULETTE_MANAGEMENT_ERROR_CODES`` 闭集，``message`` 为固定文案，**绝不回显异常
正文/身份**（测试注入 ``SECRET_LEAK = "SECRET-LEAK-9f2c4d"`` 并断言缺失）。三个
code **统一映射 503**：``roulette_status_unavailable``（owner 未装配，绝不伪造
``ready``）、``roulette_storage_unavailable``、``roulette_aggregate_corrupt``——
统一「运维上不可用」语义，避免把「无 owner」错读成 404/500。

响应面（冻结键集，多一键即红）：

| 路由 | 响应 |
|---|---|
| `status` | `== RouletteObservation.as_dict()`（`runtime_status`/`runtime_reason`/`latest_scan`/`latest_cleanup`/`pending_receipts`/`fault_counts`），不包含任何进程/队列内部态 |
| `inspect` | `app_id`/`group_openid`/`consistent`/`cached_entry_count`/`completed_entry_count`/`cached_total_wins`/`completed_total_wins`/`discrepancy_codes`/`entries`；entry 仅 `display_name`/`wins`/`last_won_at` |
| `rebuild` | `app_id`/`group_openid`/`consistent`/`entry_count`/`total_wins` |

rebuild 处理器算法（冻结）：校验 body（extra 禁止 / 空标识 422）→ 鉴权
（缺 token 401 / 权限不足 403）→ reason + request-id（缺/空白 400，超 200 字符或
格式非法 422）→ 开 session → ``rebuild_leaderboard`` → ``inspect_leaderboard`` →
``commit``，全部在 ``management_audit_span`` 内。审计事件固定：
``action="roulette.leaderboard.rebuild"``、``resource="roulette"``、
``field_name=None``、``target_hash=hash_management_target(app_id, group_openid)``、
``metadata={entry_count, total_wins, result_code="rebuilt"}``、``status_code=200``；
**只读面不写审计**；``started`` 必须先于变更、``succeeded`` 后于变更（用
``probe`` 在事件时刻抄 ``storage.rebuilds`` 证明）。失败语义：``started`` 阶段失败
→ 中止在变更之前（零 rebuild / 零 commit / 非 200）；``succeeded`` 阶段失败 →
只告警，**不回滚已提交事实**（200 + commit 保留）。rebuild **不**申请/校验
10 分钟预览确认令牌（该令牌在本控制面不存在，由路由面精确相等守住）。

正式管理装配（生产必须改的两处）：

* `komari_bot/plugins/komari_management/api_runtime.py`：
  `ManagementApiComponents` 增加 `register_roulette_management_api` 与
  `roulette_observation_getter` 两个**必填**字段，并在
  `register_management_api_for_driver` 中调用
  `components.register_roulette_management_api(...)` 且传
  `observation_getter=components.roulette_observation_getter`。
* `komari_bot/plugins/komari_management/__init__.py`：`_load_management_components`
  增加 `require("komari_roulette")`、`from komari_bot.plugins import komari_roulette as roulette_plugin`，
  并把 `register_roulette_management_api=roulette_plugin.register_roulette_management_api`、
  `roulette_observation_getter=roulette_plugin.get_roulette_observation` 作为真实符号传入。

**是否必填的裁决（C2 基线根审后纠正）**：两个新字段
``register_roulette_management_api`` / ``roulette_observation_getter`` **必须必填**
（无默认值），与 TSK-280 的 ``register_character_binding_repair_api`` /
``character_binding_repair_service_getter`` 先例一致；**不得**用 ``None`` / ``_noop``
默认值去迁就旧构造点。既有构造点
（``tests/komari_management/test_management_api_runtime.py``、
``test_plugin_integration.py``）的 fixture 必须同步补齐这两个字段（仅 dummy
registrar/getter，不削弱任何业务断言），并以
``test_management_components_require_binding_repair_fields`` 同款 ``inspect.signature``
断言覆盖新字段无默认值。

**真实 loader 证据（取代源码字符串守卫）**：删除只匹配 ``__init__.py`` 源码文本
形状（``from ... import ... as roulette_plugin`` 的逐字形状、逐字 kwarg 行）的守卫
——它既不证明真实 loader 载入真实符号，又钉死了 ``from import`` 形状（根要求
“别钉”）。代之以行为证据：复用 ``tests/komari_management/test_plugin_integration.py``
的真实 NoneBot 测试语境，**实际执行生产** ``komari_management/__init__.py`` 的
``_load_management_components``（隔离加载真实 ``__init__``，其余插件依赖按既有
shim/stub 提供），断言
``components.register_roulette_management_api is komari_bot.plugins.komari_roulette.register_roulette_management_api``
与 ``components.roulette_observation_getter is ...get_roulette_observation``，并记录
loader 确实调用过 ``require("komari_roulette")``（不匹配 import 形状）。后续真实
``register_management_api_for_driver`` 挂载路由的测试仍由
``api::test_real_management_assembly_mounts_roulette_routes`` 承担。

装配路径沿用 ``binding_repair_lifecycle.py`` 先例（函数内 deferred ``require`` +
顶层公开面 import），跨插件只走顶层面。

### 14.2 AC → 用例 → 冻结断言

| AC | 用例 | 断言性质 |
|---|---|---|
| 观测快照键集 + 共享管理接缝可用 | `api::test_green_probe_observation_and_shared_management_seams` | **GREEN 探针（不依赖 C2 seam）** |
| 审计投影（事件 → dict）永不携带 app/group 身份 | `api::test_audit_event_projection_never_carries_identity` | **GREEN 探针（不依赖 C2 seam）** |
| 路由模块声明冻结前缀与三条路径 | `api::test_roulette_management_module_declares_the_frozen_prefix` | 缺 seam RED |
| 错误 code 恰为闭集 | `api::test_management_error_codes_are_the_frozen_closed_set` | 缺 seam RED |
| 顶层 `__all__` 暴露三个 C2 符号 | `api::test_roulette_package_exposes_the_c2_surface_at_top_level` | 缺暴露面 RED |
| 路由面精确三条 + 注册幂等 + 无禁用入口词 | `api::test_exact_route_surface_and_idempotent_registration` | 缺 seam RED |
| 控制面不读 `plugin_enable`、不过群准入（无关权限仍 403） | `api::test_control_plane_needs_no_plugin_enable_or_admission_gate` | 缺 seam RED |
| 路由模块不 import 门控/管理插件内部模块 | `api::test_roulette_management_module_imports_no_forbidden_gate` | 缺 seam RED |
| `status` 键集 == `RouletteObservation.as_dict()`；read/manage/wildcard 200，无关 403，匿名 401，零审计 | `api::test_status_projects_the_frozen_snapshot_and_permission_matrix` | 缺 seam RED |
| owner 未装配 ⇒ 503 `roulette_status_unavailable`（走真实默认 getter） | `api::test_status_fails_closed_when_no_application_owns_the_runtime` | 缺 seam RED |
| `inspect` 安全面（entry 三键、无 `member_openid`）+ 三凭据 200 / 无关 403 / 零变更 / 零审计 | `api::test_inspect_projects_safe_leaderboard_and_permission_matrix` | 缺 seam RED |
| `inspect` 差异如实透出且不写缓存 | `api::test_inspect_reports_discrepancies_without_mutating_the_cache` | 缺 seam RED |
| `inspect` 存储/聚合失败 → 固定外壳 + 不回显正文（参数化 2） | `api::test_inspect_failures_use_the_fixed_error_envelope[StorageUnavailableError\|AggregateCorruptError]` | 缺 seam RED |
| inspect/rebuild extra 字段与空标识一律 422 | `api::test_inspect_and_rebuild_forbid_extra_fields_and_empty_identifiers` | 缺 seam RED |
| rebuild 必须带 manage + reason + request-id（缺/空白 400、超长/非法 422、reader/无关 403） | `api::test_rebuild_requires_manage_reason_and_request_id` | 缺 seam RED |
| rebuild 恰一次 commit、`started` 先于变更、审计事件安全投影 | `api::test_rebuild_commits_once_and_records_safe_audit_events` | 缺 seam RED |
| 审计 `succeeded` 写失败不回滚已提交事实 | `api::test_rebuild_audit_final_failure_keeps_the_committed_fact` | 缺 seam RED |
| 审计 `started` 写失败 ⇒ 零 rebuild / 零 commit | `api::test_rebuild_started_audit_failure_leaves_no_change` | 缺 seam RED |
| rebuild 失败 → 固定外壳 + 零 commit | `api::test_rebuild_failures_use_the_fixed_error_envelope` | 缺 seam RED |
| `ManagementApiComponents` 含两个 C2 **必填**字段 | `api::test_management_components_expose_the_roulette_fields` | 缺字段 RED |
| 生产 `_load_management_components` 载入**真实** roulette 两符号 + `require` 声明 | `plugin_integration::test_production_loader_carries_real_roulette_symbols` | 缺接线 RED |
| 真实 `register_management_api_for_driver` 挂载三条路由并端到端可读 | `api::test_real_management_assembly_mounts_roulette_routes` | 缺 seam RED |
| `status` 对 ready/disabled/failed 经**真实 getter** 全 200（控制面不因业务状态拒绝） | `api::test_status_returns_every_lifecycle_state_through_the_real_getter[ready\|disabled\|failed]` | 缺 seam RED |
| 共享 `management_audit_span` 的 `started` fail-closed / final 吞异常语义 | `api::test_shared_audit_span_started_is_fail_closed_and_final_is_swallowed` | **GREEN 探针（不依赖 C2 seam）** |
| 空作用域核查一致且为空 | `pg::test_inspect_empty_scope_is_consistent_and_empty` | 缺 seam RED |
| 一次真实 completed 后核查与真实投影一致 | `pg::test_inspect_matches_the_real_projection_after_a_completed_game` | 缺 seam RED |
| 缓存缺行/多行/错 wins/显示名/时间 → 精确差异 code（参数化 5） | `pg::test_inspect_reports_the_exact_cache_divergence[delete\|insert\|wins\|display_name\|last_won_at]` | 缺 seam RED |
| rebuild 只从 completed 证明重建缓存并回到一致 | `pg::test_rebuild_reproduces_the_cache_from_completed_proofs` | 缺 seam RED |
| 损坏证明 ⇒ 两个入口都抛 `AggregateCorruptError` 且缓存不变 | `pg::test_inspect_and_rebuild_fail_safe_on_a_corrupt_proof` | 缺 seam RED |
| rebuild 连做两次仍同一真实投影、不增 wins、不改 completed 证明 | `pg::test_rebuilt_projection_is_idempotent_and_never_recounts_wins` | 缺 seam RED |
| 同名不同 member、总 wins 相同的成员级 last_won_at 错配必须 `consistent=False`（只比 displayName/总 sum 不能过） | `pg::test_inspect_flags_same_name_member_level_mismatch` | 缺 seam RED |
| 并发在途终局投影时 `inspect` 仍读到单一一致快照（复用真实 PG 暂停接缝） | `pg::test_inspect_reads_one_consistent_snapshot_while_a_projection_is_uncommitted` | 缺 seam RED |
| rebuild 先持组锁、终局投影后到：wins 不丢且核查一致 | `pg::test_rebuild_holding_the_scope_lock_keeps_the_next_terminal_win` | 缺 seam RED |
| 终局投影先持组锁、rebuild 后到：两种顺序收敛同一事实 | `pg::test_terminal_projection_holding_the_scope_lock_keeps_the_win` | 缺 seam RED |
| HTTP inspect/rebuild 走真实存储默认缝并持久生效 | `pg::test_http_inspect_and_rebuild_drive_the_real_storage` | 缺 seam RED |
| HTTP rebuild 遇到损坏证明 → 503 `roulette_aggregate_corrupt` 且缓存不被破坏 | `pg::test_http_rebuild_maps_a_corrupt_proof_to_the_fixed_code` | 缺 seam RED |

### 14.3 RED / GREEN 分类（C2 基线实测：34 failed / 2 passed；根审 delta 后的当前实测见 §14.7）

| 桶 | 用例数 | 实测首错 |
|---|---|---|
| 缺 seam（路由模块不存在） | API 14 + PG 2 | `ModuleNotFoundError: No module named 'komari_bot.plugins.komari_roulette.management_api'` |
| 缺 seam（`inspect_leaderboard` 不存在） | PG 11 | `AttributeError: 'PostgresRouletteStorage' object has no attribute 'inspect_leaderboard'. Did you mean: 'list_leaderboard'?` |
| 缺暴露面 | API 1 | `AssertionError: 顶层 __all__ 缺少 Stage-C2 跨插件暴露面: ['get_roulette_observation', 'register_roulette_management_api', 'LeaderboardInspection']` |
| 缺字段（`ManagementApiComponents`） | API 2 | `pyright: No parameter named "register_roulette_management_api" / "roulette_observation_getter"`（运行期经 `build_management_components` 的 assert 硬化） |
| **GREEN（不依赖 C2 seam）** | API 2 | ✅ 观测键集 == `STATUS_RESPONSE_FIELDS`、审计投影不含身份 |

无「业务断言 RED」桶：C2 的一个业务常量（三 code 全 503、manage⇒read、
`started` 先于变更）在路由面不存在时无法与真值对照，因此不伪装成已测；它们
改为在 seam 落地后由上述用例**首次真实求值**。本地 SQLite/无 PG 环境不跑 PG 文件
（`skipif(not POSTGRES_URL)`），门控库不一致时 `db_scope` 再 skip 一层。

### 14.4 本阶段未验证（保留给后续修复轮）

- `inspect_leaderboard` 的**聚合语义**（真实 completed 证明 → 逐 member 期望
  wins/显示名/时间，以及差异 code 的**完整组合**）只在 seam 落地后才能求值；
  本阶段只能证明「种子数据 + 原始 SQL 变异 + 会话/事务编排」全部真实可用
  （见 §14.5 首错行号证据）。
- 差异 code 的**多 code 同时出现**（如缺行 + wins 错）未断言：PG 参数化各只
  制造**单一**变异，只断言期望 code `in` 且 `set(codes) <= DISCREPANCY_CODES`。
- `entries` 的**排序**（`wins desc, last_won_at asc, member_openid asc`）在 C2
  未断言（HTTP/PG 都只断言成员集合与 wins 集合），沿用 C1 `list_leaderboard` 覆盖。
- rebuild **幂等重放**：根审 delta 已补
  `pg::test_rebuilt_projection_is_idempotent_and_never_recounts_wins`（连做两次
  收敛同一真实投影、不增 wins、不改 completed 证明），原缺口关闭（见 §14.7）。
- HTTP 层并发（两个同时 rebuild / rebuild 与 inspect 并发）未做：PG 层用真实
  `pg_advisory_xact_lock` 阻塞 + `_wait_until_blocked` 断言阻塞方 backend pid，
  已确定性覆盖两种加锁顺序，但未覆盖 HTTP 请求级并发。
- `X-Komari-Change-Reason` 的**恰好 200 字符**边界（应接受）未断言：只断言缺/
  空白 → 400、201 字符 → 422。
- CORS：`allowed_origins` 只做透传，C2 全部用例传 `()`，未断言预检/CORS 头
  （沿用共享 `ensure_management_cors` 的既有覆盖）。
- 真实 ASGI 进程（uvicorn/gunicorn）下的挂载未验证：`test_real_management_assembly_mounts_roulette_routes`
  用 `_FakeDriver("fastapi", app)` + `httpx.ASGITransport`，与 §13.4 同一限制。
- `status` 在 `runtime_status` 为 `disabled`/`failed` 时的投影：根审 delta 已补
  `api::test_status_returns_every_lifecycle_state_through_the_real_getter[ready|disabled|failed]`
  （真实默认 getter + 真实 owner 状态归一化，控制面一律 200），原缺口关闭（见 §14.7）。

### 14.5 执行记录（命令日志）

环境：worktree `/Users/derbay32/project/komari-bot/.agents/worktrees/tsk-279`，
branch `pi/TSK-279-runtime-recovery`，HEAD `d869bfe575986ddb94a509f7bd051174045d4000`，
root venv `/Users/derbay32/project/komari-bot/.venv/bin/python`（3.13.11）。PG/Redis
门控同 §13.5（`postgresql+asyncpg://komari_test@127.0.0.1:55458/komari_tsk279_resume`、
`redis://127.0.0.1:56358/15`，pytest 均带 `-p no:cacheprovider`）。

| 命令 | 结果 |
|---|---|
| `ruff check tests/komari_roulette/` | ✅ All checks passed |
| `pyright --pythonpath … tests/komari_roulette/{tsk279_management_support,test_tsk279_management_api,test_tsk279_leaderboard_management_pg}.py` | 7 errors / 0 warnings，**全部为设计内 RED**（2 × `ManagementApiComponents` 缺字段 + 5 × `inspect_leaderboard` 缺接口） |
| `pyright --pythonpath …`（全仓） | 7 errors / 0 warnings / 0 informations，**与本文件同源**（C1 基线全仓 0 报错，§13.8）；即本轮增量为 +7，全部落在上表两个新测试文件 |
| `pytest tests/komari_roulette/test_tsk279_management_api.py -q --tb=no -rA` | **21 failed / 2 passed**（2 GREEN 为不依赖 seam 的探针） |
| `pytest tests/komari_roulette/test_tsk279_leaderboard_management_pg.py -q --tb=line`（带门控） | **13 failed**，首错：11 × `AttributeError: inspect_leaderboard`、2 × `ModuleNotFoundError: …management_api`（无连接错误） |
| `pytest tests/komari_roulette/test_tsk279_lifecycle.py tests/komari_roulette/test_tsk278_handler.py -q --tb=no`（带门控，C1 回归） | ✅ **61 passed**（C1 生命周期 + 278 handler 未受影响） |
| `git status --porcelain` / `git diff --cached --stat` / `git rev-parse HEAD` | 见 §14.6 |

首错行号证据（证明「种子/变异/事务编排」在真实 PG 上已经跑完，而非 fixture 报错）：

| 失败行 | 含义 | 已真实执行的部分 |
|---|---|---|
| `test_tsk279_leaderboard_management_pg.py:157`（7 例） | `_inspect` 辅助里的 `inspect_leaderboard` | 空作用域路径；2×`_persist_completed`（含 waiting→joining→shoot→forfeit 各阶段 commit）；5 种原始 SQL 缓存变异（DELETE / INSERT 幽灵行 / wins+5 / 改名 / `last_won_at + make_interval`）全部成功提交 |
| `:189`（2 例，正是两条竞态用例） | 末尾 `_assert_consistent_projection` | `_persist_completed(project=False)`、真实 `rebuild_leaderboard`、真实 `project_terminal`、`_backend_pid` 抄取、`_wait_until_blocked` **断言阻塞方 pid 命中**、两种顺序的 `commit()` 全部成功——即 `pg_advisory_xact_lock` 双向阻塞与「两顺序都保留 wins」已在真实库上跑通，只差最后的一致性核查 |
| `:275`（1 例） | `rebuild_reproduces` 的首次 `inspect` | 两次 completed 种子 + `DELETE` 缓存全部成功 |
| `:308`（1 例） | `fail_safe_on_a_corrupt_proof` 的 `inspect` | 种子 + `UPDATE komari_roulette_results SET winner_display_name = NULL` 成功（迁移 0019 对 winner 列无 CHECK，允许该变异），随后缓存未被改写 |

### 14.6 C1 索引不变声明

本阶段**只新增**三个测试文件 + 追加本节，未改动任何 C1 产物：

```
M  komari_bot/plugins/komari_roulette/lifecycle.py      ┐
M  komari_bot/plugins/komari_roulette/maintenance.py    │
M  komari_bot/plugins/komari_roulette/qq/__init__.py    ├ 6 个已 staged 的 C1 生产文件
M  komari_bot/plugins/komari_roulette/qq/delivery.py    │ （git diff --cached --stat 仍是
M  komari_bot/plugins/komari_roulette/qq/handler.py     │  562 insertions / 161 deletions）
M  komari_bot/plugins/komari_roulette/runtime.py        ┘
 M tests/group_admission/test_qq_roulette_gate.py       ┐ 3 个 C1 测试文件
 M tests/komari_roulette/test_tsk278_handler.py         ├ 仍为未 staged 的 ` M`，内容未动
 M tests/komari_roulette/test_tsk279_lifecycle.py       ┘
?? tests/komari_roulette/tsk279_management_support.py   ┐ 本轮新增（untracked）
?? tests/komari_roulette/test_tsk279_management_api.py  ├
?? tests/komari_roulette/test_tsk279_leaderboard_management_pg.py ┘
```

HEAD 仍为 `d869bfe575986ddb94a509f7bd051174045d4000`；C1 生命周期回归
61 passed 证明 C1 语义未被本阶段触碰。本阶段**未提交**、未触碰分支/索引/工作树
之外的任何状态（除本节追加到已跟踪的 `tests/komari_roulette/TSK-279-contract.md`）。

### 14.7 根审 delta 收尾执行记录（general test 收尾）

C2 基线根审后追加三项关键覆盖（`api::test_status_returns_every_lifecycle_state_through_the_real_getter`
参数化 3 例、`pg::test_rebuilt_projection_is_idempotent_and_never_recounts_wins`、
`pg::test_inspect_flags_same_name_member_level_mismatch`、
`pg::test_inspect_reads_one_consistent_snapshot_while_a_projection_is_uncommitted`）
与 `api::test_management_components_expose_the_roulette_fields`，并把「`__init__.py`
源码字符串守卫」换成真实 loader 行为证据（§14.1「真实 loader 证据」）。本轮收尾
修复了后者的一处 fixture 缺陷并冻结共享审计源码语义。

**唯一代码变更（测试专用）**：`tests/komari_management/test_plugin_integration.py`
的 `real_management_loader` fixture 原先对非 roulette 依赖回落到 suite 的
`nonebot.plugin.require` 桩（`tests/conftest.py:453`），而该桩白名单不含
`komari_help`，导致生产 `_load_management_components` 在 `require("komari_help")`
处 `RuntimeError: Unsupported plugin require in tests: komari_help` 中止——RED 落在
fixture 而非真实接线。现改为纯记录桩（loader 丢弃 `require` 返回值，其余依赖本就可
stub，§14.1），真实 loader 随后实际执行完毕。

实测 loader 记录到的 `require` 顺序（真实执行证据）：`komari_knowledge,
komari_help, komari_memory, agent_run_logger, komari_search, user_ban, komari_chat,
group_admission, character_binding`，缺 `komari_roulette` ⇒ 断言
`"komari_roulette" in required_plugins` 是设计内缺接线 RED；两符号身份断言
（`is roulette_plugin.register_roulette_management_api` / `get_roulette_observation`）
同样 RED，且不会被 stub 冒充（fixture 不 stub roulette 两符号）。

**`management_audit_span` 源码证明（冻结，不另造审计）**：`komari_bot/management/management_audit.py:253`
的 `await recorder(base_event)` 位于 `try: yield span`（`:257`）之前，`started` 写失败
直接抛出 `__aenter__`、业务体零执行 ⇒ fail-closed；`:204-211` 的 `_record_final_event`
把 final 写入异常吞掉并只 `logger.critical` ⇒ final 失败不回滚已提交事实。
`api::test_shared_audit_span_started_is_fail_closed_and_final_is_swallowed` 已 GREEN
冻结该语义（未改生产）。

**当前实测 counts**（root venv `/Users/derbay32/project/komari-bot/.venv/bin/python`
3.13.11；PG/Redis 门控同 §13.5）：

| 命令 | 结果 |
|---|---|
| `ruff check`（C2 3 文件 + 2 个 management fixture） | ✅ All checks passed |
| `pyright --pythonpath …`（全仓） | **16 errors / 0 warnings**，全部设计内 RED：`ManagementApiComponents` 缺字段 6（support 2 + runtime fixture 2 + plugin_integration fixture 2）+ roulette 顶层缺符号 2 + `inspect_leaderboard` 缺接口 8；C1 基线全仓 0 |
| `pytest tests/komari_roulette/test_tsk279_management_api.py` | **23 failed / 3 passed**（3 GREEN 探针：观测键集、审计投影、审计 span 语义） |
| `pytest tests/komari_roulette/test_tsk279_leaderboard_management_pg.py`（门控） | **16 failed**（14 × `AttributeError: inspect_leaderboard`；2 × `ModuleNotFoundError: …management_api`） |
| `pytest tests/komari_management/{test_management_api_runtime,test_plugin_integration}.py` | **6 failed / 3 passed**（4 + 2，均为 `ManagementApiComponents` 缺字段 / roulette 缺接线 RED） |

**新增 RED 分类**：API 新增 `test_management_components_expose_the_roulette_fields`（缺字段
RED）、`test_status_returns_every_lifecycle_state_through_the_real_getter[ready|disabled|failed]`
（缺 seam RED ×3）、`test_shared_audit_span_…`（GREEN）；PG 新增 3 例（幂等重放 /
同名成员级错配 / 在途投影一致快照）全部缺 seam RED；旧 management fixture 新增 6 例
缺字段/缺接线 RED。

**未覆盖义务**（保留原状）：§14.4 其余条目（多 code 同时出现、entries 排序、HTTP
请求级并发、reason 恰好 200 字符、CORS、真实 ASGI 进程）。

本轮只改 `tests/komari_roulette/TSK-279-contract.md` 与
`tests/komari_management/test_plugin_integration.py` 的测试 fixture；C1 的 6 生产 + 3
旧测试仍原样未提交，HEAD / 分支 / 索引 / 生产实现未动。

### 14.8 C2 根审测试自错误修复（test-only，3 例 fixture 缺陷，2026-09-11）

C2 生产接缝落地后（`management_api.py` / `inspect_leaderboard` / 顶层暴露面 /
`ManagementApiComponents` 两字段），C2 四文件基线为 **2 failed / 49 passed**
（唯一失败均落在 `test_tsk279_leaderboard_management_pg.py`）。根审确认这 3 例
是**用例自身**的 fixture 缺陷，不是生产 bug：两例必红、一例假绿（安全比对即可
识别，未真正钉成员 key）。本轮只改该 PG 测试文件与本合同，**不碰任何生产代码**。

**（1）`test_http_rebuild_maps_a_corrupt_proof_to_the_fixed_code`（必红 → 绿）**
HTTP rebuild 对损坏证明正确返回 503 `roulette_aggregate_corrupt`，但原末行
`_assert_consistent_projection` 又要求损坏 proof 能成功 `inspect_leaderboard`，
与该接缝自己的 fail-safe 合同（损坏证明必须抛 `AggregateCorruptError`）冲突。
改为请求前后**直接读缓存完整行（含 `member_openid`）与损坏证明行**：缓存逐字节
相等、仍 1 胜；损坏证明未被擅自修复/改写（`winner_display_name` 保持 NULL）。
HTTP 503 断言不变，且不再为检查缓存去 inspect 损坏源。

**（2）`test_inspect_reads_one_consistent_snapshot_while_a_projection_is_uncommitted`
（必红 → 绿）** 原用例两局复用同一 `SEATS` 的同一 winner member，两胜实际只落
1 个 entry，却断言 `late.cached_entry_count == 2`。按根裁决**保留强 2-entry
断言**：在途第二局改用不同合法 `member_openids`（显示名不变），使已提交快照
1 entry/1 胜 → 提交后 2 entry/2 胜；保留 `early.cached_entry_count ∈ {1,2}`、
`late == 2`、`late totals == 2` 与「cache/proofs 计数与总胜场不得撕裂」断言。
entry 按**冠军成员聚合行**定义，不是 completed 对局数。

**（3）`test_inspect_flags_same_name_member_level_mismatch`（假绿 → 真证）**
原用例用 `CACHE_MUTATIONS["last_won_at"]`（全部行 `+1h`）制造差异，安全的公共
entry 列表比较也能识别，未真正钉成员 key。改为**先断言两名同名成员真实落库的
`last_won_at` 不相等**，再在同一事务内**互换这两行的时间**（只改本 case 的两个
PK，不动 completed 证明）：时间值集合、总 wins、`completed_*` 计数、排序后的
公共 `entries` 全部与 baseline 相同，只有 `member_openid → last_won_at` 映射错位
⇒ `consistent=False` / `last_won_at_mismatch`。即仅逐成员比对才能发现。

新增测试内原始 SQL 读写助手 `_cache_rows` / `_swap_cached_last_won_at`（均在本
测试文件内，无外部探针）；`CACHE_MUTATIONS["last_won_at"]` 仍由单 entry 的
参数化用例 `test_inspect_reports_the_exact_cache_divergence[last_won_at]` 使用。

**执行记录**（环境同 §14.5：worktree `.../worktrees/tsk-279`，branch
`pi/TSK-279-runtime-recovery`，HEAD `d869bfe575986ddb94a509f7bd051174045d4000`，
root venv `/Users/derbay32/project/komari-bot/.venv/bin/python` 3.13.11；PG/Redis
门控 `postgresql+asyncpg://komari_test@127.0.0.1:55458/komari_tsk279_resume`、
`redis://127.0.0.1:56358/15`）：

| 命令 | 结果 |
|---|---|
| `ruff check`（PG 测试文件） | ✅ All checks passed |
| `ruff format --check`（PG 测试文件） | ✅ 1 file already formatted |
| `ruff check`（C2 3 测试/支撑文件 + 2 个 management fixture） | ✅ All checks passed |
| `pyright --pythonpath <root venv>`（PG 测试文件） | ✅ 0 errors / 0 warnings |
| `pyright --pythonpath <root venv>`（全仓） | ✅ **0 errors / 0 warnings**（§14.7 的 16 个设计内 RED 随生产接缝落地全部消失） |
| `pytest …/test_tsk279_leaderboard_management_pg.py -q`（门控） | ✅ **16 passed**（修复前 14 passed / 2 failed） |
| `pytest`（C2 四文件：`test_tsk279_management_api.py` + PG 文件 + `test_management_api_runtime.py` + `test_plugin_integration.py`，门控） | ✅ **51 passed**（修复前 2 failed / 49 passed） |
| 同四文件重复 2 次 | ✅ 51 passed / 51 passed（无抖动） |

**是否新暴露生产 bug**：否。三处修复后全绿，且第 3 例的成员级错配确实由生产
`inspect_leaderboard` 的逐 `member_openid` 对照捕获（若生产只比显示名/总胜场，
该用例必红），说明生产比对语义正确。

**边界**：本轮只改 `tests/komari_roulette/test_tsk279_leaderboard_management_pg.py`
与本合同；C1 的 6 个 staged 生产文件与全部 C2 生产未暂存/新文件原样未动，
HEAD / 分支 / 索引均未变，未提交、未清表、未跑 full suite、未 deselect。

### 14.9 C2 错误路径回归（test-only，固定外壳三 code 全 503，2026-09-11）

独立源审发现生产 `komari_bot/plugins/komari_roulette/management_api.py` 的错误分支
违反 §14.1 冻结外壳。本轮**只**补回归用例（生产由 TSK-279 本体另人实现）：

1. **rebuild `session.commit()` 未被 storage 包装**：commit 抛 SQL/连接错误 → inner
   `except Exception` 直接返回 500 `{"detail": "轮盘管理接口内部错误"}`，而非 503
   `roulette_storage_unavailable`。
2. **status `.as_dict()` 在 try 外**：快照投影协作者抛错无人接住 → FastAPI 默认 500
   （`Internal Server Error`），而非 503 `roulette_status_unavailable`。
3. 其余 generic inspect/rebuild（非两已知 storage/aggregate 异常）同样落 500 内部错误壳。

**本轮新增/加强（测试专用，不动生产）**

| 用例 | 性质 | 冻结断言 |
|---|---|---|
| `api::test_status_projection_failure_fails_closed_without_leaking` | RED（新） | 投影失败的协作者（`as_dict()` 抛 canary）→ 503 `roulette_status_unavailable` 固定外壳、无 canary、零审计；故意非 `RouletteObservation` 协作者对 `object` 接缝合法 |
| `api::test_rebuild_commit_failure_fails_closed_without_partial_state` | RED（新） | commit 抛 generic RuntimeError → 503 `roulette_storage_unavailable`、`commit_attempts==1`/`commits==0`、恰好一次 rebuild、一个已退出 session、审计零 `succeeded` |
| `api::test_inspect_failures_use_the_fixed_error_envelope[RuntimeError-…]` | RED（既有参数化 +1） | generic inspect 失败同样 503 固定外壳，且响应体**不是**空排行榜（`set(body)=={"detail"}`、无 `entries`） |
| `api::test_rebuild_failures_use_the_fixed_error_envelope[RuntimeError-…]` | RED（既有参数化 +1） | generic rebuild 失败同样 503 固定外壳、零 commit 尝试 |

支撑改动（`tsk279_management_support.py`，测试专用）：

- `_FakeSession` / `FakeSessionFactory` 新增 `commit_error` 注入与 `commit_attempts`
  计数，区分「commit 尝试一次」与「成功提交 0」；`_FakeSessionContext` 退出时标记
  `closed`。`commit_error` 是**已知**失败（应用前抛出），故可断言无成功提交；**不**
  宣称真实网络 commit 结果未知时必然回滚（PG 语义如要覆盖另报，不扩本票）。
- `asgi_client` 新增 `raise_app_exceptions: bool = True`；置 `False` 时观察 FastAPI
  默认 500 响应而不被 ASGI 传输重抛（默认值不变，其余用例零影响）。

**根裁决遵守**：统一沿用 §14.1 原三 code、统一 503，不新增 code；不改权限/校验
（400/401/403/422）既有语义；新外壳仍只 `except Exception`（不吞 `CancelledError`）。
共享 `management_audit_span` 的 `started` fail-closed / final 吞错语义不复测、不重写。

**RED 证据**（环境：worktree `.../worktrees/tsk-279`，branch
`pi/TSK-279-runtime-recovery`，HEAD `d869bfe575986ddb94a509f7bd051174045d4000`，
root venv `/Users/derbay32/project/komari-bot/.venv/bin/python` 3.13.11；门控
`postgresql+asyncpg://komari_test@127.0.0.1:55458/komari_tsk279_resume`、
`redis://127.0.0.1:56358/15`）：

| 命令 | 结果 |
|---|---|
| `ruff check`（2 测试/支撑文件） | ✅ All checks passed |
| `pyright --pythonpath <root venv>`（2 文件） | ✅ 0 errors / 0 warnings |
| `pytest …/test_tsk279_management_api.py -q` | **4 failed / 26 passed** |
| `pytest …/test_tsk279_leaderboard_management_pg.py -q`（回归） | ✅ 16 passed |

4 例首错：`test_status_projection_failure_fails_closed_without_leaking` 为
`AssertionError: Internal Server Error`；另 3 例为
`AssertionError: {"detail":"轮盘管理接口内部错误"}`（均来自支持文件
`assert_error_envelope` 的 status 断言）。即现状是默认 500 / generic 500 内部错误壳，
正是本轮要收敛到 503 固定外壳的缺口。

**边界**：本轮只改 `tests/komari_roulette/test_tsk279_management_api.py`、
`tests/komari_roulette/tsk279_management_support.py` 与本合同；生产 `management_api.py`
及 C1 6 个 staged 文件、全部 C2 生产未暂存/新文件原样未动，HEAD / 分支 / 索引均未变，
未提交、未跑 full suite。`ruff format --check` 的漂移为仓库现存 ruff 版本差异（未改的
management fixture 同样 “would reformat”），非本轮引入，未擅自重排。

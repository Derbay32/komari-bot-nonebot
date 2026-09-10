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
| 已安装真实 handler 在组锁内复核撤权 → 零 receipt/零发送 | `effect::test_installed_handler_rechecks_authority_inside_the_group_lock` | **业务断言 RED（缺锁后 effect 复核）** |

### 13.3 RED / GREEN 分类（Stage-C1 返工后实测：29 failed / 4 passed）

| 桶 | 用例 | 实测首错 |
|---|---|---|
| 缺 seam（lifecycle 模块不存在） | 11 个 `lifecycle::*` + 6 个 `lifecycle_pg::*` | `AssertionError: 生产未注册恰一个 komari_roulette driver startup hook，实际 0` / `ModuleNotFoundError`/`AttributeError` |
| 缺签名（`effect_check`） | `effect::…exposes_effect_check_keyword`、`…is_evaluated_only_after…`、`…propagates_storage_errors`、`delivery::…exposes…`、`delivery::…blocks_network…`、`delivery_variant…`、`installed::…accepted_effect_check_still_honours_expired_window` | `TypeError: … got an unexpected keyword argument 'effect_check'` / `AssertionError: 'effect_check' in mappingproxy(...)` |
| 缺符号（`EffectCheckRejectedError`） | `…leaves_zero_effects`、`handler::…silently_absorbs…` | `ImportError: cannot import name 'EffectCheckRejectedError'` |
| 业务断言（handler 未传 per-call closure） | `handler::…original_token_closure…`、`concurrent_handlers…` | `AssertionError: the handler must pass the per-call effect_check to the service / each handler call must pass its own post-lock closure` |
| 业务断言（已安装 handler 缺锁后复核） | `installed::test_installed_handler_rechecks_authority_inside_the_group_lock` | `AssertionError: an authority revoked while the command queued must leave no domain receipt`（receipt 被提交） |
| 业务断言（`maintenance.close` 立即返回） | `lifecycle_pg::test_maintenance_close_waits_for_the_in_flight_cleanup_round` | `AssertionError: maintenance.close() must wait for the in-flight cleanup round instead of returning immediately`（`assert not True`） |
| **GREEN（不依赖 lifecycle 的真实旧 API 探针）** | `effect::test_old_api_authority_chain…`、`effect::test_real_delivery_true_runtime_check_with_expired_pg_window_never_sends`、`effect::test_installed_matcher_commits_valid_token_and_captures_real_sdk_payload`、`effect::test_installed_matcher_business_gate_reads_live_authority_for_same_token` | ✅ 4 passed（真实 `AdmissionRuntime` + 真实 `BindingTransaction` + 真实 `user_ban` + 真实 `RouletteCommandService`/`RouletteDelivery` + 真实 QQ adapter 传输录制） |

未使用 `fakeSender`/import error 单独宣称任一 AC 通过；缺 seam 用例在被测符号存在
后才会执行真实业务断言。四条 GREEN 均不依赖 `lifecycle` 模块，证明 C1 不是靠
「`QQruntime != None` + 全 true 门」才能通过。

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
| `pytest .../test_tsk279_lifecycle.py .../test_tsk279_lifecycle_pg.py .../test_tsk279_effect_recheck_pg.py -q`（带门控，C1 返工后，HEAD `9592c74`） | 4 passed / 29 failed（4 GREEN 旧 API 探针；29 缺 seam/签名/符号/业务断言 RED，详见 §13.6） |
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

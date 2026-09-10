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

# TSK-272 纯领域测试契约

本目录只测试 `komari_bot.plugins.komari_roulette.domain` 的公开领域边界。测试不导入 NoneBot、OneBot、SQL、character_binding ORM 或发送器；平台身份由测试构造的 `PlayerRef` 值对象传入。

## TSK-275 PostgreSQL storage seam

TSK-275 adds a separate persistence seam.  The tests in this directory use the
following public names; an implementation may split modules internally, but it
must keep these imports stable (the top-level plugin package may re-export
them):

```python
from komari_bot.plugins.komari_roulette.mapper import (
    GameSnapshot,
    StateTransition,
    TerminalProjection,
    game_state_from_snapshot,
    game_state_to_snapshot,
    transition_from_action_result,
)
from komari_bot.plugins.komari_roulette.storage import (
    AggregateCorruptError,
    PostgresRouletteStorage,
    RevisionConflictError,
    StorageUnavailableError,
    TerminalProjectionRejectedError,
)
```

`PostgresRouletteStorage(session)` receives an already opened SQLAlchemy
`AsyncSession`.  It never commits, rolls back, or opens a second transaction;
the caller owns one unit of work and can combine a state transition, terminal
projection, and leaderboard update atomically.  `GameSnapshot` carries the
storage generated `game_id` alongside the trusted `GameState` fields.  Public
operations are:

```python
load_current(group: GroupRef, *, for_update: bool = False) -> GameSnapshot | None
create_waiting(snapshot: GameSnapshot) -> GameSnapshot
save_transition(transition: StateTransition, *, expected_revision: int) -> GameSnapshot
project_terminal(projection: TerminalProjection)
get_result(group: GroupRef, game_id: str)
list_leaderboard(group: GroupRef)
rebuild_leaderboard(group: GroupRef)
```

The test construction helper is `GameSnapshot.from_state(state,
game_id=...)`; the mapper functions must round-trip that value without losing
any trusted field.

`transition_from_action_result(before, result, action_kind=..., occurred_at=...)`
is the only test construction seam for a state change.  It carries both the
previous trusted snapshot and the domain result, so the adapter can record
elimination order/reason/time even though the pure `GameState` intentionally
does not contain that history.  `TerminalProjection.from_state(snapshot,
lifecycle=..., reason=..., ended_at=..., winner_seq=...)` is only used after
the corresponding waiting/join/start/action transitions have been persisted;
the adapter must verify the current root and runtime history before projecting
an immutable result.  Callers never provide raw SQL rows or an arbitrary
winner for a game that was not stored.

The return values are structured DTOs or `GameState` snapshots, never ORM
objects.  `game_state_to_snapshot()` preserves the trusted fields needed to
resume a game: `next_join_seq`, ordered chamber, pending reward queue,
`pending_locks`, frozen player names and item weights.  The inverse mapper
returns an immutable `GameState` and validates the persisted aggregate.

`TerminalProjection` contains the complete immutable result proof (group key,
game id, lifecycle/reason, creation/start/end times, terminal revision,
ordered frozen seats, final player status/elimination metadata, and winner).
`get_result()` returns a structured result whose player entries expose the
frozen `join_seq`, identity, display name, final alive flag, elimination order,
elimination reason, and PostgreSQL timestamps; `list_leaderboard()` exposes
safe display fields, `wins`, and `last_won_at`, and never includes an openid in
a user-facing field.
The storage adapter writes the result and winner projection in the caller's
transaction, then removes every consumable runtime row/secret.  `completed`
is the only lifecycle that can add a win; a duplicate result is a read of the
existing proof and never increments wins again.  `cancelled`, `expired`, and
`failed` have no winner.

Storage failures raise `StorageUnavailableError` and do not change lifecycle
or fabricate an empty game.  A successfully read aggregate that violates a
cross-row invariant raises `AggregateCorruptError` (the caller may perform a
controlled safe `failed` projection with a whitelist reason code).  Stale
`state_revision` writes raise `RevisionConflictError` and leave all rows
unchanged.  A terminal projection for an absent game, a non-terminal game, or
one without exactly one eligible winner raises
`TerminalProjectionRejectedError` and leaves the current game/result rows
unchanged.

The model/migration tests lock these relation names and database-local
constraints:

* `komari_roulette_games`, `komari_roulette_players`,
  `komari_roulette_results`, `komari_roulette_result_players`, and
  `komari_roulette_leaderboard`;
* `waiting`/`active` partial uniqueness on `(app_id, group_openid)`;
* constrained `TEXT[]` chamber/reward arrays and closed lifecycle/item
  elements;
* non-negative four-item inventory with a total of at most four;
* player/result foreign keys and one result per `game_id`;
* leaderboard scope `(app_id, group_openid, member_openid)` and wins ≥ 1.

The mapper/storage tests are intentionally red until these production seams
and Alembic revision `0019` exist.  They do not treat a missing PostgreSQL or
Redis service as a passing result.

## 最小公开入口

实现需要提供以下窄入口（模块可以拆分，但这些符号必须由插件顶层或 `domain` 模块稳定导出）：

```python
from komari_bot.plugins.komari_roulette.domain import (
    Action,
    ActionResult,
    ChamberKind,
    GameState,
    ItemType,
    PlayerRef,
    GroupRef,
    apply_action,
    initial_state,
)
```

`initial_state(group)` 创建一个没有进行中游戏的 `GameState`。`apply_action(state, action, *, now, random_source)` 是唯一的领域变更入口：它接收完整可信 `GameState`，返回 `ActionResult`，不在测试中调用 repository、ORM 或 matcher。

仓储恢复测试可以使用 `GameState.from_trusted_snapshot(snapshot)` 构造合法的可信领域输入。`snapshot` 至少包含上面列出的生命周期/回合/修订/席位字段、`ordered_chamber`、`pending_rewards`、已冻结的 `item_weights` 与道具效果状态（包括 `pending_burst`、`pending_locks`）；`pending_locks` 的目标使用冻结 `join_seq` 表示。该入口只接收可信持久化适配器输入，不把普通用户结果或不受信任消息直接反序列化为状态。

`Action` 是不可变的命令值对象，至少提供以下构造器：

```python
Action.create(player)
Action.join(player)
Action.leave(player)
Action.cancel(player)
Action.start(player, item_weights=None)
Action.shoot(player)
Action.forfeit(player)
Action.end_turn(player)
Action.reload(player)
Action.use_item(player, item, target_seq=None)
Action.discard_item(player, item)
Action.choose_item(player, decision, replace_item=None)
Action.transfer(player, target_seq)
Action.open_item_panel(player)
Action.expire()
```

`Action.start` 接收纯值 `item_weights` 并在 waiting→active 成功时冻结副本；调用方随后修改原映射不能改变进行中对局的抽样权重。`choose_item` 的 `decision` 为 `discard` 或 `replace`；失败选择返回失败结果且不续期。合法数字在当前阵容中不存在（包括失效旧 `join_seq`）返回 `player_seq_not_found`；已淘汰目标返回 `invalid_item_target/eliminated`；自己返回 `invalid_item_target/self`；已被待锁占用返回 `item_effect_conflict/target_already_locked`，并且这些失败均不消耗锁。

`PlayerRef` 必须携带已验证的应用/群/成员协议身份和非空冻结显示名；测试不把裸 QQ 号、昵称或 `character_binding` 内部记录当作身份。`GroupRef` 至少区分 `app_id` 与 `group_openid`。

玩家资格比较完整的 `(app_id, group_openid, member_openid)`，同一 `member_openid` 在其他应用或群中不能操作本局。

随机与时间均在边界注入。测试 fake 提供 `chamber_order(live_count, blank_count)` 和 `weighted_item(weights)`；实现可以采用兼容的结构化协议，但不能从全局随机源、系统时钟或数据库读取。随机源抛出异常或返回非法结果时，动作必须返回 `random_source_failed` 并保持输入状态不变。

## 结果与可信状态的分层

`ActionResult` 至少提供：

- `ok: bool`、`code: str`、`reason: str | None`；
- `state: GameState`，供下一次领域动作和持久化恢复使用；
- `reply`，只含可安全给上层投影的结构化结果。

成功 `code` 可以区分动作完成与终局原因；终局语义以 `state.lifecycle` 和 `reply.completion_reason` 为准，测试不要求“射击终局”和“弃权终局”共享某个动作码。

`GameState` 可保留完整有序弹仓、当前首项、尚未处理的预抽奖励（`pending_rewards`）、库存、待连发与待锁等可信耐久事实；它不能被当作普通用户结果直接序列化。`reply` 不得包含完整弹仓、当前消费后首项、未处理奖励类型、随机种子、异常正文或任何内部 ORM 对象。item_choice 只公开当前待选奖励和其后尚未处理的件数（`pending_item_count = len(pending_rewards) - 1`），后续预抽类型必须保密。

为便于恢复和领域续跑，`GameState` 的公开可信字段包含 `lifecycle`、`phase`、`state_revision`、`chamber_revision`、`turn_seq`、`current_player_seq`、`deadline`、`host_seq`、`players` 与 `ordered_chamber`；`players` 是按冻结顺序排列的不可变席位快照，每席至少有 `join_seq`、`member_openid`、`display_name`、`alive`、`inventory`。这些字段属于可信输入状态，不是普通消息结果。

顶层生命周期是 `waiting | active | completed | cancelled | expired | failed`；`first_shot | follow_up | locked_turn | item_choice` 是 active 内阶段/覆盖态。可信状态始终独立携带 `state_revision` 和 `chamber_revision`：创建的 state revision 为 1，成功外层动作恰增 1；开始建立 chamber revision 1，每次消费/重建/手动装填按弹仓契约递增。只读道具面板不递增任一修订号；成功使用放大镜会消耗库存并续期，因此递增 `state_revision`，但放大镜观察本身不递增 `chamber_revision`。

## 行为覆盖

测试固定并可追溯到 TSK-260～263、TSK-266 与 TSK-268：

1. 等候阶段支持创建、加入、重复加入、满员、退出、局主转让、取消、超时和开始；人数 2–6，`join_seq` 严格递增且退出重入不复用（编号可大于 6），开始冻结顺序/名字并拒绝迟到入席。
2. 开始建立有序 6 发弹仓，初始 2 实 4 空；射击与啤酒逐发消费，`empty` 优先于 `no_live` 归一化为新 2 实 4 空；即使最后一发实弹会淘汰当前玩家或连发首发实弹立即停止，消费后的弹仓仍先完成必要归一化，归一化随机失败时整次动作回滚；手动装填保留剩余实弹并添加恰好 1 发实弹、补空至 6、轮转。
3. `first_shot`、`follow_up`、`locked_turn` 和 `item_choice` 分别验证合法/非法动作。首发实弹立即停止连发；待连发随手枪跨回合保留；啤酒与待连发冲突；普通主动丢弃只在 first_shot/follow_up 合法；锁在下一次实际获得回合时触发，淘汰/终局清理。
4. follow_up 全空射击按发预抽奖励；权重可注入且开始时冻结；满仓从首个满槽开始进入 item_choice，逐件支持丢弃新物或按类型替换旧物，包括同类替换；安全结果只返回当前待选类型及不依赖后续类型的未处理件数（不含当前项）；超时/弃权只丢弃未处理队列。
5. 成功和失败动作的 15 分钟续期语义（迟到很久的超时轮转从提交时点重新计算期限）、过期先结算、唯一胜者终局不建立下一回合、随机失败无提交、`state_revision` 与 `chamber_revision` 独立均由可观察状态断言。

测试不验证随机频率、不锁定实现类层级、不访问私有 collaborator；常数仅使用已决议的 2 实 4 空、最大 6 人/发、15 分钟绝对期限与四类道具。

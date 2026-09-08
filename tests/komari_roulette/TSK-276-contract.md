# TSK-276 测试契约

本票测试只锁定轮盘命令事务的可观察边界。实现可以拆分模块，但
`komari_bot.plugins.komari_roulette` 顶层应提供下列窄入口（名称若有等价
命名须保持同等语义）：

```python
from komari_bot.plugins.komari_roulette import (
    CanonicalCommand,
    CommandReceipt,
    CommandRequest,
    CommitOutcomeUnknownError,
    ExpiryAdvance,
    FulfillmentClaim,
    FulfillmentConflictError,
    FulfillmentState,
    IdempotencyKeyConflictError,
    Observation,
    ReplyProjection,
    ReplyProjectionContext,
    RouletteCommandService,
    StateConflictError,
)

service = RouletteCommandService(
    session_factory=session_factory,
    reply_projector=projector,
    random_source=random_source,
)
receipt = await service.execute_group_command(request, observation=observation)
observation = await service.observe_current(group)
expiry = await service.advance_expired(group, observation=observation)
claim = await service.claim_fulfillment(receipt.receipt_id)
await service.mark_delivered(claim, platform_message_id="...")
await service.mark_not_delivered(claim)
```

`session_factory` 是唯一的事务拥有者；服务自己打开并关闭
`AsyncSession`，且只在一个明确边界 `commit()`。binding facade、roulette
storage、receipt repository 和 fulfillment repository 共用此 session，不能
各自 commit、rollback 或新开 session。回复投影器接收结构化安全
`ReplyProjectionContext` DTO，返回
结构化 `ReplyProjection(body: str, metadata: Mapping[str, str | int | bool])`
冻结载荷；不能以 `Any`/`object` 逃逸到 handler 或把当前状态延后拼入回复。

`CommandRequest` 固定包含 `(app_id, group_openid, inbound_msg_id)`、调用者
`member_openid`、规范 `CanonicalCommand`、目标 mention 数量和必要的已验证
目标身份。`CanonicalCommand` 只含 typed intent/参数（例如 `target_player_seq`、
道具类型、选择；只读 `open_item_panel`/`leaderboard` 也属于规范 intent），不含
原始正文、目标 openid、昵称或事件对象。测试允许以
工厂/构造器生成这些值对象，但不接受在 service 入口传入 raw QQ 文本。

`Observation` 独立于 request 和指纹，至少是
`(game_id, state_revision, turn_seq)`。同 key 同 fingerprint 重投必须直接读
第一次收据，即使 observation、配置、随机源和当前局面已经变化；同 key 异
fingerprint 抛稳定 `IdempotencyKeyConflictError`，不产生第二张假收据。不同
`inbound_msg_id` 不要求 fingerprint 相同。

履约确认以 typed claim 为入口：对已 `DELIVERED` 收据重复确认同一
`platform_message_id` 幂等成功；不同平台消息 ID 抛稳定
`FulfillmentConflictError`，原 ID 与状态保持不变。

## 13 项验收与测试入口

| AC | 测试观察与最小并发/故障方法 |
| --- | --- |
| 1 | 两个真实 PG `AsyncSession` 并行 create；以 `pg_blocking_pids`/短暂 row-lock 等待证明由数据库唯一活动槽裁决。一个 receipt+game 成功，另一个稳定 `game_already_exists`，不同 app/group 各自成功。 |
| 2 | 多连接并行 waiting join/start/cancel/leave；每次事务锁后重读 root。最后席位只成功一条，`join_seq` 严格递增，leave/rejoin 不复用旧编号，旧 target seq 不指向新席位。 |
| 3 | 同一 UoW 通过 `BindingTransaction(session)` 读取当前 group/member binding，再创建/加入；并发 clear/rename/new join 用同一 group scope lock。无绑定、空名、名字规范化键冲突都 fail-closed；已入席冻结名字不被后续改名/清名回写。binding 的独立 group resolver 对确实未映射返回 `None`，PG/存储失败抛明确异常。 |
| 4 | 两个 active/item-choice request 使用同一 `(game_id, state_revision, turn_seq)`，真实 PG 行锁串行；首个合法动作成功，后者固定 `state_conflict`，无随机/库存/期限副作用；重新观察新版本后动作可成功。只读道具面板/排行榜忽略旧 observation，在锁后按最新局面投影，不增 revision、不续期。 |
| 5 | `execute_group_command` 与 `advance_expired` 并行，锁后用 `SELECT clock_timestamp()`；同一旧 deadline 只推进一次。停机补偿最多淘汰旧期限玩家，新玩家得到完整期限。不可用本地/QQ event 时间替代 PG 时间。 |
| 6 | 同 key 同指纹并行/重投只读原 receipt，不重做 expiry/action/random/projector；同 key 异 payload 抛 `IdempotencyKeyConflictError`，不写第二张 receipt、不改变原 receipt、不发送。 |
| 7 | 固定 request 后改变 observation、运行时配置、projector/random 和游戏状态，重投仍返回原 fingerprint/result/reply；fingerprint 只由规范命令类型/参数、调用者、目标编号与 mention 数等安全字段组成，不落 raw 正文。 |
| 8 | typed syntax failure 可生成一次安全失败 receipt 且不推进 expiry；普通文本与现有 help 在 service 外被拦截，不能建 receipt、读游戏或续期。测试不伪造 QQ handler。 |
| 9 | 在同一真实事务注入 domain/storage/result/receipt 任一写入失败，断言游戏、terminal result、leaderboard、receipt/fulfillment 全回滚；无 terminal 半成品、胜场或成功无 receipt。禁止先 commit terminal 再写结果。 |
| 10 | 用真实 PG 精确 commit hook/窄连接包装器分别注入“已提交但响应丢失”和“未提交”。第一次调用均抛 `CommitOutcomeUnknownError` 且不发送；原 key 重试时前者读原 receipt，后者重新执行；不得猜测新 key。 |
| 11 | 在首次事务内注入 deterministic projector，提交后变更当前游戏/配置，再读 receipt；reply 字节/结构完全不变。普通正文不含有序弹仓、完整 pending reward、随机种子或异常正文；合法单人 mention 目标可在独立 metadata。 |
| 12 | 两个真实 PG worker 对同一 receipt 并行 claim，只有一条 `NOT_STARTED -> PENDING_CONFIRMATION`；typed claim 只能对应该 receipt，receipt replay 不取得第二发送权。发送前明确失败记 `NOT_DELIVERED`；已开始后不确定保持 `PENDING_CONFIRMATION`；履约不改 revision/wins。 |
| 13 | 真实 PG 断连/事务失败抛 infrastructure error，不返回“无局”或伪造 failed。只有 `PostgresRouletteStorage.fail_corrupt_current()` 确认损坏时，才在同一 UoW 生成受控 failed 安全结果。 |

## 固定锁与入口边界

共享锁位于纯 `komari_bot.db.group_transaction_locks`，公开
`lock_group_scope(session, *, app_id, group_openid)`；roulette 与
character_binding 都消费它。BindingTransaction 是 caller-owned facade，至少
提供独立的 group mapping resolver 以及 resolve member、bind、rename、clear
操作。`character_binding` 不反向 import roulette；普通 manager 写入与事务 facade
复用相同锁序。resolver 的 `None` 只表示真实未映射，PG/存储异常必须原样以
明确异常暴露，不能变成 274 可放行的 unknown group。

绑定 facade 的测试构造使用以下 caller-owned 形状：

```python
tx = BindingTransaction(session)
await tx.bind(
    app_id=..., group_openid=..., member_openid=..., character_name=...,
    group_id=..., member_qq=...,  # 协议映射需要时提供
)
await tx.rename(..., character_name=...)
await tx.clear(...)
group = await tx.resolve_group(app_id=..., group_openid=...)
member = await tx.resolve_member(
    app_id=..., group_openid=..., member_openid=...
)
```

这些方法从不自行 commit；写入成功后由命令服务或调用者统一提交。锁后必须
重新核实当前关联仍对应原 `(app_id, group_openid, member_openid)` 身份，不能
只相信 manager 的旧缓存解析出的 canonical target。

服务锁序固定为：receipt identity → shared group scope → binding group/member/name
→ roulette current root/players。`advance_expired(group, observation=...)` 返回无
receipt 的结构化 `ExpiryAdvance`，是 279 worker 的无 msg_id 深入口，不创建收据、
不发送；用户
`CanonicalCommand` 不能伪装成 expire 内部动作。所有关键期限取锁后
`clock_timestamp()`。

数据库新增一条唯一 Alembic revision `0020`，维护独立的
`komari_roulette_command_receipts` 与 `komari_roulette_fulfillments` 元数据表；
不复用 `komari_chat` outbox。历史 `0018/0019` 的 binding/roulette
阶段断言保留，current head 只允许单一 `0020`。

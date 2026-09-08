# TSK-274 测试合同

本文件固定 TSK-274 的最窄可观察公共接缝。测试只通过这些接缝观察 QQ
事件资格、临时取证会话和效果前重审；不调用私有 runtime、SQLModel 元数据、
QQ 网络或真实发送器。

## `group_admission` 顶层公共面

`adjudicate(Collection[int], *, intent=...)` 保持既有同步整数群号契约。
QQ 入口新增以下顶层符号，并由 `__all__` 明确导出：

```python
QQ_ADMISSION_STATE_KEY: str

class QQAdmissionToken: ...
class QQBindClaim: ...
class QQInitialBindRequest: ...
class QQVerifiedBindingSession: ...
class QQEffectDecision: ...

async def qualify_qq_event(bot, event) -> QQAdmissionToken | None: ...
def get_qq_admission_token(state) -> QQAdmissionToken | None: ...

def register_qq_group_resolver(
    resolver | None,
    *,
    member_resolver | None = None,
) -> None: ...
def register_qq_initial_bind_claimer(
    claimer | None,
    *,
    validator | None = None,
) -> None: ...
def register_qq_binding_session_resolver(resolver | None) -> None: ...
def register_qq_ban_checker(checker | None) -> None: ...
async def recheck_qq_effect(token, *, effect) -> QQEffectDecision: ...
```

注册回调是显式装配接缝，不在 `group_admission` 中反向 `require` 或 import
`character_binding` / `user_ban`。

`register_qq_ban_checker` 是可选的显式封禁装配接缝，接收已由 QQ evidence
证实的 numeric QQ 号与业务封禁 scope，返回当前是否命中封禁。它不能从
`GroupAtMessageCreateEvent.get_user_id()` 推导 QQ 号；没有可信 `member_qq` 时
不调用。封禁服务异常按准入故障关闭处理。该接缝由 `user_ban` 或绑定协调器在
插件装配时注册，避免 `group_admission` 反向 require `user_ban`。

### 不可变值对象

`QQBindClaim` 字段集合与语义固定（实现宜使用关键字构造；不要求调用方依赖
字段声明顺序）：

```text
session_code, app_id, group_openid, member_openid, qq_message_id,
connection_generation, expires_at, is_new
```

`QQInitialBindRequest` 字段集合与语义固定（实现宜使用关键字构造；不要求调用方依赖
字段声明顺序）：

```text
app_id, group_openid, member_openid, qq_message_id, command
```

其中 `command` 必须是适配器剥离官 Bot @ 后、去除外层空白的精确 `/bind`。

`QQVerifiedBindingSession` 字段集合与语义固定（实现宜使用关键字构造；不要求调用方依赖
字段声明顺序）：

```text
session_code, app_id, group_openid, member_openid, qq_message_id,
group_id, member_qq, connection_generation, expires_at
```

`QQAdmissionToken` 字段集合与语义固定（实现宜使用关键字构造；不要求调用方依赖
字段声明顺序）：

```text
scope, app_id, group_openid, member_openid, qq_message_id,
group_id, member_qq, effective_policy_revision,
connection_generation, claim, verified_session
```

`scope` 的闭集为 `business`、`binding_challenge`、`binding`：

- `business`：已映射且当前策略允许的 QQ 群资格。`member_qq` 可以是
  `None`，不能因成员尚未绑定而在群门禁层拒绝；成员缺名由 271/276 的业务
  接缝处理。若有可信 QQ 才用于封禁检查，不能把 OpenID 转成 QQ 号。
- `binding_challenge`：未正式映射群的首次一次性 `/bind` 取证资格；只能
  产生唯一挑战，不能代表 BUSINESS。
- `binding`：可信 numeric 群号与成员 QQ 已由当前 generation 的 evidence
  证实，但正式群映射尚未完成时，允许后续绑定流程继续到确认。每次继续和
  确认仍需重审策略、封禁、TTL 与 generation。

`QQEffectDecision` 字段集合与语义固定（实现宜使用关键字构造；不要求调用方依赖
字段声明顺序）：

```text
allowed, effect, reason_code, effective_policy_revision
```

`effect` 也只允许上述三个值。`binding_challenge` 不能被重审为普通
`AdmissionResult.BUSINESS`；已核验 `binding` 不能因为没有正式 group resolver
映射而被一概拒绝。

## 回调语义

```python
QQGroupResolver = Callable[[str, str], Awaitable[int | None]]
```

参数是 `app_id, group_openid`。返回 `None` 只表示确实未映射；异常表示存储
失败，必须拒绝且不得转入首次未知群例外。`BindingTransaction.resolve_group()`
返回的 `GroupBindingGroup.group_id` 由 character_binding 装配层规范化为正整数
后再交给此接缝；群映射没有成员也必须成功解析。

`register_qq_group_resolver` 的可选 `member_resolver` 形状为
`Callable[[str, str, str], Awaitable[int | None]]`，参数依次是 `app_id`、
`group_openid`、`member_openid`。它只返回数据库确认的 canonical numeric QQ；
返回 `None` 表示没有可靠成员映射，异常必须拒绝当前资格。已映射且 admitted
群的 BUSINESS 资格不能因此强制要求成员已绑定，但有 canonical QQ 时必须把它
传给封禁 facade；即便 OpenID 恰好全是数字也不能代替该值。

```python
QQInitialBindClaimer = Callable[
    [QQInitialBindRequest],
    Awaitable[QQBindClaim | None],
]
```

一次性与 10 分钟绝对 TTL 由 character_binding 的真实临时会话协调器负责。
重复事件可以返回同一个 session 的 `is_new=False` claim，但成功 claim 数量
至多一次；不可用当前 claim 续期或创建第二个挑战。

`register_qq_initial_bind_claimer` 的可选 `validator` 接收待重审的
`QQAdmissionToken` 并返回 awaitable bool。它只验证协调器仍持有的原始 claim；
不能通过客户端重构相同字段的 token。validator 与当前有效策略、TTL、generation
共同决定 challenge 是否仍可产生效果。

```python
QQBindingSessionResolver = Callable[
    [str, str, str],  # app_id, group_openid, member_openid
    Awaitable[QQVerifiedBindingSession | None],
]
```

该 resolver 查询当前 generation 的已核验临时会话。它独立于正式 group
resolver，供未正式映射但 evidence 已通过的 `/bind continue` 等后续步骤使用。
返回 `None` 或过期/旧 generation 都不能放行；不能只凭客户端重新构造同字段
dataclass 获得资格。

## event preprocessor 与 handler 交接

唯一全局 `event_preprocessor` 接收 `bot, event, state`。QQ 入口只接受精确
`GroupAtMessageCreateEvent`；callback、interaction、普通群消息、C2C、频道、
子类和伪造同名类均拒绝。

成功时 preprocessor 将不可变 `QQAdmissionToken` 写入
`state[QQ_ADMISSION_STATE_KEY]`。`get_qq_admission_token(state)` 只读，不 pop。
277 handler 必须读取该 token，不能在 handler 再次调用 `qualify_qq_event`。
未映射首次 `/bind` 的 claim 因此不会被 preprocessor 消耗后再次拒绝。

对未映射群，gate 先查询 verified-session resolver：已有当前 admitted evidence
时返回 `binding`，不再发挑战；没有当前 session 时仅精确 `/bind` 可请求一次
`binding_challenge`。其他向导语法由 277 根据既有 token/session/步骤处理，274
不解析向导、不发送 QQ 消息。

对已映射群，当前策略允许即返回 `business`，不新增成员绑定门槛；成员未知时
后续绑定证据由 binding 协调器处理。

## coordinator 生命周期与证据

character_binding 顶层导出 `QQBindingCoordinator`。构造器只接收启动装配提供的
每 app collector/fetcher、caller-owned ORM session factory 和 clock，不创建或
销毁共享 ORM engine：

```python
class QQBindingCoordinator:
    def __init__(
        self, *, collectors, group_resolver, clock
    ) -> None: ...
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    def reset_generation(self) -> None: ...
    async def cancel(self, session_code: str) -> None: ...
    async def claim_initial_bind(
        self, request: QQInitialBindRequest
    ) -> QQBindClaim | None: ...
    async def accept_reply_evidence(
        self, evidence: ReplyEvidence
    ) -> QQAdmissionToken | None: ...
    async def resolve_verified_binding_session(
        self, app_id: str, group_openid: str, member_openid: str
    ) -> QQVerifiedBindingSession | None: ...
    async def recheck(
        self, token: QQAdmissionToken, *, effect: str
    ) -> QQEffectDecision: ...
```

该真实临时会话协调器负责：

1. `start()`：接收启动装配提供的每 app `ReplyEvidenceCollector`，注册 group
   resolver、initial claimer、verified-session resolver 和可信身份注入；安装
   runtime collectors。
2. `accept_reply_evidence(evidence)`：只接收 273 collector 返回的 accepted
   evidence，立即以可靠 numeric `group_id` 调用整数 `adjudicate`；不自动发送
   QQ/OneBot 消息，返回或发布新的 `binding` token。
3. `reset_generation()`：使旧 collector、claim、session 和 token 失效；不
   延长 TTL。
4. `cancel(session_code)`：只撤销该临时会话及其 claim/evidence，不删除其他
   app、群或成员会话，不续期，也不改变正式绑定。
5. `close()`：先撤销所有注册接缝，再清理 collectors/session；不 dispose
   nonebot-plugin-orm 共享 engine。
6. `recheck(token, effect=...)` 委托 `group_admission.recheck_qq_effect`：每次提交、不可分业务效果和发送前
   重新验证权威群关系或 evidence、策略有效 revision、封禁、TTL 和 generation。
   该操作不消费初始 claim、不续期。

`user_ban` 通过显式可信 identity resolver/facade 取得 evidence 中的 numeric
`member_qq`；QQ OpenID（即便恰好全是数字）永远不进入通用 QQ 封禁查询。正常
OneBot `event_support` 行为保持不变，且 group_admission 不反向依赖 user_ban。

273 的 `ReplyEvidenceCollector.handle_event(event)` 调用仍然有效；双协议监听器
在处理已准入 OneBot 事件时必须把收到的 OneBot `bot` 作为可选显式参数传入
（`handle_event(event, *, bot=onebot_bot)` 或等价公共接缝）。取证 fetcher 只能
从这个事件对应的 OneBot bot 调用 `get_msg`，不能通过 `get_bot()`、QQ adapter
或“第一个 bot”猜测身份。QQ bot 即使同时在线也不得收到 OneBot 取证调用。

273 listener 另提供 `register_evidence_receiver(receiver | None)` 测试可观察的
装配接缝。collector 返回 `ReplyEvidence` 后，listener 必须在同一处理链中调用
当前 receiver；coordinator `start()` 注册自己的 `accept_reply_evidence`，
`close()` 撤销它。拒绝的 OneBot 事件和 `None` evidence 不得触发 receiver。
其 receiver 形状为 `Callable[[ReplyEvidence], Awaitable[object | None]]`，注册函数
同步返回 `None`，只保留一个当前接收者。

## 启动裁决

OneBot 始终注册。QQ 只在 `qq_bots` 非空时注册；QQ 账号为空时必须保持当前
`DRIVER=~fastapi` 的 OneBot 启动行为。不能仅 mock `register_adapter` 宣称
启动成功：有 QQ 配置时必须验证所选 driver 具备 adapter 所需 HTTP/WS 能力，
能力缺失应明确失败；当前仓库已有 `aiohttp`，不升级 QQ adapter 依赖。

启动配置另有 `qq_official_bot_qq_by_app: dict[str, str]`，按 QQ `BotInfo.id`
逐 app 绑定可信数字官 Bot QQ；它不写入 `BotInfo`，也不回退到 OneBot 登录号。
缺失或非正整数值只关闭对应 app 的首次取证挑战，并返回固定安全原因；不得在
日志中输出 token、secret、OpenID 或官 Bot QQ 原文。不同 app 的 collector 与
可信身份不得串用。

## AC → 测试映射

| AC | 测试 | 观察点 |
|---|---|---|
| 双协议启动、最小 Intent、零 QQ 不影响 OneBot | `test_qq_startup.py`、`test_qq_coordinator.py`、`test_qq_lifecycle.py::test_real_character_binding_startup_installs_and_closes_qq_admission` | 条件 QQ 注册、真实 adapter setup 能力、真实 driver 配置解析、显式 Intent、transport 保留、合法/非法 app collector 与初始 challenge、关闭后撤销资格、secret/身份不泄漏；未配置 app 无初始 claim |
| 精确 QQ @ 闭集与 mapped 策略矩阵 | `test_qq_event_gate.py` | mapped admitted 即 business（member_qq 可 None）、restricted/error 静默、其他事件拒绝 |
| 未映射一次挑战与 state 交接 | `test_qq_event_gate.py`、`test_qq_coordinator.py` | claim 至多一次、`is_new`、state token 不被消费 |
| 已核验未正式映射会话继续绑定 | `test_qq_coordinator.py` | session resolver 返回 binding，continue 不重新 challenge |
| evidence、效果前重审与策略撤销 | `test_qq_recheck.py` | business/binding_challenge/binding 分流、generation/TTL/LKG、无陈旧许可 |
| trusted QQ 封禁分流 | `test_qq_identity.py` | 数字形 OpenID 不冒充 QQ，真实 QQ 才查封禁，异常故障关闭 |
| 启停、真实 listener/collector 接线 | `test_qq_lifecycle.py`、`test_qq_lifecycle.py::test_real_character_binding_startup_installs_and_closes_qq_admission` | start/close/reset、真实插件 init/close hook、拒绝 OneBot 不进 collector、真实 evidence receiver、OneBot get_msg 路由、旧 token 关闭后拒绝、不 dispose ORM |
| OneBot 22 类闭集与 QQ 独立闭集 | `entry_gate_census.py`、`test_entry_gate_census.py` | 保持现有 OneBot census，不新增 matcher/事件旁路 |

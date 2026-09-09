# TSK-278 测试契约：QQ 俄罗斯轮盘命令接缝

> 本文件固定 TSK-278 最窄可观察公共接缝。测试只通过这些接缝观察行为，不锁私有
> 实现。生产实现由 TSK-278 本体负责，但必须满足本文件定义的可观察行为；测试文件
> `test_tsk278_*` 是这些行为的 RED 基线（当前全部因缺失行为而失败）。
>
> 最终依据：TSK-278 描述、TSK-267 Resolution（评论 6a946fe7ce48fb284d28beb8）、
> TSK-266 最终决议索引（评论 6a9ec4831af305fd1c073eac）与人工覆盖评论（1H
> `6a9bc5dd`、10.2 排行榜 `6a9c5988`、12.2 Markdown `6a9e5588`、真实提及验收
> `6a9ece96`、编号展示 `6a9bf4b7`）。排行榜属于本票范围：`CanonicalCommand` 已含
> `leaderboard` intent（TSK-276 顶层导出），本票实现其 QQ 解析与渲染。

## 1. 范围与数据流

```
QQ 群消息 (GROUP_AT_MESSAGE_CREATE)
  → RouletteQQHandler.handle(bot, event, state=...)  # 严格事件资格 + 准入 token + 解析 + 编排
      → parse_command(text)                  # 纯函数：文本 → CanonicalCommand | None
      → service.observe_current(group)       # 仅 active 状态变更动作，预读观察
      → service.execute_group_command(...)   # 一次领域/收据执行（TSK-276 深命令接口）
      → delivery.deliver(receipt, sender)    # 构建冻结载荷 → 原子领取 → 最多一次 QQ 发送（TSK-267）
  → render_reply(context)                    # 冻结投影 → 完整 Markdown 正文 + metadata（含 keyboard spec）
  → build_keyboard / keyboard_from_spec       # 冻结 keyboard spec → 真实 QQ MessageKeyboard
```

- 收据/履约/领域模型复用 TSK-276（`RouletteCommandService` / `CommandReceipt` /
  `ReplyProjection` / `ReplyProjectionContext` / `FulfillmentClaim` /
  `FulfillmentState` / `CanonicalCommand` / `CommandRequest` / `Observation`），本票
  **不新增收据/履约 Schema**。
- 发送保证复用 TSK-267：`msg_id = 原始 inbound_msg_id`、`msg_seq = 1`、无
  `message_reference`、最多一次发送、发送开始后的超时/崩溃/取消判 UNKNOWN 不重发。
- 发送前实时核查轮盘开关与群准入（TSK-278 评论 6a9ed97d）：关闭或受限不启动新发送、
  不重渲染已提交投影、不补发。
- 帮助复用 TSK-266 10.1：`komari_help` 扫描 `PluginMetadata.usage`；**无 `/轮盘 帮助`**，
  输入 `/轮盘 帮助` 按未知命令处理。排行榜是合法只读轮盘命令，不适用 help 规则。

## 2. 接缝模块布局（不扩充顶层 `__all__`）

TSK-278 的 QQ 适配层放在插件子包 `komari_bot.plugins.komari_roulette.qq`，测试从
下列子模块导入。本票七个新符号**没有跨插件消费者**，因此**不要求**在插件顶层
`__all__` 重导出；顶层 `__init__.py` 只须 import 子包以注册 handler 与
`__plugin_meta__`（TSK-276 已导出符号保持不变）。实现可调整子模块文件名，但导入路径
语义等同。

```python
from komari_bot.plugins.komari_roulette.qq.parser   import parse_command
from komari_bot.plugins.komari_roulette.qq.renderer import render_reply
from komari_bot.plugins.komari_roulette.qq.keyboard import (
    build_keyboard,
    keyboard_from_spec,
)
from komari_bot.plugins.komari_roulette.qq.delivery import (
    DeliveryOutcome,
    RouletteDelivery,
    SendNotAcceptedError,
)
from komari_bot.plugins.komari_roulette.qq.handler  import RouletteQQHandler
```

`ReplyProjection` 的 `metadata` 是标量映射（TSK-276），因此冻结 keyboard 以 JSON 字符串
`metadata["keyboard"]` 承载（按钮行/标签/填入命令的**冻结快照**），交付时由
`keyboard_from_spec` 还原为真实 QQ `MessageKeyboard`。`render_reply` 是回复投影器，
`RouletteCommandService` 在命令事务内调用它并把 body+metadata 冻结进收据；发送模块只
消费已提交收据（TSK-267 11）。

## 3. parse_command 契约

纯函数，只读 `text`。解析失败**不抛异常**、不写日志、不访问服务。

- 入口识别：`text` 先 trim 空白、折叠连续空白为单空格；以 `/轮盘` 开头（可选前导空白）
  才算轮盘命令，否则返回 `None`（非轮盘入口，handler 静默返回）。
- 子命令表（大小写敏感的中文子命令；道具字母不区分大小写）：
  - 无子命令 → `syntax_failure`
  - `开局`→create、`加入`→join、`退出`→leave、`取消`→cancel、`开始`→start、
    `开枪`→shoot、`弃权`→forfeit、`结束`→end_turn、`装填`→reload、
    `道具`→open_item_panel、**`排行榜`→leaderboard（只读，本票范围）**
  - `道具 使用 <字母>`/`道具 丢弃 <字母>` → use_item/discard_item
  - `奖励 丢弃` → choose_item(discard)；`奖励 替换 <名称>` → choose_item(replace, 名称)
  - `转让 <正整数>` → transfer
  - 其余子命令（含 `帮助`）→ **`unknown_command`**
- 道具字母归一：`a/A/b/B/c/C/d/D` → `MAGNIFIER/BEER/BURST/LOCK`；`D<num>`/`d<num>` →
  `use_item(LOCK, num)`。
- 玩家编号语法：`[1-9][0-9]*`，**禁止前导零**、禁止 `0`、非数字、负号；编号必须与字母
  紧邻成同一 token（`D2`/`d2` 合法，`D 2` 视为参数多余 → `invalid_args`）。
- `排行榜` 不带多余参数；`/轮盘 排行榜 x` → `invalid_args`。

### 语法失败码（syntax_code）

固定标识符；`invalid_args` 携带预定义正确用法后缀（**唯一通道是 syntax_code**，因为
TSK-276 `_project_reply` 对 syntax_failure 只转发 `result_code=code`、`details={}`）：

| 码 | 场景 | 说明 |
|----|------|------|
| `unknown_command` | 未知子命令（含 `/轮盘 帮助`） | 固定文案指向 `.docs 轮盘` |
| `invalid_item_letter` | 使用/丢弃字母非 A-D | 固定文案列出字母 |
| `invalid_reward_name` | 奖励替换名称非四道具名 | 固定文案列出名称 |
| `invalid_player_seq` | 编号格式错误（前导零/0/非数字/负数） | 固定文案 |
| `invalid_args[: 用法]` | 参数缺失/多余/格式错误 | 用法为预定义静态模板，**永不回显原始输入** |

`invalid_args` 用法后缀示例：`invalid_args:道具 使用 A｜B｜C｜D<玩家编号>`、
`invalid_args:转让 <玩家编号>`、`invalid_args:奖励 丢弃｜替换 <道具名>`、
`invalid_args:排行榜`（排行榜不接受参数）。

## 4. render_reply 契约

纯函数：`ReplyProjectionContext`（冻结投影）→ `ReplyProjection(body, metadata)`。不读
服务、不读随机。**所有用户可见文案为 TSK-266 定稿，测试按精确文本断言；生产不得自行
改词、截断或调整排版。**

- **完整 Markdown 结构**（TSK-266 12.2 定稿）：
  - 普通动作结果用块引用 `>` 一行；当前玩家、关键弹仓数字、当前新道具、待处理奖励数用
    `**粗体**`；玩家阵容用无序列表 `- `；段落换行分隔。
  - 继续行动/成功上锁：`> 结果句 — **当前：{冻结名}** — 弹仓信息 — *** — 玩家列表`
    （`***` 前后空行；提及 tag 紧跟当前玩家行内的冻结名之后）。
  - 奖励选择：`> 结果句 — 道具列表已满，选择一项来替换。 — 当前新道具 — 已有道具 —
    后续待处理奖励 — *** — 当前局面 — *** — 玩家列表`（两条 `***`）。
  - 终局：**单个普通段落**，无 `>`、无 `**`、无 `***`、无列表、无按钮；必须表达
    **导致终局的事件**（由 `details` 的 `completion_reason`/`eliminated_reason` 等
    事实驱动，如谁被淘汰/弃权/超时）、唯一胜者与本群累计胜场（`winner_group_wins`）。
    终局文案遵循 TSK-266 1F/269 文案池；`{冻结名} {<qqbot-at-user>} 获胜，累计胜场
    {n}。` 只是 md-mention 实机验收样例，**不是无事件精确定稿**，测试不得钉死单句。
- **单次真实提及与位置**：
  - `context.mention_target` 非空时，`body` 恰好嵌入一个
    `<qqbot-at-user id="{mention_target.member_openid}" />`（QQ 官方 Markdown 原生提及，
    原型 `codex/tsk-266-md-mention-acceptance` 实机验收格式）。位置按
    `context.mention_reason`：
    - `turn` / `reward`：`**当前：{冻结名}** {tag}`（紧跟当前玩家行内的冻结名之后）；
    - `winner`：提及紧跟胜者冻结名之后（样例 `{冻结名} {tag} 获胜，累计胜场 {n}。`
      只示意提及位置；终局正文还须表达终局事件，见上）；
    - `lock_target`：`{冻结名}（{tag}）`（上锁结果句内、冻结名后的括号中）。
  - `metadata` 同时携带 `mention_member_openid` 与 `mention_display_name` 一对（供日志/
    校验），但**真实发送载荷的提及断言必须以 `MessageSegment.markdown` 正文中的
    `<qqbot-at-user>` 位置为准**，metadata 对不是充分证据。
  - `mention_target` 为 None（原地继续、无提醒目标或错误）→ body 无 tag、metadata 无
    mention 对。普通正文除平台提及 tag 的 `id` 外不内联 openid。
- **名单转义、无编号泄漏**：
  - 冻结显示名以**纯文本**插入正文；必须先转义/清洗，禁止引入 Markdown 链接、粗体、
    代码、块引用、列表或 XML 标签（`[` `]` `*` `` ` `` 及 `<` `>` `&`），也禁止
    伪造第二个 `<qqbot-at-user>`。转义策略不限（反斜杠/全角/实体均可），测试按**结果**
    断言：原始注入结构（`](`、`**加粗**`、`` `code` ``、第二个 tag、`<b>`）不得出现在
    正文，名字内容仍可见。注入测试构造 `<qqbot-at-user id="x" />`、`<b>`、`&amp;`、
    `[恶意](url)`、`**粗**` 等名字。
  - 普通正文（轮转/当前玩家/动作结果/玩家列表/奖励/终局/标题）**不出现玩家编号**。
    编号式玩家标识按**行锚定**判定：`(?m)^- {N}｜`（`- 2｜小红` 形式）；弹仓计数
    （`弹仓 **4/6**`）、`道具 N` 数量、排行榜名次行（`1. 玩家｜N 胜`）都不是玩家编号。
    **玩家阵容行格式**：`- {冻结名}｜{状态}｜道具 {N}｜待锁`（当前玩家加粗，状态
    `当前`/`存活`/`出局`）；**`道具 N` 是道具总数**（TSK-266 1B：每名玩家持有的道具
    总数，各类型数量求和）：啤酒×2＋锁×1 显示 `道具 3`；待锁标记仅在目标玩家
    `pending_lock` 时出现。
  - 编号只出现在两个专用区域：道具面板"可上锁的玩家"（`- 2｜小红`）与等候"可转让给"
    （`- 2｜小红`）；按稳定编号升序、保留缺口、不重排、不 @。
- **排行榜渲染**（TSK-266 10.2 定稿）：
  - `context.result_code == "leaderboard"` 且 `details["leaderboard"]` 为空元组 →
    `本群还没有俄罗斯轮盘胜者。`（普通段落，无按钮、无 mention）。
  - 有记录时：`**本群俄罗斯轮盘排行榜｜前 10 名**` ＋ 有序列表
    `1. {冻结名}｜{N} 胜`，**最多显示前 10 名**（不足 10 显示全部）；末行
    `共有 {M} 名玩家取得过胜利。`，`M` 统计本群**所有**至少一次胜场的玩家（含未进
    前 10 者），不是仅显示行数。`details["leaderboard"]` 条目**恒为**
    `"{冻结名}:{N}"`（TSK-276 `_leaderboard_values` 实际编码：1 胜也是 `":1"`，
    不存在裸名字条目）。冻结名本身可含冒号：渲染必须按**最后一个冒号**拆分
    （`rsplit(":", 1)`），绝不能把玩家名当胜场数或拆错名字（`"小红:小明:5"` →
    冻结名 `小红:小明`、胜场 5）。
  - 行首数字是排行榜名次，不是玩家稳定编号；名字是最近一次获胜对局保存的**冻结显示名**
    （改名不刷新）；不 mention、不展示 openid/QQ 号。
  - 排行榜无按钮；不延长行动时间（到期推进由 TSK-276 服务完成）。
  - **超时原玩家抑制榜单**（TSK-266 10.2）：若排行榜查询惰性推进到期且查询者正是被该
    请求推进超时的原当前玩家，本票实现必须返回超时结果（`turn_expired` 及轮转/终局
    消息），**不追加排行榜**。该场景由真实 PG 测试经真实 `RouletteCommandService`
    断言收据 `result_code` 不是 `leaderboard`。
- **固定错误**：`result_code` → 固定文案（TSK-266 11.1–11.5 定稿），**不附加局面、按钮
  或出站 mention**，不泄漏弹仓顺序、随机数据、异常详情、内部身份。`chamber_empty`
  （`item_precondition_failed` + `chamber_empty`）无定稿文案，渲染层保持通用错误格式且
  不得泄露弹仓状态。

### 11.x 固定错误文案表（render 断言依据；定稿，逐字断言）

| result_code | details | 文案 |
|---|---|---|
| `unknown_command` | - | 无法识别这条轮盘命令。发送 .docs 轮盘 查看使用说明。 |
| `invalid_args:<用法>` | - | 命令参数不正确。正确用法：@Bot /轮盘 <用法> |
| `invalid_player_seq` | - | 玩家编号格式不正确。请填写不带前导零的正整数。 |
| `player_seq_not_found` | - | 当前游戏中不存在这个玩家编号，请检查目标编号后重新操作。 |
| `invalid_item_letter` | - | 道具字母只能是：A＝放大镜、B＝啤酒、C＝连发器、D＝锁。 |
| `invalid_reward_name` | - | 替换的道具名称只能是：放大镜、啤酒、连发器或锁。 |
| `game_already_exists` | - | 本群已有一局俄罗斯轮盘，暂时不能创建新局。 |
| `no_waiting_game` | - | 本群没有等待开始的俄罗斯轮盘。 |
| `already_joined` | - | 你已经加入当前游戏。 |
| `game_full` | - | 当前游戏已满员（6/6）。 |
| `game_already_started` | - | 游戏已经开始，无法再改变等候阵容。 |
| `not_joined` | - | 你尚未加入当前等候局。 |
| `not_host` | - | 只有当前局主可以执行这个操作。 |
| `not_enough_players` | - | 至少需要 2 名玩家才能开始游戏。 |
| `invalid_transfer_target` | `self` | 不能把局主转让给自己。 |
| `waiting_game_expired` | - | 这局游戏等待太久仍未开始，现已自动结束。 |
| `no_active_game` | - | 本群没有进行中的俄罗斯轮盘。 |
| `game_completed` | - | 这局俄罗斯轮盘已经结束。 |
| `not_participant` | - | 你不是当前游戏的参与者。 |
| `player_eliminated` | - | 你已经出局，不能再操作这局游戏。 |
| `not_current_player` | - | 现在不是你的回合。 |
| `turn_expired` | - | 你的行动时间已经结束，本次命令未执行。 |
| `state_conflict` | - | 局面刚刚发生变化，本次操作未执行。请根据机器人最新回复重新操作。 |
| `action_not_allowed_in_phase` | - | 当前阶段不能执行这个操作。 |
| `action_not_allowed_in_phase` | `item_choice_pending` | 请先处理当前新道具；现在只能丢弃奖励、替换道具或弃权。 |
| `locked_turn_restriction` | - | 你本回合受到锁限制，只能执行一次开枪命令或弃权。 |
| `invalid_game_state` | - | 游戏状态异常，本次操作未执行。请联系管理员。 |
| `chamber_not_ready` | - | 弹仓尚未就绪，本次操作未执行。 |
| `chamber_full` | - | 弹仓已满，不能装填。 |
| `invalid_chamber_state` | - | 弹仓状态异常，本次操作未执行。 |
| `random_source_failed` | - | 随机结果生成失败，本次操作未执行。请重新发送命令。 |
| `item_not_owned` | - | 你没有这件道具。 |
| `invalid_item_target` | `self` | 锁不能对自己使用。 |
| `invalid_item_target` | `eliminated` | 目标玩家已经出局。 |
| `item_effect_conflict` | `burst_already_pending` | 手枪已经带有连发效果，不能重复使用连发器。 |
| `item_effect_conflict` | `target_already_locked` | 目标已经有一把待生效的锁。 |
| `item_precondition_failed` | `insufficient_chamber_for_burst` | 弹仓至少需要剩余 2 发才能使用连发器。 |
| `item_precondition_failed` | `beer_blocked_by_burst` | 手枪处于待连发状态，不能使用啤酒。 |
| `no_pending_item_choice` | - | 当前没有需要处理的道具奖励。 |

## 5. keyboard 契约

- `build_keyboard(context: ReplyProjectionContext) -> str`：纯函数，把冻结投影布局为
  JSON spec 字符串（`{"rows": [[{"label","data","action_type","permission_type",
  "reply","enter"}, ...], ...]}`）。布局决策（按阶段/局面出哪些按钮）在
  `build_keyboard` 内；`render_reply` 调用它并把结果放入 `metadata["keyboard"]`。
- `keyboard_from_spec(spec: str) -> MessageKeyboard`：纯函数，spec → 真实 QQ
  `MessageKeyboard`（`InlineKeyboard`/`InlineKeyboardRow`/`Button`，
  `Action(type=2, permission=Permission(type=2), data=..., reply=False, enter=False)`）。
- 按钮规则（TSK-266 1H `6a9bc5dd` 定稿）：
  - 每行最多 3 个按钮；标签统一带 emoji；通常每个按钮最多 3 个汉字；一行只有 2 个按钮
    时可放宽至 5 个汉字。**奖励替换标签不得为 6 字标签**：`🔄替换放大镜`（emoji＋5 汉字
    ＝6 字）违反规则，必须用 `🔄放大镜`（emoji＋3 汉字）。
  - 普通 follow_up（按权威局面动态）：`🧰使用 / 🗑️丢弃`、`🔫开枪 / 🔄装填`、
    `⏹️结束 / 🏳️弃权`；`🧰使用` 填入 `/轮盘 道具 使用`、`🗑️丢弃` 填入
    `/轮盘 道具 丢弃`（1H 定稿：由用户补完道具字母后手动发送，**无尾随空格**；1D 奖励
    替换与 6a9e6ce3 转让按钮仍按各自定稿保留参数分隔空格）。
  - 锁定回合：`🔫开枪 / 🏳️弃权`。
  - 奖励选择（1D）：`🗑️丢弃新道具 / 🏳️弃权` ＋ 各实际持有类型 `🔄<道具名>` 行
    （`🔄放大镜` / `🔄啤酒` / `🔄连发器` / `🔄锁`，填入 `/轮盘 奖励 替换 <名称>`，按
    A→B→C→D 顺序）。
  - 等候：`加入 / 开始 / 退出 / 取消` ＋ 通用 `🔄转让`（填入 `/轮盘 转让 `，末尾保留
    参数分隔空格）；仅局主一人时省略转让按钮。
  - 终局、排行榜与固定错误：**无按钮**（`metadata["keyboard"]` 为规范空对象
    `'{"rows": []}'`）。规范 spec 恒为 JSON 对象 `{"rows": [...]}`；本票无历史数据/
    兼容要求，`keyboard_from_spec`/`build_real_keyboard` **不接受裸 `[]` 形式**（无
    历史宽容回退），`render_reply` 输出恒为对象形式。
  - 按钮标签不截断昵称、不把冻结姓名/编号塞进短标签。
- 覆盖评论按 `6a9e6ce3`（等候转让）、`6a9bc5dd`（1H 尺寸）、`6a9c5988`（排行榜）定稿。

## 6. RouletteDelivery 契约

`RouletteDelivery(service, *, runtime_check: Callable[[], bool | Awaitable[bool]] | None = None)`；
`async deliver(receipt: CommandReceipt, sender) -> DeliveryOutcome`。`DeliveryOutcome`
是真实枚举 `DELIVERED | NOT_DELIVERED | UNKNOWN | NO_CLAIM`；`SendNotAcceptedError`
是 sender 明确"发送前/未被接受"失败的型别异常。**顺序固定（TSK-267 6）**：

1. **先从已提交收据构建冻结载荷**：`MessageSegment.markdown(receipt.reply.body)` ＋
   `MessageSegment.keyboard(keyboard_from_spec(receipt.reply.metadata["keyboard"]))`；
   不重读服务状态、不 observe、不重渲染。构建失败 → 按第 3 步语义转 NOT_DELIVERED
   （0 网络）。
2. `service.claim_fulfillment(receipt.receipt_id)`：返回 `None` → `NO_CLAIM`，0 网络
   （重复事件/已领取）；抛异常 → 原样上抛，0 网络。
3. **发送前 runtime 最后重核**：`await runtime_check()`（轮盘开关、群准入、5 分钟凭证
   过期等实时核查；同步返回 bool 或返回 Awaitable[bool] 均可，真实准入重核是异步
   可调用，不得强制仅同步）；结果 False → `service.mark_not_delivered(claim)` →
   `NOT_DELIVERED`，
   0 网络。**276 没有 `mark_not_started_failed` 方法（已核查实际接口）；预发送失败经真实
   `claim_fulfillment`（NOT_STARTED→PENDING）＋ `mark_not_delivered`
   （PENDING→NOT_DELIVERED）完成，0 网络调用平台。**
4. `sender.send_to_group(group_openid, message, msg_id=receipt.inbound_msg_id,
   msg_seq=1)`；**无 `message_reference` / `msg_ref_id`**。
5. sender 抛 `SendNotAcceptedError` → `mark_not_delivered` → `NOT_DELIVERED`。
6. sender 抛其他异常（超时/断连/崩溃）→ **不 mark、不重试**，返回 `UNKNOWN`（保持
   `PENDING_CONFIRMATION`）；`asyncio.CancelledError` 同样不 mark、不重试、原样重抛。
7. 发送成功 → `service.mark_delivered(claim, platform_message_id=...)`；mark 抛异常
   （发送已发生）→ **不重试**，返回 `UNKNOWN`。
8. mark 成功 → `DELIVERED`。

测试断言顺序：build 先于 claim，claim 先于 runtime_check，runtime_check 先于 send，
send 先于 mark_delivered（无服务模式下用记录型 builder/runtime_check/service 记录顺序；
真实 PG 模式用 sender 在 `send_to_group` 时快照履约行状态证明 claim 已先于 send 生效）。

## 7. RouletteQQHandler 契约

`RouletteQQHandler(service, delivery, *, send_gate: Callable[[], bool | Awaitable[bool]] | None = None)`；
`async handle(bot, event, *, state=None) -> None`：

1. 严格事件资格：仅 `GroupAtMessageCreateEvent`；`event.id`、`event.group_openid`、
   `event.author.member_openid` 非空，否则静默返回。
2. **准入 token（真实 helper）**：`get_qq_admission_token(state)`（group_admission 顶层
   现有导出）；`None` → 静默返回（0 execute/0 deliver）。
3. `event.get_message().extract_plain_text()` → `parse_command`；`None` → 静默返回。
4. active 状态变更动作（shoot/reload/end_turn/use_item/discard_item/choose_item/
   forfeit/transfer 之领域相关者）先 `observe_current(group)`，observation 传入
   `execute_group_command`；等候动作（create/join/leave/cancel/start/open_item_panel/
   leaderboard）不 observe。
5. 恰好一次 `execute_group_command(request, observation=...)`。
6. **发送门（TSK-278 评论 6a9ed97d）**：`await send_gate()`（同步或异步）为 False
   （轮盘开关关闭/群准入受限）
   → **不启动新发送**：不调用 `deliver`、不重渲染、不补发；收据保持 NOT_STARTED 由后台
   过期收敛。为 True 或未注入时才恰好一次 `deliver(receipt, sender)`。
7. `request = CommandRequest(app_id=bot.self_id, group_openid, inbound_msg_id=event.id,
   member_openid=event.author.member_openid, command, target_mention_count=len(event.mentions or []))`。
8. handler 自身**不执行 SQL、不写领域、不碰随机**；全部通过注入的 service/delivery 完成。
   错误由上层捕获/记录，handler 不自行重试领域动作或 QQ 发送。

## 8. 测试约定

- 测试文件：`tests/komari_roulette/test_tsk278_*.py`；独立 helpers 在 `tsk278_support.py`。
- 夹具名固定：小明（member-1）/ 小红（member-2）/ 小白（member-3）；群 `group-1`；
  app `tsk278-app`。
- 无服务模式：QQ 事件用 `model_construct` 构造，bot 用最小子类，服务/发送/履约用记录型
  fake；真实 PG 用例按既有 `KOMARI_TEST_POSTGRES_URL` 门控编写（未配置时跳过）。
- `test_tsk278_fixture_probe.py` 是**独立夹具探针**：PG 门控、**不 import 任何 TSK-278
  符号**，只验证本票测试用到的真实 TSK-276 PG API/SQL/helper（`create_engine_and_factory`
  / `seed_binding` / `create_waiting` / `CountingProjector` / 收据与履约表结构 / claim 与
  mark 行状态 / 排行榜冻结名 / 真实上下文 mention 优先级）当前健康。这样"缺失行为"的
  ImportError 只证明 TSK-278 接缝缺失，不能把 fixture/276 回归误判为整体基线失败。
- RED 判定：子包 import 缺失（`komari_bot.plugins.komari_roulette.qq` 等）即"缺失行为"，
  不是夹具/依赖错误。

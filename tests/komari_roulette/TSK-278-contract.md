# TSK-278 测试基线：QQ 俄罗斯轮盘命令接缝契约

> 本文件是测试基线的**契约**（seam contract），只读可测试的公开接缝，不锁私有实现。
> 生产实现由 TSK-278 本体负责，但必须满足本文件定义的可观察行为；测试文件 `test_tsk278_*`
> 是这些行为的 RED 基线（当前全部因缺失行为而失败）。

## 1. 范围与数据流

```
QQ 群消息 (GROUP_AT_MESSAGE_CREATE)
  → RouletteQQHandler.handle(bot, event)     # 严格事件资格 + 解析 + 编排
      → parse_command(text)                  # 纯函数：文本 → CanonicalCommand | None
      → service.observe_current(group)       # 仅 active 状态变更动作，预读观察
      → service.execute_group_command(...)   # 一次领域/收据执行
      → delivery.deliver(receipt, sender)    # 最多一次 QQ 发送（TSK-267）
  → render_reply(context)                    # 冻结投影 → 完整 Markdown 正文
  → build_keyboard(context)                  # 冻结投影 → 独立键盘（独立字段）
```

- 收据/履约/领域模型复用 TSK-276（`RouletteCommandService` / `CommandReceipt` /
  `ReplyProjection` / `ReplyProjectionContext` / `FulfillmentClaim`），本票**不新增收据 Schema**。
- 发送保证复用 TSK-267：`msg_id = 原始 inbound_msg_id`、`msg_seq = 1`、无 `message_reference`、
  最多一次发送、超时/崩溃判 UNKNOWN 不重发。
- 帮助复用 TSK-266 10.1：`komari_help` 扫描 `PluginMetadata.usage`；**无 `/轮盘 帮助`**。

## 2. 顶层导出（测试导入的公开接缝）

`komari_bot.plugins.komari_roulette.__all__` 必须新增：

| 符号 | 说明 |
|------|------|
| `parse_command(text: str) -> CanonicalCommand \| None` | 文本解析，纯函数，无副作用 |
| `render_reply(context: ReplyProjectionContext) -> ReplyProjection` | Markdown 渲染，纯函数 |
| `build_keyboard(context: ReplyProjectionContext) -> MessageKeyboard` | 键盘渲染，纯函数 |
| `RouletteDelivery` | `deliver(receipt, sender) -> DeliveryOutcome` 履约编排 |
| `DeliveryOutcome` | `DELIVERED \| NOT_DELIVERED \| UNKNOWN \| NO_CLAIM` |
| `SendNotAcceptedError` | sender 明确"发送前/未被接受"失败的型别异常 |
| `RouletteQQHandler` | `async handle(bot, event) -> None`，群消息入口 |

实现可位于子模块，但必须从顶层包 `__all__` 暴露（TSK-276 惯例，测试统一从顶层导入）。

## 3. parse_command 契约

纯函数，只读 `text`。解析失败**不抛异常**、不写日志、不访问服务。

- 入口识别：`text` 先 trim 空白、折叠连续空白为单空格；以 `/轮盘` 开头（可选前导空白）才算轮盘
  命令，否则返回 `None`（非轮盘入口，handler 静默返回）。
- 子命令表（大小写敏感的中文子命令；道具字母不区分大小写）：
  - 无子命令 → `syntax_failure`
  - `开局`→create、`加入`→join、`退出`→leave、`取消`→cancel、`开始`→start、`开枪`→shoot、
    `弃权`→forfeit、`结束`→end_turn、`装填`→reload、`道具`→open_item_panel
  - `道具 使用 <字母>`/`道具 丢弃 <字母>` → use_item/discard_item
  - `奖励 丢弃` → choose_item(discard)；`奖励 替换 <名称>` → choose_item(replace, 名称)
  - `转让 <正整数>` → transfer
  - 其余子命令（含 `帮助`、`排行榜`）→ **`unknown_command`**（排行榜不在本票范围，帮助已删除）
- 道具字母归一：`a/A/b/B/c/C/d/D` → `MAGNIFIER/BEER/BURST/LOCK`；`D<num>`/`d<num>` → `use_item(LOCK, num)`。
- 玩家编号语法：`[1-9][0-9]*`，**禁止前导零**、禁止 `0`、非数字、负号；编号必须与字母紧邻成同一 token
  （`D2`/`d2` 合法，`D 2` 视为参数多余 → `invalid_args`）。

### 语法失败码（syntax_code）

固定标识符；`invalid_args` 携带预定义正确用法后缀（**唯一通道是 syntax_code**，因为 TSK-276
`_project_reply` 对 syntax_failure 只转发 `result_code=code`、`details={}`）：

| 码 | 场景 | 说明 |
|----|------|------|
| `unknown_command` | 未知子命令（含 `/轮盘 帮助`） | 固定文案指向 `.docs 轮盘` |
| `invalid_item_letter` | 使用/丢弃字母非 A-D | 固定文案列出字母 |
| `invalid_reward_name` | 奖励替换名称非四道具名 | 固定文案列出名称 |
| `invalid_player_seq` | 编号格式错误（前导零/0/非数字/负数） | 固定文案 |
| `invalid_args[: 用法]` | 参数缺失/多余/格式错误 | 用法为预定义静态模板，**永不回显原始输入** |

`invalid_args` 用法后缀示例：`invalid_args:道具 使用 A｜B｜C｜D<玩家编号>`、
`invalid_args:转让 <玩家编号>`、`invalid_args:奖励 丢弃｜替换 <道具名>`。

## 4. render_reply 契约

纯函数：`ReplyProjectionContext`（冻结投影）→ `ReplyProjection(body, metadata)`。不读服务、不读随机。

- **完整 Markdown 结构**（TSK-266 12.2 定稿）：
  - 普通动作结果用块引用 `>` 一行；当前玩家、关键弹仓数字、当前新道具、待处理奖励数用 `**粗体**`；
    玩家阵容用无序列表 `- `；段落换行分隔。
  - 继续行动/成功上锁：`弹仓信息 — *** — 玩家列表`（`***` 前后空行）。
  - 奖励选择：`后续待处理奖励 — *** — 当前局面 — *** — 玩家列表`（两条 `***`）。
  - 终局：**单个普通段落**，无 `>`、无 `**`、无 `***`、无列表、无按钮；只表达唯一胜者和结算后
    本群累计胜场。
- **名单转义、无编号泄漏**：
  - 普通正文（轮转/当前玩家/动作结果/玩家列表/奖励/终局/标题）**不出现玩家编号**。
  - 编号只出现在两个专用区域：道具面板"可上锁的玩家"（`- 2｜小红`）与等候"可转让给"（`- 2｜小红`）；
    按稳定编号升序、保留缺口、不重排、不 @。
  - 玩家显示名作为**纯文本**插入，不得引入 Markdown 链接、粗体、@ 或列表注入。
- **mention 至多一次**：`metadata` 恰有 `mention_member_openid` 与 `mention_display_name` 一对
  （终局 @胜者、上锁 @锁目标、回合轮转/奖励选择 @目标）或没有；`body` 永不内联 `member_openid`。
- **固定错误**：`result_code` → 固定文案（TSK-266 11.1–11.5），**不附加局面、按钮或出站 mention**，
  不泄漏弹仓顺序、随机数据、异常详情、内部身份。

### 11.x 固定错误文案表（render 断言依据）

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

> 表内为渲染层要求的**可见文案**。实现可调整细节措辞，但测试按本表断言；若生产有意变更文案，
> 应同步更新本契约与测试。`chamber_empty`（`item_precondition_failed` + `chamber_empty`）不在
> TSK-266 11.x 定稿表中，无固定可见文案，渲染层必须保持错误通用格式且不得泄露弹仓状态。

## 5. build_keyboard 契约

纯函数：冻结投影 → `MessageKeyboard`（QQ `InlineKeyboard`，`action.type=2`、`permission.type=2`、
`reply=False`、`enter=False`）。按钮只"填入"命令文本、不自动发送；**每行最多 3 个**。

- 普通 follow_up（按权威局面动态）：`🧰使用 / 🗑️丢弃`、`🔫开枪 / 🔄装填`、`⏹️结束 / 🏳️弃权`；
  `🧰使用` 填入 `/轮盘 道具 使用 `、`🗑️丢弃` 填入 `/轮盘 道具 丢弃 `（末尾**保留参数空格**）；
  其余无尾随空格。
- 锁定回合：`🔫开枪 / 🏳️弃权`。
- 奖励选择：`🗑️丢弃新道具 / 🏳️弃权` ＋ 各持有类型 `🔄替换<道具名>` 行。
- 等候：`加入 / 开始 / 退出 / 取消` ＋ `🔄转让`（填入 `/轮盘 转让 `，末尾保留空格）。
- 终局与固定错误：**无按钮**（空键盘）。
- 按钮标签不截断昵称、不把冻结姓名/编号塞进短标签。

## 6. RouletteDelivery 契约

`RouletteDelivery` 构造时注入履约服务（`claim_fulfillment` / `mark_delivered` / `mark_not_delivered`）。
`async deliver(receipt: CommandReceipt, sender: object) -> DeliveryOutcome`：

1. `claim_fulfillment(receipt.receipt_id)`；返回 `None` → **`NO_CLAIM`，0 次网络调用**（重复事件/幂等）。
2. claim 抛异常（回执保存失败）→ 原样上抛，**0 次网络调用**。
3. 从**冻结** `receipt.reply` 构建载荷（不重读服务状态、不 observe）；载荷含 Markdown 正文、
   `metadata` 中的至多一个 mention、`build_keyboard` 键盘。构建失败 → `mark_not_delivered` → `NOT_DELIVERED`，
   **0 次网络调用**。
4. `sender.send_to_group(group_openid, message, msg_id=receipt.inbound_msg_id, msg_seq=1)`；
   **无 `message_reference` / `msg_ref_id`**。
5. sender 抛 `SendNotAcceptedError`（明确发送前/未被接受）→ `mark_not_delivered` → `NOT_DELIVERED`。
6. sender 抛其他异常（超时/断连/崩溃）→ **不 mark、不重试**，返回 `UNKNOWN`（保持 `PENDING_CONFIRMATION`）。
   `asyncio.CancelledError` 同样不 mark、不重试、原样重抛。
7. 发送成功 → `mark_delivered`；若 mark 抛异常，发送已发生 → **不重试**，返回 `UNKNOWN`。
8. mark 成功 → `DELIVERED`。

## 7. RouletteQQHandler 契约

`RouletteQQHandler(service, delivery)`；`async handle(bot, event) -> None`：

1. 严格事件资格：仅 `GroupAtMessageCreateEvent`；`event.id`、`event.group_openid`、
   `event.author.member_openid` 非空，否则静默返回。
2. 取 `event.get_message().extract_plain_text()` → `parse_command`；`None` → 静默返回（非轮盘）。
3. active 状态变更动作（shoot/reload/end_turn/use_item/discard_item/choose_item/forfeit/transfer 之
   领域相关者）先 `observe_current(group)`，`observation` 传入 `execute_group_command`；
   等候动作（create/join/leave/cancel/start/open_item_panel）不 observe。
4. 恰好一次 `execute_group_command(request, observation=...)` 与一次 `deliver(receipt, sender)`。
5. `request = CommandRequest(app_id=bot.self_id, group_openid, inbound_msg_id=event.id,
   member_openid=event.author.member_openid, command, target_mention_count=len(event.mentions or []))`。
6. handler 自身**不执行 SQL、不写领域、不碰随机**；全部通过注入的 service/delivery 完成。
   错误由上层捕获/记录，handler 不自行重试领域动作或 QQ 发送。

## 8. 测试约定

- 测试文件：`tests/komari_roulette/test_tsk278_*.py`；独立 helpers 在 `tsk278_support.py`。
- 夹具名固定：小明（member-1）/ 小红（member-2）/ 小白（member-3）；群 `group-1`；app `tsk278-app`。
- 全部**无服务、无 NoneBot driver 初始化**：QQ 事件用 `model_construct` 构造，bot 用最小子类，
  服务/发送/履约用记录型 fake。真实 PG 用例按既有 `KOMARI_TEST_POSTGRES_URL` 门控编写（未配置时跳过）。
- RED 判定：顶层 import 缺失（`parse_command` 等）即"缺失行为"，不是夹具/依赖错误。

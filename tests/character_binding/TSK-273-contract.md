# TSK-273 测试契约

本票测试固定一个窄的、可替换取证接缝，供后续生产实现与测试夹具共同使用。
测试只观察取证结果和 OneBot matcher 的注册/分发，不调用正式角色绑定写入。

## 取证接缝

`komari_bot.plugins.character_binding.reply_evidence` 暴露：

- `ReplyEvidenceCollector(app_id, official_bot_qq, message_fetcher, clock)`；
- `ReplyEvidenceCollector.open_session(session_code, group_openid, member_openid, original_command, qq_message_id)`；
- `ReplyEvidenceCollector.cancel_session(session_code)`；
- `ReplyEvidenceCollector.reset_connection()`；
- `ReplyEvidenceCollector.handle_event(event)`，返回 `ReplyEvidence | None`；
- `ReplyEvidenceSession`、`ReplyEvidence`、`SessionCodeCollisionError`。

`message_fetcher` 是异步的 `message_id -> 完整 OneBot get_msg payload` 端口；`clock`
返回带时区的当前时间。`open_session` 的输入只来自 QQ 侧：应用、官方群 OpenID、
官方成员 OpenID、QQ 原始命令正文和 QQ 消息 ID；它不能预先要求 OneBot 数字群号或
数字 QQ。生产监听器必须经这个窄端口读取 `get_msg`，不能直接发送 OneBot 消息。
临时会话从 `open_session` 起有效 600 秒，恰好到期时拒绝；取消、新连接代次和新的
collector 实例都会使旧缓存/证据失效。

## 事件与证据边界

测试使用真实 OneBot V11 `GroupMessageEvent`、`MessageSegment.reply` 和 `Reply`。
流程方向固定为：OneBot 先缓存人类的原始命令；官方 Bot 再发送一条带会话码、原生
引用该命令的挑战；OneBot 收到挑战后沿 reply 读取原始命令。`event.message` 可以
已经移除 reply 段，`event.original_message` 保留原始 reply；挑战外层发送者必须是
配置的官方数字 QQ。挑战事件的 `to_me=False` 且 OneBot `self_id` 与官方 Bot 数字
QQ 不同，仍应按配置的 `official_bot_qq` 校验可信发送者。

成功证据至少包含 `app_id`、`session_code`、`group_id`、`member_qq`、
`group_openid`、`member_openid`、`original_command`、QQ 原始命令 ID、OneBot 原始命令
ID、官方挑战 OneBot 消息 ID 和连接代次。两端消息 ID 必须分别保留，不能把它们当成
同一个 ID。

只有以下条件全部满足才产生证据：会话码仍有效；挑战外层发送者是可信官方 QQ；引用
是正常原生 reply；完整 `get_msg` 返回 `message_type=group`、有完整 `group_id`、
同一群、被引用发送者是人类成员、同一正文；QQ 原始消息 ID 与会话一致；当前原始
消息缓存与完整 `get_msg` 一致。`get_msg.group_id` 缺失、读取失败、跨群、跨成员、
正文或消息类型不符均故障关闭。重复合法事件幂等，不重复取证或产生绑定。

## 监听与准入

实际导入 `komari_bot.plugins.character_binding`（不是测试中的 package shim）时，
`reply_evidence` 注册一个 `message` matcher，且 `block=False`。它在已获准的群
消息 matcher 阶段消费证据；受 `group_admission` 拦截的群事件不得进入监听器、不得
写入原消息缓存。监听器没有可见 OneBot 发送副作用。

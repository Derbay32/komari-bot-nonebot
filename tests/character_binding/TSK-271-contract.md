# TSK-271 测试契约

这些测试把以下窄公共边界作为实现与验收的共同语言。测试只调用 `CharacterBindingManager` 的公开行为；数据库建表、唯一约束和事务由 Alembic 与真实 PostgreSQL 提供。

## 绑定管理器

`komari_bot.plugins.character_binding.manager.CharacterBindingManager` 提供：

```python
await manager.bind_group_member(
    app_id="qq-app",
    group_id="100",
    group_openid="group-openid",
    member_qq="42",
    member_openid="member-openid",
    character_name="花火",
    bot_self_id="onebot-connection-a",  # 连接来源，不属于群身份键
)
manager.get_qq_character_name(
    app_id="qq-app",
    group_openid="group-openid",
    member_openid="member-openid",
    fallback_nickname="平台昵称",
)
await manager.clear_character_name(
    app_id="qq-app",
    group_openid="group-openid",
    member_openid="member-openid",
)
manager.list_group_bindings(app_id="qq-app", group_openid="group-openid")
```

一次成功的 `bind_group_member` 同时建立或复用群映射、成员身份关联和角色名。失败时三者都不改变。`get_qq_character_name` 只解析带应用/群/成员上下文的正式群角色名；缺失时允许调用方提供普通平台昵称回退，但不能从旧全局 `user_id` 值回退。OneBot 事件使用下方的 `get_character_name(group_id, user_id, fallback_nickname)` 窄桥接查询。`clear_character_name` 只清当前群角色名，保留群映射与成员身份关联。

冲突、非法名字和持久化错误应通过公开异常区分，异常类型至少实现 `BindingConflictError`、`CharacterNameValidationError` 与 `BindingPersistenceError`，具体子类可以更细。

## 身份与名字规则

- 群身份键是 `(app_id, group_id)` 与 `(app_id, group_openid)` 的双向映射；同一实际 QQ 群不因 `bot_self_id`/OneBot `self_id` 不同而拆分。
- 成员身份键是 `(app_id, group_openid, member_qq)` 与 `(app_id, group_openid, member_openid)` 的双向映射。
- 名字先去首尾空白并把允许的连续空白折叠；最终 Unicode 码点长度为 1–64；拒绝 `Cc`、`Cf`、`Zl`、`Zp`。
- 同群名字以 `NFKC + casefold` 比较键唯一，跨群可使用相同名字。
- 旧全局值只可由本人主动 `/bind` 迁移流程作为候选读取；聊天、总结、抽签、提案、调试列表和入席流程不得按裸 `user_id` 自动 fallback。

## 消费者边界

`sr`、聊天 prompt builder、群总结 planner、`komari_custom` 和 debug bind 的名字解析必须收到同一个群上下文，并通过 `character_binding` 顶层公开接口读取。消费者不得继续依赖旧的两参数裸 `get_character_name(user_id, fallback)` 或全局 `list_bindings()` 语义。

OneBot 事件本身通常只有 `group_id`/QQ 号，消费者不负责把它们拼成
`group_openid`/`member_openid`，也不读取管理器内部映射。顶层包须提供最小的
群作用域公开查询：

```python
name = character_binding.get_character_name(
    group_id=event.group_id,
    user_id=event.get_user_id(),
    fallback_nickname=nickname,
)
```

该查询内部负责应用适配器/官方身份桥接，所有消费者复用它，不能硬编码 QQ
应用 ID、根据 `self_id` 拆群，或伪造缺失的 OpenID。一个群成员在一个
`(app_id, group_openid)` 群内只有一份角色资料；QQ 与官方 `member_openid`
只是这份资料的双向身份入口。多应用别名无法唯一解析时必须 fail-closed，
不得随意取第一条；旧全局值也不能在该 seam 内自动回退。

QQ 官方事件已具备 OpenID 时使用独立查询，不把 OpenID 伪装为 OneBot QQ 号：

```python
name = character_binding.get_qq_character_name(
    app_id=app_id,
    group_openid=group_openid,
    member_openid=member_openid,
    fallback_nickname=nickname,
)
```

管理写入口可使用与查询同一群作用域的
`set_group_character_name(group_id, user_id, name)` /
`clear_group_character_name(group_id, user_id)`。列表有两个明确的调用上下文，
不能把参数语义隐式重载成一套全局接口：

- 管理器的 canonical QQ/官方入口是
  `CharacterBindingManager.list_group_bindings(app_id=..., group_openid=...)`，
  只接受已验证的应用与官方群身份；
- OneBot/debug 入口可以提供独立的
  `CharacterBindingManager.list_onebot_group_bindings(group_id=...)` 群号桥接
  （或同等顶层公开接口），由绑定层完成应用与官方群身份解析后再调用
  canonical 资料查询。它不能让消费者读取管理器内部映射，也不能接受裸
  `list_bindings()` 全局语义。

两者都返回当前群的完整 canonical 成员关系，但不改变“明细仅私聊
SUPERUSER、群内只发安全回执”的调试投影规则。对于没有已验证群身份的事件，
日常名字解析必须安全失败并使用明确的非绑定展示策略；`.debug bind` 的成功
路径不能从私聊事件凭裸用户号推断群。

## 迁移与模型


`orm_models` 可以在不初始化 NoneBot、数据库或插件注册的干净 Python 进程中导入；Alembic `upgrade head` 与 ORM `check` 在真实测试 PostgreSQL 上成功，且无运行时导入副作用。

## AC 与测试映射

| 验收点 | 测试 |
| --- | --- |
| 群号/group_openid 双向唯一、self_id 不拆群 | test_group_id_and_openid_are_bidirectionally_unique_and_ignore_self_id |
| 同群 QQ/member_openid 双向唯一、跨群可复用 | test_member_qq_and_openid_are_bidirectionally_unique_per_group |
| 应用隔离 | test_app_isolation_allows_same_group_and_member_aliases |
| 名字 trim/空白折叠、长度、控制类别 | test_manager.py 名字校验参数组 |
| NFKC+casefold 同群唯一、跨群同名 | test_same_group_name_uses_nfkc_casefold_and_cross_group_allows_same_name |
| 真实 PG 并发同名仅一胜 | test_same_group_name_race_has_one_conflict_and_one_committed_row |
| 交叉群映射并发不把成员挂到错误群 | test_cross_group_mapping_race_never_attaches_member_to_wrong_group |
| 群/成员/名字原子失败无孤立记录；失败 manager 不发布快照 | test_same_group_name_race_has_one_conflict_and_one_committed_row、test_conflicting_name_rolls_back_group_member_and_name_atomically、test_new_group_write_failure_rolls_back_and_keeps_snapshot_unchanged |
| 清当前群名但保留身份关联 | test_clear_name_retains_verified_group_and_member_identity |
| 旧全局值不作为默认群查询 | test_legacy_global_value_is_not_a_default_group_lookup |
| OneBot 消费者传群作用域；总结逐条解析历史名字 | test_consumer_context.py |
| 旧全局 API 不再暴露 | test_manager_does_not_expose_legacy_global_binding_apis |
| 模型导入无运行期副作用、upgrade/check | test_model_and_migrations.py |

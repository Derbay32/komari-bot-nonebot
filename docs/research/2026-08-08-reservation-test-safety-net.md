# Findings：预占生命周期与双确认路径的测试安全网

> Wayfinder 研究 ticket [R2 — 预占与确认路径的现有测试安全网清单](https://github.com/Derbay32/komari-bot-nonebot/issues/44) 的调查产出。
> 调查范围：`tests/komari_chat/` 与 `tests/komari_memory/` 全部测试文件，对照源码符号定位：`RedisManager.reserve/confirm/renew/release_proactive_reply`（`komari_bot/plugins/komari_memory/services/redis_manager.py:1067/1112/1147/1176`）、`ReplyCommitRepository`（`komari_bot/plugins/komari_chat/repositories/reply_commit_repository.py:81`，`PendingReplyCommit` 在 :32）、`MessageHandler.prepare_pending_reply/:1150 cancel_prepared_reply/:1221 _process_claimed_reply_commit/:1377 retry_pending_reply_commits/:1412 commit_delivered_reply/:1495 discard_pending_reply`、`_reply_commit_worker`（`komari_bot/plugins/komari_chat/__init__.py:84-97`，`_start_reply_commit_worker` :100-104，driver 钩子 :119-121）。

## 1. 测试清单

### 1a. Redis 预占 API（tests/komari_memory/test_redis_manager.py）

| 测试名 | 位置 | 覆盖对象 |
|---|---|---|
| `test_proactive_reservation_is_atomic_for_concurrent_requests` | :924-951 | `reserve_proactive_reply`（并发两请求，断言 1×`reserved` + 1×`cooldown`） |
| `test_proactive_reservation_release_restores_capacity` | :954-980 | `reserve_proactive_reply` + `release_proactive_reply`（max=1 释放后第二次可占） |
| `test_confirmed_proactive_reservation_is_idempotent_and_rate_limited` | :983-1027 | `confirm_proactive_reply`（重复 confirm）、confirm 后 `release` 返回 False、`rate_limited` 状态 |
| `test_proactive_reservation_heartbeat_extends_pending_lease` | :1030-1066 | `renew_proactive_reply`（zset score 延长断言） |
| `test_chat_commit_redis_steps_are_idempotent` | :1069-1119 | `push_message_once` / `push_global_interaction_once`（outbox 的 Redis 侧幂等原语） |
| `test_expired_proactive_reservation_is_pruned` | :1122-1152 | TTL 过期 pending 槽在 reserve 时被剪除（`now_ms` 前移） |

### 1b. 真实 Redis 集成（tests/komari_memory/test_redis_manager_integration.py）

| 测试名 | 位置 | 覆盖对象 |
|---|---|---|
| `test_real_redis_chat_commit_steps_are_idempotent` | :221-323 | 真实 Redis 上 `reserve_proactive_reply`（:231）、`renew_proactive_reply`（:239）、`push_message_once`/`push_global_interaction_once` 去重（:254-275）、互动 summary 租约 |

### 1c. handler 级预占生命周期（tests/komari_chat/test_message_handler.py）

| 测试名 | 位置 | 覆盖对象 |
|---|---|---|
| `test_proactive_attempt_reserves_then_confirms_after_delivery` | :1337-1432 | `_attempt_reply` 内 reserve（:1540-1557 源码）→ 生成后 renew（:1707-1712）→ `PendingReply.proactive_reservation_id` → `commit_delivered_reply` 直连路径 confirm（:1451-1455 源码） |
| `test_proactive_generation_failure_releases_reservation` | :1435-1501 | 生成抛 `_FavorabilityReadError` 时 finally 释放（源码 :1648-1660 + :1747-1753） |
| `test_reserve_failure_returns_failure_with_reaction_sent_false` | :2038-2112 | reserve 抛异常 → `ReplyFailureInfo(stage="reserve", reaction_sent=False)`（monkeypatch 注入 `_failing_reserve`，:2067-2072） |
| `test_commit_delivered_reply_does_not_trigger_reaction_callback` | :2115-2210 | 直连路径 `commit_delivered_reply`（无 repository 实例，:2208） |

### 1d. outbox 编排（tests/komari_chat/test_reply_commit_handler.py）

| 测试名 | 位置 | 覆盖对象 |
|---|---|---|
| `test_delivered_reply_commits_all_idempotent_steps` | :272-289 | `prepare_pending_reply` → `commit_delivered_reply`（outbox 路径）→ `_process_claimed_reply_commit` 四步（confirm/favorability/ai_history/interaction）→ `complete`，断言 repository record `COMPLETED` + Redis 集合状态 |
| `test_partial_commit_retries_without_reapplying_completed_step` | :293-316 | Redis push 失败一次 → record 回 `DELIVERED`、已完成的 step 不重放；`retry_pending_reply_commits()`（:313）重投成功，`favorability.application_count == 1` 保持 |

### 1e. 仓库层真实 PG 集成（tests/komari_chat/test_reply_commit_repository_integration.py）

`test_reply_commit_outbox_prepare_claim_steps_and_tombstone` :54-138：`prepare`（幂等，重复返回 False）→ `has_active_operation` → `mark_delivered(platform_message_id)` → `claim_operation`（PROCESSING + attempt_count=1）→ `renew_lease` → `mark_step`×4 → `complete` → 原生 SQL 校验 `komari_chat_reply_commit_outbox` 行（status/request_trace_id/platform_message_id、正文与 `proactive_reservation_id` 置空、completed_at）→ `cancel_prepared` 后 `has_active_operation=False` 且可重新 `prepare`。

### 1f. 插件入口编排（tests/komari_chat/test_plugin_entry.py，全为 fake handler）

| 测试名 | 位置 | 覆盖对象 |
|---|---|---|
| `test_send_failure_does_not_commit_reply_side_effects` | :202-279 | ActionFailed → prepare 调用、commit 不调用、`cancel_prepared_reply` + `discard_pending_reply` 调用 |
| `test_successful_send_commits_reply_side_effects_after_delivery` | :283-329 | 顺序 准备→发送→提交，`platform_message_id`（"7788"）透传 |
| `test_commit_failure_after_delivery_does_not_release_reservation` | :375-426 | 送达后 commit 抛异常 → **不**调用 discard（保留 outbox 记录） |
| `test_unknown_send_result_keeps_prepared_outbox_for_reconciliation` | :430-482 | send 抛 TimeoutError → 不 cancel、不 discard（`delivery_outcome="unknown"` 分支） |

### 1g. 其他锚点

- `tests/db/test_migration_chain.py:24` — 断言迁移链包含表名 `komari_chat_reply_commit_outbox`。

## 2. 各测试的手法

- **Redis Lua 语义级 fake**（test_redis_manager.py）：`_FakeRedis`（:88-96）+ `_FakePipeline`（:45-85）用 Python 字典/集合/zset 复刻 Redis 数据结构；`execute_command("EVAL", ...)` 按脚本内 marker 字符串（`"proactive_reserve"`、`"proactive_confirm"` 等，:138-192）分派到 `_eval_proactive_*` 纯 Python 实现（:200-266），`now_ms` 可手动前移模拟时间流逝（:95、:1050、:1125）。断言的是**行为语义**（状态码、zset 成员/score、去重结果），非参数转发。manager 经 `_build_manager`（:792-801）直接注入 fake `_redis`，monkeypatch 模块级 `get_config`。
- **handler 级调用记录 fake**（test_message_handler.py）：`_FakeRedisForDebug`（:792-884）只记录调用参数列表（`reserve_proactive_calls`/`confirm_proactive_calls`/`renew_proactive_calls`/`release_proactive_calls`，:800-803）并返回固定 `reservation_status="reserved"`；断言是**参数转发**（如 :1404-1411 断言 reserve 的完整 kwargs dict 列表）。另有一个无预占方法的 `_FakeRedis`（:135-170）供 `proactive_enabled=False` 的测试用。handler 用 `MessageHandler.__new__` 裸构造（:1244-1246 等），模块级 `get_config`、`user_data_plugin`、`build_prompt`、`generate_reply_with_tools` 等 monkeypatch 注入。
- **outbox 编排 fake**（test_reply_commit_handler.py）：`_FakeReplyCommitRepository`（:17-137）以 `records[operation_id]` dict 模拟状态机（PREPARED→DELIVERED→PROCESSING→COMPLETED），`_FakeRedis`（:140-186）记录 confirmed 集合 + 幂等 push 去重，`_FakeUserData`（:189-208）计数 adjust。handler 裸构造并直接赋值 `reply_commit_repository`/`redis`/`_reply_commit_owner`/`_last_reply_commit_cleanup`（:260-268）。断言**行为语义**（status 迁移、step 时间戳列、去重计数）。
- **真实 PG 集成**：`KOMARI_TEST_POSTGRES_URL` 门控（:19、:52 skipif），asyncpg 直连 `ReplyCommitRepository(pool)`，测试后 DELETE 清理（:130-137）。断言行为语义 + 原生 SQL 行内容。
- **真实 Redis 集成**：`KOMARI_TEST_REDIS_SOCKET` / `KOMARI_TEST_REDIS_URL` 门控（:23-24、:36-39 skipif），`flushdb` 前后清理。
- **插件入口**：`chat_module` fixture（test_plugin_entry.py:61-101）把 `komari_chat/__init__.py` 作为 `_entry_under_test` 加载，monkeypatch `_get_or_build_handler` 返回 duck-type fake handler；断言**编排顺序与分支**（调用/不调用、参数透传），不触碰真实实现。

## 3. 覆盖缺口

以下分支在现有测试中**没有任何测试**（按源码符号逐条核对）：

- **租约丢失（outbox 侧）**：`_mark_reply_commit_step` 中 `lease_lost.is_set()` 抛错（message_handler.py:1197-1199）、`mark_step` 返回 False 抛错（:1205-1207）、`_process_claimed_reply_commit` 完成前 `lease_lost` 检查（:1328-1330）、`_reply_commit_heartbeat` 中 renew 失败/异常置位 lost（:1167-1178）。fake 的 `renew_lease`/`mark_step` 恒返回 True（test_reply_commit_handler.py:92-110），无测试翻转它们。
- **租约丢失（预占侧）**：`_attempt_reply` 生成完成时 `reservation_lost.is_set() or not renewed` → `ProactiveReservationLostError` 失败分支（message_handler.py:1713-1723）；`_proactive_reservation_heartbeat` 失败置位（:1773-1779）。`_FakeRedisForDebug.renew_proactive_reply` 恒 True（test_message_handler.py:874），无测试令其返回 False。
- **孤儿预占 TTL 淘汰**：Redis 级 prune 有覆盖（test_redis_manager.py:1122-1152）；handler 级 `_release_proactive_reservation` 的释放失败"等待 TTL 回收"分支（message_handler.py:1489-1493）无测试（无测试令 `release_proactive_reply` 抛异常）；真实 Redis 上无 prune/confirm/release 测试（integration 只覆盖 reserve+renew，test_redis_manager_integration.py:230-243）。
- **outbox 崩溃恢复 / worker 重投**：`_reply_commit_worker` 循环本身（`komari_chat/__init__.py:84-97`）**零测试**；`retry_pending_reply_commits` 仅测单记录重投（test_reply_commit_handler.py:313），`claim_pending` 的 limit/批量语义（源码 :1380-1384）、`mark_failure` 的 `FAILED` 耗尽重试分支（源码 :1356-1374；fake :127-133 恒返回 `DELIVERED`）、`_finish_claimed_reply_commit` 异常→`mark_failure` 路径均未断言。`retry_pending_reply_commits` 的每小时 cleanup 分支（源码 :1393-1409）会执行（`_last_reply_commit_cleanup=0.0` 导致 3600s 门槛必然越过，test_reply_commit_handler.py:267）但无断言。
- **双确认竞态（直连 vs outbox 同时 confirm）**：handler 级无测试模拟同一 reservation 既走直连 confirm（message_handler.py:1451-1455）又被 outbox worker confirm（:1244-1257）；`commit_delivered_reply` outbox 路径中 `claim_operation` 返回 None 的分支（:1437）无测试。Redis 级 confirm 幂等有单测（test_redis_manager.py:983-1027），但无真实 Redis 双确认。
- **预占后生成失败的释放路径**：仅 `_FavorabilityReadError` 一条（test_message_handler.py:1435-1501）；`EmptyReplyError`（源码 :1689-1695）、`FavorabilityDeltaMissingError`（:1697-1705）、泛异常（:1661-1673）、CancelledError 中途退出（:1548/:1596/:1641，finally :1747-1753 释放）均未验证释放行为。
- **reserve 状态分支**：`_attempt_reply` 对 `cooldown`/`rate_limited`/`duplicate` 的 `(None, False, None)` 返回（源码 :1561-1569）无 handler 级测试；未知状态 `UnknownReservationStatusError`（:1570-1577）无测试（test_message_handler.py:2043 设置 `reservation_status="error"` 后立刻被 :2072 的抛错 monkeypatch 覆盖，该状态从未被消费）。
- **prepare 重复 operation**：`prepare_pending_reply` 返回 False 时入口 discard+return 分支（`__init__.py:261-265`）无测试（test_plugin_entry.py 所有 prepare 均返回 True）。
- **仓库层缺口**：integration 未覆盖 `claim_pending` 批量（真实 SQL :287）、`renew_lease` 错误 owner（真实 SQL :335）、`mark_failure`（真实 SQL :418）、`cleanup_tombstones`（真实 SQL :462）。

## 4. 真实环境门控集成测试覆盖路径

- **PG 门控**（`KOMARI_TEST_POSTGRES_URL`，test_reply_commit_repository_integration.py:19/:52）：单记录全生命周期 prepare→mark_delivered→claim_operation→renew_lease→mark_step×4→complete→行内容校验→cancel_prepared→重 prepare（:63-128）。即 outbox 状态机的主成功路径 + cancel，无批量/失败/超时路径。
- **Redis 门控**（`KOMARI_TEST_REDIS_SOCKET`/`KOMARI_TEST_REDIS_URL`，test_redis_manager_integration.py:23-24/:36-39）：`test_real_redis_chat_commit_steps_are_idempotent`（:221-323）覆盖 reserve（:231）+ renew（:239）+ 两条 Redis 幂等原语（:254-275）——即预占的"占用/续期"与 outbox 的 Redis 写入去重；**无 confirm、无 release、无 cooldown/rate_limited/duplicate 状态、无并发、无双确认**。另两个集成测试（conversation owner takeover :40、dead letter requeue :129）与预占/outbox 无关。

## 5. 引入新「预占 module」并切换调用方时的测试影响

**直接锚定被移动符号、大概率会破的测试：**

- `tests/komari_memory/test_redis_manager.py:924-1153`（6 个预占测试）— 直接调用 `RedisManager.reserve/confirm/renew/release_proactive_reply`，且 fake 依赖脚本 marker `"proactive_reserve"` 等（:142-145）；若方法从 `RedisManager` 移除则全破。
- `tests/komari_memory/test_redis_manager_integration.py:221-323` — 真实 Redis 上调用 `manager.reserve/renew_proactive_reply`（:231/:239），同样锚定 `RedisManager` 实例方法。
- `tests/komari_chat/test_message_handler.py:1337-1501` — 通过 `self.redis` 假对象断言预占四方法的**精确调用参数**（:1404-1432、:1495-1500），切换调用方后签名/属性名任一变化即破。
- `tests/komari_chat/test_message_handler.py:2038-2112` — monkeypatch 目标 `redis.reserve_proactive_reply`（:2065/:2072）锚定 handler 对该属性名的调用。
- `tests/komari_chat/test_reply_commit_handler.py:272-316` — 锚定 `MessageHandler.prepare_pending_reply`/`commit_delivered_reply`/`retry_pending_reply_commits` 及 `reply_commit_repository`/`_reply_commit_owner`/`_last_reply_commit_cleanup` 实例属性（:260-268）；`_FakeReplyCommitRepository` 实现即当前仓库接口的契约快照（:17-137）。
- `tests/komari_chat/test_reply_commit_repository_integration.py:14-17/:54-138` — 锚定 `komari_bot.plugins.komari_chat.repositories.reply_commit_repository` 导入路径、`ReplyCommitRepository(pool)` 构造、`PendingReplyCommit` 字段名、表 `komari_chat_reply_commit_outbox` 及列名。
- `tests/komari_chat/test_plugin_entry.py:202-482` — 锚定 `komari_chat/__init__.py` 对 handler 的 duck-type 入口调用（`prepare_pending_reply`/`commit_delivered_reply`/`discard_pending_reply`/`cancel_prepared_reply`，`__init__.py:253-321`），入口编排若改挂新对象则破。

**可原样保留作回归网的测试：**

- `tests/komari_chat/test_message_handler.py:397-558`（`test_attempt_reply_only_rewrites_current_message`，直连 commit 路径 :539）、`:1229-1335`（`test_normal_attempt_reply_defers_side_effects_until_delivery`，直连路径 :1324）、`:2115-2210`（`test_commit_delivered_reply_does_not_trigger_reaction_callback`）、`:2213-2269`（`test_read_buffers_failure`）— 使用无预占方法的 `_FakeRedis` 或仅用缓冲方法，不触碰将被移动的符号；前提是 `MessageHandler.commit_delivered_reply` 直连路径与 `_attempt_reply` 签名保留。
- `tests/komari_memory/test_redis_manager.py` 中 conversation/interaction 租约测试（:1268-1727 等）与 `test_chat_commit_redis_steps_are_idempotent`（:1069-1119，若 `push_message_once`/`push_global_interaction_once` 不移走）。
- `tests/db/test_migration_chain.py:24` — 只锚定表名。
- `tests/komari_chat/test_reply_commit_handler.py` 的两个测试在仓库接口不变的前提下可整体复用（fake 已实现全部仓库方法），但测试文件本身锚定 `message_handler` 模块导入路径（:214）。

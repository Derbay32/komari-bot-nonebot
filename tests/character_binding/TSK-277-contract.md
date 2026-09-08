# TSK-277 测试契约：QQ 群绑定向导、旧名迁移、本群改名解绑、旧入口退役

本文件固定 TSK-277 的最窄可观察公共接缝。测试只通过这些接缝观察真实 QQ
handler、wizard 会话、真实发送载荷与真实 PostgreSQL 事务；不实现生产算法，
不重复 273 取证与 274 准入。

## 公共 seam

### wizard 模块

`komari_bot.plugins.character_binding.wizard` 导出（测试允许模块内 import，
不强制跨插件顶层重导出）：

```python
class BindingWizard:
    def __init__(
        self,
        *,
        coordinator: QQBindingCoordinator,
        session_factory: Callable[[], AsyncSession],
        clock: Callable[[], datetime],
        manager: CharacterBindingManager | None = None,
        legacy_loader: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None: ...

    async def handle_event(
        self,
        event: GroupAtMessageCreateEvent,
        token: QQAdmissionToken,
    ) -> WizardReply | None: ...

    async def authorize_send(
        self,
        token: QQAdmissionToken,
        reply: WizardReply,
    ) -> bool: ...

    async def get_session(self, scope: WizardScope) -> WizardSessionView | None: ...

    async def cancel(self, session_code: str) -> bool: ...

class BindingCommitOutcomeUnknownError(RuntimeError): ...

def set_binding_wizard(wizard: BindingWizard | None) -> None: ...
def get_binding_wizard() -> BindingWizard | None: ...
```

`coordinator` 只需满足以下端口（真实 `QQBindingCoordinator` 已具备）：

```python
async def claim_initial_bind(request: QQInitialBindRequest) -> QQBindClaim | None
async def resolve_verified_binding_session(app_id, group_openid, member_openid) -> QQVerifiedBindingSession | None
async def recheck(token, *, effect: str) -> QQEffectDecision
async def cancel(session_code: str) -> None
```

值对象（keyword 构造）：

```text
WizardScope(app_id, group_openid, member_openid)
WizardStep = challenge_pending | legacy_choice | name_input
           | binding_confirm | rename_confirm | unbind_confirm | completed
WizardOperation = bind | rename | unbind
WizardSessionView(session_code, step, operation, scope, character_name,
                  created_at, expires_at, challenge_sent, completed)
WizardButton(label, command)
WizardReply(body, keyboard, reply_to_message_id)
```

- `handle_event` 返回 `None` 表示不产生任何可见 QQ 消息：静默拒绝、证据到达
  后无自动追加、未映射群重复 `/bind`、发送准入拒绝、同一入站消息重复投递。
- 每次可见回复与最终 commit 前，wizard 用 `coordinator.recheck(token,
  effect=token.scope)` 复核；`allowed=False` 时不得产生可见效果。
- `authorize_send` 是 handler 在真正调用 `bot.send` 之前的最后一道同步复核
  （覆盖 handle_event 返回后到发送之间的等待窗口）。
- `session_factory()` 返回的 `AsyncSession` 由 wizard 自己打开、关闭，且只在
  一个明确边界调用一次 `await session.commit()`（不得用 `async with
  session.begin()` 把 commit 藏进上下文），以便测试注入提交结果不确定。
- 提交成功后 wizard 调用 `coordinator.cancel(session_code)` 清理临时
  claim/evidence，并保留自身 completed 幂等态（重复 confirm 不再写库）。
- `manager` 非空时，只有 commit 成功后才允许发布进程内快照；失败与提交结果
  不确定时快照保持原值。

### 真实 handler

`komari_bot.plugins.character_binding.qq_commands` 注册恰好一个 matcher 符号
`bind_qq`，工厂 `on_message`（不得用 `on_type` 逃 census）。行为：

- 只处理精确 `GroupAtMessageCreateEvent` 且 `content` 以 `/bind` 开头的入站；
- 从 NoneBot `state` 用 `get_qq_admission_token(state)` 取 274 写入的 token，
  不得再次 `qualify_qq_event` 或重复初始 claim；
- `reply = await wizard.handle_event(event, token)`；`reply is None` 直接返回；
- `await wizard.authorize_send(token, reply)` 为假则不发送；
- 发送载荷必须是真实 QQ Markdown：`post_group_messages` 的
  `markdown.content` 等于 `reply.body` 原文，`msg_type == 2`，按钮进入
  `keyboard`（每按钮 `action.type == 2`、`action.data` 为命令），普通
  `content` 不承载正文；
- 挑战消息带原生引用：`message_reference.message_id == event.id`；
- 发送异常只发生一次，不补发任何 fallback 消息。

### 按钮命令（权威定稿）

| 视图 | 按钮 → 命令 |
| --- | --- |
| 挑战 | 继续绑定 → `/bind`；取消 → `/bind cancel <session>` |
| 旧名迁移 | 沿用旧名 → `/bind reuse <session>`；重新填写 → `/bind name <session> `；取消 → `/bind cancel <session>` |
| 填写名字 | 填写名字 → `/bind name <session> `；取消 → `/bind cancel <session>` |
| 最终绑定确认 | 确认绑定 → `/bind confirm <session>`；修改名字 → `/bind name <session> `；取消 → `/bind cancel <session>` |
| 已有绑定 | 改名 → `/bind rename`；解绑 → `/bind unbind` |

`/bind name <session>` 之后的完整剩余文本（不按空格截断）交给既有
`validate_character_name`/`character_name_key` 规范器；按钮填入的
`/bind name <session> ` 含尾随分隔空格，用户补充名字后手动发送。

## 定稿文案

测试逐字断言以下文案（含中文弯引号、换行与 Markdown 记号）：

- 挑战：`正在确认你的本群身份。\n会话码：<session>\n请点击“继续绑定”进入下一步。\n本次绑定流程有效期为 10 分钟，可随时取消。`
- 未就绪：`身份确认尚未完成，请稍后点击“继续绑定”。`
- 填写名字：`请填写本群角色名。\n\n长度为 **1–64 个字符**，同群不可重名。`
- 旧名迁移：`你之前的角色名是：**<name>**。\n\n是否在本群继续使用？`
- 最终确认：`本群角色名：**<name>**\n\n仅对当前群生效。\n\n确认绑定？`
- 绑定成功：`已完成绑定。\n\n本群角色名：**<name>**。`
- 已有绑定：`本群角色名：**<name>**\n\n仅对当前群生效。`
- 改名确认：`将本群角色名从“<old>”改为“<new>”。\n确认修改？`
- 改名成功：`本群角色名已修改为：<name>。`
- 解绑确认：`将清除你的本群角色名“<name>”。\n两个机器人入口已确认的账号关联会保留。\n其他群不受影响；当前对局仍使用原名字。\n确认解绑？`
- 解绑成功：`已解除本群角色名绑定。\n再次开局或加入前，请通过 /bind 设置角色名。`
- 取消：`已取消本次操作，原有绑定未变更。`
- 失效：`本次操作已失效，请重新运行 /bind。`
- 名字格式：`请输入 1–64 个字符的角色名。`
- 名字控制字符：`角色名不能包含换行、控制字符或零宽字符，请换一个名字。`
- 名字重复：`这个角色名在本群已被使用，请换一个名字。`
- 未知命令/参数缺失：`绑定命令格式不正确，请发送 /bind 查看当前步骤。`
- 他人流程：`这不是你的绑定流程，请通过 /bind 发起自己的操作。`
- 错误群：`请在发起本次绑定的群内继续操作。`
- 错误步骤：`当前步骤不支持这个操作，请发送 /bind 查看当前步骤。`
- 无旧名：`没有可沿用的旧角色名，请填写本群角色名。`
- 无角色名：`你还没有设置本群角色名，请通过 /bind 完成绑定。`
- 群信息无法确认：`暂时无法确认本群信息，请稍后重新运行 /bind。`
- 群映射冲突：`本群的机器人身份关联存在冲突，请联系管理员处理。`
- 成员冲突：`你的本群账号关联存在冲突，请联系管理员处理。`
- 身份验证失败：`暂时无法完成身份验证，请稍后重新运行 /bind。`

## 会话与作用域语义

- 每个 `(app_id, group_openid, member_openid)` 至多一个活动草稿；不同成员/群/
  应用可并行。
- TTL 从会话创建起 10 分钟绝对有效；重复命令、无效输入、格式错误与重名都不
  延长；恰好到期即失效。
- 会话身份 = `(app_id, group_openid, member_openid)`；命令携带的 session 码
  必须匹配。成员或应用不匹配 → 他人流程文案；同应用同成员但群不匹配 → 错误群
  文案；码不存在/已取消/已过期 → 失效文案。
- 同一入站消息 ID 重复投递只处理一次（不重复写库、不重复发送）。
- 旧按钮/旧确认/迟到证据只作用于原会话；新会话不被影响。
- 取消在发送准入拒绝时仍丢弃本地草稿，但静默；取消不主动通知他人。

## 流程

- 未映射群首次 `/bind`：wizard 使用 274 已写入 state 的 `binding_challenge`
  claim，发送一条挑战（原生引用原人类消息），不重复挑战；证据由 273/274
  静默采集，wizard 不自动追加消息。
- 已映射群成员未绑定：wizard 经 `coordinator.claim_initial_bind` 请求一次取证
  claim，首次发挑战，再次 `/bind` 发“未就绪”；不重建、不续期、不重复取证。
- **pending → 证据 → 再次 /bind**：`challenge_pending` 会话在协调器出现同
  `session_code` 的已验证会话后，不自动发送任何消息；下一次 `/bind`（未映射群
  scope `binding`、已映射未绑定 scope `business` 均可）消费证据并推进到旧名
  选择或名字输入。推进必须复用原 session_code、原 expires_at，不重建、不续期、
  不重复初始 claim。
- 已验证身份（scope `binding` 或 `business` 且成员身份可用）：`/bind` 跳过
  核验，进入旧名迁移选择或名字输入。
- 旧名候选只在本人主动 `/bind`、当前群未绑定、numeric identity 已验证时读取；
  `/bind reuse` 显式沿用并重新规范/唯一校验；不做自动迁移、日常 fallback、
  其他群身份猜测。
- `/bind rename` → `name_input`；`/bind name <session> 新名` → `rename_confirm`；
  `/bind confirm` → 成功。确认前/取消/失败保留原名。
- `/bind unbind` → `unbind_confirm`；`/bind confirm` → 只清名字、保留 group 与
  member identity。
- 已有绑定且无活动会话时 `/bind` 展示已有绑定视图；无角色名时 rename/unbind
  提示先绑定。
- confirm 只在最终提交阶段可用，不作中间推进。
- **confirm 覆写保护（按 operation 判定）**：提交前在同一事务内重读 canonical
  名字。`bind`：canonical 等于草稿目标 → 幂等成功不写；canonical 为空 → 写入；
  canonical 为其他非空值 → 拒绝并返回失效文案，绝不覆盖。`rename`：canonical
  等于目标 → 幂等成功；等于确认时展示的旧名 → 改名；其他值 → 拒绝。`unbind`：
  canonical 为空 → 幂等成功；等于确认时展示的名字 → 清除；其他值 → 拒绝。
  这样 after-commit/before-commit 不确定后，即使同一成员被另一合法维护流程
  改名或新建正式状态，重复旧 confirm 也绝不覆写后续正式值。

## 提交与并发

- 群映射 + 成员身份 + 角色名在同一事务提交，复用 `BindingTransaction` 与
  `komari_bot.db.group_transaction_locks.lock_group_scope`（276 共享组锁）。
- 双向群/成员 identity 冲突保留原记录；同群并发同名只有一方成功，失败方仍可
  改名；故障保留旧值。
- 提交结果不确定必须抛 `BindingCommitOutcomeUnknownError`，不得返回成功或肯定
  失败文案，也不得猜测新 session 绕过；后续重复 confirm 依据 canonical 记录
  收敛：已提交且等于目标则不再改写，未提交且无既有正式值则补做一次，canonical
  已被其他合法流程改为不同值则拒绝（失效文案）且不覆写。
- 两协议公开查询（`get_qq_character_name` 与 `get_character_name`）读取同一
  canonical 记录；缓存只在 commit 成功后发布。
- **成功发送与临时会话清理的次序**：提交成功后 wizard 清理临时 claim/evidence
  （`coordinator.cancel(session_code)`）不得使成功回复的发送前重审失效；成功
  文案必须实际发送。实现可推迟清理到发送授权之后，或对已确认完成的会话用
  canonical 状态授权发送。

## AC → 测试映射

| AC | 测试节点 |
| --- | --- |
| 1 命令/按钮/纯文本可达/无 fallback | `test_tsk277_handler.py::test_real_handler_sends_markdown_challenge_with_quote_and_no_second_claim`、`test_tsk277_handler.py::test_real_handler_ignores_non_bind_and_non_native_events`、`test_tsk277_handler.py::test_real_handler_send_failure_does_not_append_fallback`、`test_tsk277_wizard.py::test_first_unmapped_bind_sends_one_challenge_and_repeat_is_silent`、`test_tsk277_wizard.py::test_legacy_candidate_only_after_verified_active_bind`、`test_commands.py::test_character_binding_has_no_onebot_bind_command_matchers` |
| 2 名字规范器与注入防护 | `test_tsk277_wizard.py::test_name_command_uses_full_remaining_text_and_normalizes`、`test_tsk277_wizard.py::test_name_validation_rejects_and_keeps_current_step`、`test_tsk277_wizard.py::test_name_rendering_does_not_inject_markdown_or_real_mention`、`test_tsk277_pg.py::test_same_group_concurrent_name_has_one_winner_and_loser_can_rename` |
| 3 每作用域一草稿/绝对 TTL/重复不重建 | `test_tsk277_wizard.py::test_ttl_is_absolute_ten_minutes_and_repeat_does_not_extend`、`test_tsk277_wizard.py::test_mapped_unbound_member_requests_single_claim_then_not_ready`、`test_tsk277_wizard.py::test_sessions_are_isolated_per_member_and_cancel_keeps_other`、`test_tsk277_wizard.py::test_duplicate_inbound_message_is_not_processed_twice`、`test_tsk277_handler.py::test_real_handler_does_not_send_twice_for_duplicate_inbound` |
| 4 首次挑战/静默取证/再次 /bind 消费 | `test_tsk277_wizard.py::test_first_unmapped_bind_sends_one_challenge_and_repeat_is_silent`、`test_tsk277_wizard.py::test_unknown_group_pending_evidence_advances_on_next_bind_without_rebuild`、`test_tsk277_wizard.py::test_mapped_pending_evidence_advances_to_legacy_without_reclaim`、`test_tsk277_handler.py::test_real_handler_sends_markdown_challenge_with_quote_and_no_second_claim`、`test_tsk277_pg.py::test_real_handler_evidence_progression_and_success_send_survive_cancel` |
| 5 会话/作用域/阶段核验与迟到失效 | `test_tsk277_wizard.py::test_cross_member_group_and_app_commands_are_rejected`、`test_tsk277_wizard.py::test_cancelled_session_code_is_stale_for_new_session`、`test_tsk277_wizard.py::test_cancel_drops_draft_even_when_send_admission_denies`、`test_tsk277_wizard.py::test_wrong_step_and_unknown_command_use_authoritative_texts`、`test_tsk277_wizard.py::test_active_draft_blocks_rename_and_unbind_switch` |
| 6 改名/解绑 preview→confirm | `test_tsk277_pg.py::test_rename_preview_confirm_conflict_and_cancel`、`test_tsk277_pg.py::test_rename_or_unbind_without_character_name_prompts_binding`、`test_tsk277_pg.py::test_unbind_clears_only_name_and_identity_stays_reusable`、`test_tsk277_wizard.py::test_active_draft_blocks_rename_and_unbind_switch` |
| 7 旧名候选与 reuse | `test_tsk277_wizard.py::test_legacy_candidate_only_after_verified_active_bind`、`test_tsk277_wizard.py::test_unverified_identity_never_reads_legacy_candidate`、`test_tsk277_wizard.py::test_reuse_without_legacy_name_reports_missing_candidate`、`test_tsk277_pg.py::test_legacy_candidate_reuse_and_conflict` |
| 8 原子事务/并发/不确定提交/覆写保护/缓存 | `test_tsk277_pg.py::test_confirm_commits_atomically_and_publishes_cache_only_after_commit`、`test_tsk277_pg.py::test_group_and_member_conflicts_preserve_original_records`、`test_tsk277_pg.py::test_same_group_concurrent_name_has_one_winner_and_loser_can_rename`、`test_tsk277_pg.py::test_commit_outcome_unknown_after_commit_does_not_report_and_converges`、`test_tsk277_pg.py::test_commit_outcome_unknown_before_commit_leaves_no_record_and_repeat_writes_once`、`test_tsk277_pg.py::test_commit_unknown_after_commit_never_overwrites_later_maintenance_rename`、`test_tsk277_pg.py::test_commit_unknown_before_commit_never_overwrites_existing_new_formal_state` |
| 9 效果前重审/state token/映射≠白名单 | `test_tsk277_handler.py::test_send_time_recheck_suppresses_reply_in_wait_window`、`test_tsk277_handler.py::test_positive_control_send_performs_send_time_recheck`、`test_tsk277_handler.py::test_restricted_group_stays_silent_even_for_configured_superuser`、`test_tsk277_handler.py::test_real_handler_sends_markdown_challenge_with_quote_and_no_second_claim`、`test_tsk277_wizard.py::test_denied_recheck_suppresses_visible_reply_and_draft_effect`、`test_tsk277_wizard.py::test_mapped_pending_evidence_advances_to_legacy_without_reclaim`、`test_tsk277_pg.py::test_real_handler_evidence_progression_and_success_send_survive_cancel` |
| 10 wizard 拥有事务与幂等完成态 | `test_tsk277_pg.py::test_confirm_commits_atomically_and_publishes_cache_only_after_commit`、`test_tsk277_pg.py::test_real_handler_evidence_progression_and_success_send_survive_cancel` |
| 11 权威文案 | 全量断言（上表文案常量），节点见 AC1–AC10 各用例 |
| 12 旧入口退役/census/help | `test_commands.py::test_census_retires_legacy_matchers_and_registers_qq_handler`、`test_commands.py::test_binding_help_metadata_documents_new_wizard_syntax`、`test_commands.py::test_real_package_keeps_silent_evidence_listener_only`、`tests/group_admission/test_entry_gate_census.py::test_matcher_census_exact_count`、`tests/group_admission/test_entry_gate_census.py::test_matcher_census_factory_totals`、`tests/group_admission/test_entry_gate_census.py::test_matcher_census_exact_registrations`、`tests/group_admission/test_entry_gate_census.py::test_matcher_census_source_paths_match_ast_scan`、`tests/group_admission/test_command_admission.py::test_no_orphan_command_matchers_in_target_modules` |

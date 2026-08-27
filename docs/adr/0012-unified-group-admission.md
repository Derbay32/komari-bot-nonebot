---
status: accepted
---

# 统一群聊准入由独立模块拥有并逐效果重新裁决

角色可以在哪些群开展业务，此前由各插件自行解释：八个业务插件的配置各持一份 `group_whitelist`，语义互相冲突；`permission_manager` 混杂插件开关、用户/群名单与 SUPERUSER 检查；私聊输入与后台工作没有统一门禁。我们决定引入统一的**群聊准入策略**，交由新的独立基础插件 `komari_bot.plugins.group_admission` 拥有：策略解释、不可变的策略修订快照、生命周期、配置发布、可观测性与专属管理 HTTP Adapter 全部收归该模块，业务插件只消费**准入裁决**，不自行解释策略。

本 ADR 汇总 TSK-213（模块所有权与配置契约）、TSK-215（领域契约）、TSK-216（NoneBot 全局拦截接缝实证）、TSK-217（可观测性）、TSK-219（统一执行边界）与 TSK-214（破坏性迁移设计）的已冻结决策；规范术语见根目录 `CONTEXT.md` 的「群聊准入」章节。本文为后续实现票提供设计依据，写作时生产代码与迁移均未落地。

## 决策

### 领域契约与策略真值表

- 群聊准入策略决定角色可以在哪些群开展业务；每次变更以一个完整的**策略修订**生效，策略修订是判断某项行为应采用哪组规则的时间基准。**群状态**只有获准与受限两种。
- 真值表：黑名单模式空集合表示全部群获准，非空集合内受限、其余获准；白名单模式空集合同样表示全部群获准（明确领域规则，不采用「空白名单拒绝全部」的直觉语义），非空集合内获准、其余受限。模式与群号集合必须作为同一个策略修订原子发布。
- 一次**群行为**必须先确定全部**关联群**（来源群、目标群与数据归属群）。只有全部关联群均获准，行为才可能取得「开展业务」资格；任一关联群受限，整个不可拆分行为都不得开展。可拆分批次按实际目标逐目标裁决（如批量公告逐个目标群），受限目标不阻断其他获准目标。可分群混合数据若只选择获准群的贡献，关联群仅包含实际消费的来源群；不可拆分地消费多个来源群时，这些群全部成为关联群。
- **行为资格**分为开展业务、既成事实收尾、技术清理与拒绝。调用方按准备执行的行为目的（intent：`BUSINESS`、`FACT_FINALIZATION`、`TECHNICAL_CLEANUP`）申请裁决；取得既成事实收尾或技术清理资格不能被扩张解释为可开展普通业务。普通业务与既成事实收尾缺少可靠关联群时拒绝；只有技术清理允许空关联群。
- 准入撤销未来能力，但不篡改已经发生的事实：已送达或待确认回复可以继续送达对账；受限前冻结的送达后承诺仍必须完成，随后新写入立即随所属群休眠，不再触发进一步总结或召回；已准备但未发送的回复在撤销后终止为未送达并释放预占，不得等待恢复后补发。
- **私聊输入**不构成合法的角色业务触发源，在全局入口静默拒绝，收到后保持静默，不回复「私聊已禁用」等提示。**系统私聊通知**是封闭类别：仅允许发给 SUPERUSER 的运维告警、诊断与处置结果，以及发给直接受影响用户的权限/状态生命周期通知（如自然解封）；其他主动私聊一律拒绝。预定义的平台连接、心跳、启动/关闭行为属于**系统行为**；策略查询、更新与健康检查始终可进入控制面。需要群上下文却缺少、无法解析或无法验证群归属时故障关闭；缺少群号不能自动解释为系统行为。
- 每个单独业务效果都有自己的**准入时点**，策略变更不追溯已经越过该时点的效果。现有领域词「判定」继续专指角色对群消息作出的社交输出决策；准入控制统一使用「裁决」，不复用「判定」。

### 模块所有权与业务接口

- 单一独立基础插件同时封装策略解释、不可变修订快照、最近有效策略（LKG）、冷启动状态、配置发布、可观测性与专属管理 HTTP Adapter；不并入 `permission_manager`，不创建只有契约类型的共享包。删除该插件后，这些复杂性会重新散回所有调用方，证明它是足够深的模块而非 pass-through。
- 业务调用面只有两个同步 callable：`adjudicate(associated_group_ids, *, intent)` 与 `get_runtime_state()`；顶层同时重导出 `AdmissionIntent`、`AdmissionQualification`、`AdmissionResult`、`AdmissionRuntimeState`、`AdmissionRuntimeStatus` 类型身份。应用装配层额外取得 `register_group_admission_api`，它不是业务裁决入口。
- `adjudicate()` 无 I/O、无数据库访问，只读取不可变运行时快照，支持每个准入时点廉价复查；结果包含实际授予的行为资格、生效策略 revision 与不含正文的机器原因分类。
- 不公开 bool helper（如 `is_group_allowed()`）、名单、快照、`force_reload()`、Service、配置对象或万能 wrapper；caller 不得自行解释 mode/list、持有旧快照或跳过刷新。
- 跨插件引用遵循 ADR-0006 的顶层暴露面规则。依赖方向单一：`group_admission` 依赖 `config_manager`，不依赖 `permission_manager`、`user_ban` 或任何业务插件；所有执行群行为的业务插件与 `komari_management` 依赖它并 `require("group_admission")`。插件开关、用户封禁与 SUPERUSER 身份鉴权只在准入之后进一步收窄，永远不能扩大准入资格；管理身份没有裁决 bypass，策略修改能力只来自管理鉴权。

### 策略存储与发布

- 强类型单行表 `komari_group_admission_config`：固定主键 `id=1`、CAS 修订号 `revision`、`updated_at`、`policy JSONB NOT NULL`。完整策略存为一个原子对象 `{"mode": "blacklist" | "whitelist", "group_ids": [...]}`，整个 `{mode, group_ids}` 以 revision CAS 一次原子发布，保证不出现 mode 与 group_ids 分步更新的中间修订。
- `mode` 只接受 `blacklist` / `whitelist`；`group_ids` 只接受正整数 OneBot 群号，拒绝 bool、零、负数与非整数，验证时确定性去重、排序，运行时编译为 `frozenset[int]`。默认 `blacklist + []`；存储健康但记录缺失时初始化并持久化为「全部群获准」。
- 复用 `config_manager` 既有的版本化快照（`value / revision / updated_at`）、快照 listener 与 strict CAS 基础设施，不自建 ORM Repository，不复制配置连接、CAS 与 revision watcher；strict CAS 不自动基于数据库最新值重试覆盖，用于专属管理 PUT 的 `If-Match`。ConfigManager 接纳本地写入或 watcher 发现的更高 revision 时同步通知准入插件，插件完整校验编译后原子发布。
- 不提供 `plugin_enable`，避免形成关闭总闸门的 bypass；不读取旧插件名单，不提供 alias、双读、运行时 fallback 或兼容配置。
- 本进程管理更新只有在 listener 已发布新运行时 revision 后才返回成功；外部受支持写入按安全配置最多约 0.25 秒轮询发现。当前单 worker 部署是「管理响应返回前本进程已发布」的一致性前提；未来放开多 worker 时必须重新设计跨 worker 发布确认，不能复用现有轮询并宣称强一致。

### 运行时生命周期与 LKG

- 运行时状态只有 `ready`（有合法快照、最近一次存储确认成功，按策略裁决）、`degraded`（有最近有效快照，但当前存储刷新失败或更高 revision 非法，按 LKG 裁决）、`failed`（从未建立合法快照，故障关闭）三种。
- LKG 只存在当前进程内存，不持久化到 Redis、磁盘或第二张表；PostgreSQL 强类型配置是唯一持久真源。已有有效快照后的存储失败无限期保留当前 LKG，不按隐藏 TTL 失效；进程重启后必须重新从 PostgreSQL 建立合法策略，不能用第二真源冒充冷启动成功。
- 更高 revision 先完整校验和编译，再原子替换旧快照，并发读取不撕裂；乱序或较低 revision 忽略。后台单飞重试使 `failed` / `degraded` 在存储恢复后进入 `ready`；shutdown 取消刷新任务、注销 listener 并清空引用。准入的 ConfigManager 延迟到 `group_admission` startup 创建和初始化，初始化异常收敛为 `failed`，不得让通用配置预热直接中止进程。
- 冷启动无快照时拒绝全部新群业务，但仍允许既成事实收尾、技术清理、策略健康查询和经批准的系统私聊通知。数据库不可用时策略更新明确失败，不得先返回成功再异步尝试持久化。

### 管理 HTTP Adapter

- 准入插件自己拥有策略与状态 HTTP Adapter；`komari_management` 经 `require("group_admission")` 从插件顶层取得 `register_group_admission_api`，并提供管理凭据、CORS 与审计装配参数。准入插件只依赖 `komari_bot.management` 的共享鉴权/审计工具，不反向依赖 `komari_management` 插件；管理插件禁用只意味着不挂载 HTTP Adapter，不影响运行时准入。
- 专属路由：`GET /api/v2/group-admission/policy`、`PUT /api/v2/group-admission/policy`、`GET /api/v2/group-admission/status`。不注册通用配置字段写入口，也不提供 reload 路由。
- GET policy（`config:read`）从 PostgreSQL 返回完整策略、revision 与 updated_at，并设置强 `ETag: "<revision>"`；存储不可读返回 503，不得以 LKG 冒充持久配置。PUT（`config:write`）必须提交完整 policy、强 `If-Match` 与管理变更原因，先校验、编译候选，再 strict CAS 持久化并发布本地快照，只有本地 effective revision 已更新后才返回成功与新 ETag；禁止仅更新内存，存储不可写时失败。If-Match 缺失/弱/非法与 policy 非法返回 422，revision 冲突返回 409，存储不可写返回 503 并显式区分已持久化但未本地发布的情形。
- GET status（`config:read`）只读取进程内不可变状态、无存储 I/O，`ready` / `degraded` / `failed` 均返回 200；只投影运行时健康、封闭问题码、configured/effective revision、快照时间、是否使用 LKG 与无动态维度的低基数计数，不返回策略模式、群号列表、异常正文或动态身份。configured_revision 与 effective_revision 不同，表示更高持久修订未通过校验或本地发布。
- 管理审计只记录操作者、请求 ID、动作、旧/新 revision 与规范化策略 SHA-256 指纹，不记录完整群号集合。群目标单目标管理行为受限时返回 403、无有效策略时返回 503，不伪装 404；可拆分批量行为逐目标返回结果，受限目标不计业务失败、不消耗重试、不阻断其他获准目标。

### 入口接缝与准入时点

- NoneBot 使用 `event_preprocessor` 作为最早入站粗门禁：group message / notice / request 提取全部关联群并交由统一裁决；私聊输入静默拒绝；meta/system 事件显式放行；关联群缺失、非法或无法可靠归属的入站业务事件故障关闭。
- NoneBot 并发运行全部 event preprocessor，内部注册表不提供可依赖的顺序：其他 event preprocessor 在未自行通过准入前不得产生业务效果；注册顺序不能作为安全保证。
- `run_preprocessor`（matcher rule 已先执行、只取消当前 matcher、破坏被拒绝 matcher 原有的传播阻断语义）、高优先级 gate matcher（同优先级 matcher 已并发执行、要求全局优先级纪律、matcher rule 已发生）与 task cancellation（matcher 经 shield 运行、无法接收即时取消）均被明确拒绝作为统一准入机制。`user_ban` 继续承担独立的 command/chat 收窄职责，但没有准入 bypass。
- 每个 LLM/工具调用、平台读取/发送、持久写入、队列领取等单个不可分业务效果前，效果所有者同步读取当前不可变策略快照并重新裁决；裁决是效果前的最后一个同步步骤，裁决与效果提交点之间不得插入无关 `await`；一次裁决只授权紧随其后的一个不可分效果，结果不缓存跨效果复用。副作用复查失败使用正常控制流返回，不制造 matcher 异常。
- 已越过准入时点的单个原子效果可以完成；后续效果按当前修订重新裁决。「立即生效」不追溯已经发出的不可逆请求，但禁止新修订发布后尚未开始的后续效果。
- 瞬时互动被拒绝后立即终止，恢复准入后不复活；持久群工作保存安全进度、释放租约并休眠，不计失败、不消耗重试。

### 效果边界：平台输出、持久工作与数据面

- Repository 与低层 LLM/Embedding/OneBot/Redis Adapter 策略无感，不猜群号、不 import 准入插件；由最接近业务目的且仍持有归属的编排 Module 在调用通用 Adapter 前放置 seam。可能脱离当前调用栈的业务对象必须显式携带归属（履约父记录、提案、对话快照、记忆 job、多群加工项目、公告目标、`create_task()` 的不可变任务输入）；禁止把 Event 当作后台归属凭据、用 `ContextVar` 授权、缓存裁决结果，或因函数签名缺少群号而归成系统行为。一个内部 Module 若开启新的独立子效果，其内部接口必须接收任务归属并在新效果前重新裁决。
- 每次群回复、恢复发送、表情反应、固定失败文本、群总结、提案通知、维护公告等平台输出都有独立准入时点：准备载荷、选择 Bot、确认能力后，对目标群 `BUSINESS` 裁决并立即开始一次平台调用；已开始的平台调用允许完成，后续消息、反应或其他目标群重新裁决。受限的瞬时载荷直接丢弃，不等待恢复、不重发、不发送准入提示。`get_msg`、`fetch_emoji_like`、群历史等消费具体群业务内容的平台读取在调用前按该群裁决；`get_group_list` 等仅取传输元数据的系统准备可执行，但可达不等于获准，管理投影逐群过滤，后续具体读取/发送仍重新裁决。
- 回复履约按阶段裁决：生成结束、完整 Draft 持久准备前为 `BUSINESS`（受限时只保留无正文、无承诺载荷的最小未送达身份并释放预占，受限的 NOT_STARTED 是终止的瞬时互动，不进入休眠）；送达/未送达对账与每项冻结的送达后承诺为 `FACT_FINALIZATION`；租约、预占与终态幂等证据清理为 `TECHNICAL_CLEANUP`。已送达履约的承诺继承父记录归属，父级归属缺失时不执行承诺，只允许保存安全失败状态、释放租约并告警。重新发送回复永不允许。
- 只有资源 ID 时采用两段式读取：先读取最小群归属投影，裁决通过后才读取正文或完整载荷。单资源命中受限群时明确返回准入拒绝，不伪装 404；列表只投影获准群记录，不暴露受限记录的正文、标题、数量或存在性。新业务事务按全部关联群裁决：不可分事务任一受限则整体不提交，可分批次逐项目裁决；已越过准入时点的事务可提交，其原子触发器效果随事务完成。
- 任何尚未形成不可逆全局结果的贡献必须持续携带来源群 lineage，直到不可逆 global commit（好感度事务提交、跨群互动事件成功提交、提案 `add_knowledge()` 成功等）；判断依据是效果为何发生、消费哪些群贡献、提交后结果是否仍可分群，而不是数据库恰好有无 `group_id` 列。管理控制面直接维护且无来源群的全局配置、Prompt、判定场景与帮助内容不受群策略限制；由群命令触发的全局修改仍携带来源群并在写入前裁决。
- 不建中央 `group_admission_jobs` 表；每项持久群工作在自己的现有记录、快照或账本中保存关联群、安全进度、deferred revision 与 deferred 时间。领取流程先发现候选并恢复关联群，再 `BUSINESS` 裁决：获准则 CAS 领取并清除 deferred 标记；受限则不取得业务处理租约，只 CAS 保存当前 effective revision 并休眠。休眠工作只在 effective revision 变化、从无有效策略恢复或显式重放触发时重新裁决；显式重放没有 bypass。休眠不增加业务失败、不消耗 retry budget、不触发普通失败告警；若现有 TTL 会让安全进度在休眠中消失，实现必须调整以保留进度。恢复后只唤醒持久群工作，瞬时效果不补做。
- 群归属数据在受限期间休眠：不参与业务读取、写入、召回、索引或后台加工，也不通过普通群目标管理接口暴露。可分群混合数据只消费获准贡献，受限贡献保留休眠；不可分群派生数据（已完成不可逆聚合的既成结果）继续可用，不因来源群后来受限而逆向撤回。旧表缺群号不自动成为不可分群派生数据；尚未形成全局结果又无法恢复归属的数据故障关闭。
- 当前后台路径按封闭分类：对话快照、分块账本、记忆总结、画像/互动加工、忘却与模糊化、提案投票/采纳属于持久群工作或角色记忆业务，受限时保存进度、释放租约并休眠；群总结、公告、未发送回复、表情反应属于瞬时互动，终止且不复活；已送达回复承诺以 `FACT_FINALIZATION` 继续；已提交的全局知识/帮助索引与全局配置、Prompt、判定场景刷新作为系统维护继续；租约、预占、连接释放与已解决履约的终态证据清理以 `TECHNICAL_CLEANUP` 继续；日志保留/轮转、user_ban 自然解封与准入控制面自身继续。策略变化本身不能删除休眠工作、清空快照或消费失败预算。
- `ADMISSION_ATTRIBUTION_FAILED` 是无法可靠归因 work 的运维持有态：不执行、不猜测、不自动重试、不删除、不随策略修订唤醒、不消耗原 retry budget；只有另行鉴权的数据修复补齐归属后才能恢复普通状态。
- 在途计算撤销：已开始的 LLM/工具/下载/平台读取允许结束，下一效果前拒绝时丢弃未发布结果并正常终止，不进入下一轮、不持久化、不在恢复后复活；并发工具逐个在 dispatch 前裁决，已 dispatch 的兄弟效果允许完成，未 dispatch 的不再开始。准入拒绝不包装为 provider 异常、不进入普通失败通知、不消耗 retry。
- 日志、Sentry、管理审计与 Agent Run 收尾是封闭运维诊断行为，不作为角色业务效果：入口已拒绝的事件不创建业务任务或 Agent Run；获准后开始的任务即使中途撤销，已完成调用的诊断仍收尾一次，但业务结果不得发布，也不回流回复、记忆、召回、索引或普通群管理接口。原则是「业务停，证据不停；证据只能走既有专用运维权限」。`.debug` 报告是群触发业务结果，仍受准入约束，不能借诊断名义绕过。

### 可观测性

- 正常策略拒绝（`policy_restricted`）与私聊拒绝（`private_input_rejected`）只做低基数进程级累计计数：无普通日志，无群输出（不回复、不发固定错误文本、不发表情反应），无 SUPERUSER 通知。同一长任务在多个效果准入时点复查被拒绝，代表多个实际受保护的效果接缝，可分别计数；不尝试建立跨 matcher 的「唯一事件」身份。
- `group_attribution_unavailable` 按 `(reason_code, event_family)` 五分钟聚合窗口观测（`event_family` 只允许 `message / notice / request / unknown`）：窗口首次出现立即告警，后续只累计，下一窗口报告上一窗口的 `suppressed_count`；不使用事件类名或任何动态标识作去重键。
- 运行时故障期从首次进入 `degraded` / `failed` 开始，到 `ready` 连续稳定 60 秒后结束：开始时写结构化日志并尝试通知 SUPERUSER；重复刷新失败只累计次数；每持续 30 分钟发送一条安全提醒；`ready` 稳定 60 秒后写一次恢复日志并通知一次；60 秒内再次失败仍属原故障期，防止存储抖动制造告警风暴。
- 原因码与问题码均为封闭白名单。准入结果原因码：`policy_admitted`、`policy_restricted`、`group_attribution_unavailable`、`effective_policy_unavailable`、`fact_finalization_granted`、`technical_cleanup_granted`、`private_input_rejected`；运行时问题码只有 `storage_unavailable`、`stored_policy_invalid`、`snapshot_publish_failed`、`internal_error`。不得拼接异常类型、异常正文、动态 ID、URL、策略内容或消息正文。
- 不引入 Prometheus、OpenTelemetry、全局指标注册表或 `/metrics` 端点；计数、窗口、故障期与待投通知均驻留进程内存，随进程重启清零（status 响应返回 `telemetry.started_at`）。不新增拒绝事件持久化面（不建新表、Redis 明细、独立诊断 JSONL 或 Agent Run）；受限群事件不形成可查询的持久诊断历史。
- 不记录群/用户/消息 ID、正文、异常类型/正文、URL 与 traceback。SUPERUSER 故障卡与归属异常卡只含事件、状态、问题码、revision 标记、LKG 标记、持续秒数、窗口计数与 UTC 时间等安全字段；通知投递不建 outbox，不写 PostgreSQL 或 Redis。

### 破坏性迁移后果

- 升级 operator 必须显式提交一个完整统一策略；禁止从八个旧插件的 `group_whitelist` 做 union、intersection、优先级合并或取任一插件为权威；`user_whitelist` 不属于群策略，不迁移；旧值只用于离线审计，报告只显示资源、数量、合法性与规范化指纹。只有真正执行基线 `0001` fresh marker 的空库自动初始化为 `blacklist + []`，legacy 库 stamp 基线时没有 marker，绝不会误走 fresh 默认。
- 未发布的迁移链（最新正式 tag 为 v1.3.1，`0002` 及以后均未发布）重排为单线：`0010` admission expand、`0011` 回复履约镜像、`0012` admission backfill 作为 **forward-only barrier**、`0013` coordinated contract（单事务内先验证全部 policy/backfill/Redis attestation，再删除旧回复 outbox、完成配置改名、收紧约束并清除 legacy JSONB 名单），原后续迁移顺延；不保留旧 revision ID alias、branch/merge revision 或开发者库兼容探针。
- Cutover 建立严格版本边界：旧 NOT_STARTED 回复全部终止（瞬时互动不跨版本）；既成发送事实（待确认/已送达）只允许 `FACT_FINALIZATION` 对账与冻结承诺完成，永不重发；可归因的持久工作按各 Module 自有 ledger/sidecar 状态休眠；不可可靠归因的旧全局互动工作全部终端隔离（保留原字节语义、从全部活动索引原子摘除、移入运行时永不扫描的命名空间，不猜测、不唤醒、不重放）；公告与提案改用 Module 自有 ledger 与状态。
- barrier 成功提交后只允许 roll-forward：禁止旧应用启动、Alembic downgrade、恢复旧列、回放隔离区或任何兼容 fallback；不提供 rolling deployment。唯一支持的灾难回退，是同时恢复停旧 worker 后、执行 `0010` 前取得的同一 checkpoint 的 PostgreSQL 与项目 Redis 命名空间及旧镜像 digest；禁止只恢复一个存储。
- 物理删除：完整删除浅 `permission_manager` 插件（各业务插件直接拥有 `plugin_enable`）；删除八个 Schema 的旧名单字段、validator、运行时读取、管理元数据/OpenAPI、env/模板与活动文档入口；删除 `komari_search` 的 caller 兼容参数。旧标识只保留在读取 released 迁移输入的 cutover 工具、contract 清理 SQL、物理删除测试与明确历史归档中。
- 完整的十九步 cutover runbook（每步 Action / Expected / Verification / Fallback）由 TSK-214 交付的部署文档承载，不在本 ADR 复制。

## Considered Options

- **扩展 `permission_manager`**：被拒绝——它是无状态的插件开关、用户/群名单与 SUPERUSER 检查器，而新准入需要全局状态、LKG、冷启动与发布语义；旧双名单本身将被删除，两者融合会形成职责不断膨胀的权限杂物箱。
- **拆出纯契约共享包**：被拒绝——准入类型、状态与裁决只有一个 runtime 所有者，当前没有第二个 Adapter 或非 NoneBot 实现需要独立契约包；提前拆包会形成浅类型桶与假想 seam。
- **仅靠 `event_preprocessor`、`run_preprocessor`、gate matcher 或 task cancellation**：被拒绝——event preprocessor 并发运行且无顺序保证，粗门禁无法单独构成效果级即时撤销；`run_preprocessor` 不是全局入口且破坏被拒绝 matcher 原有的传播阻断语义；gate matcher 要求全局优先级纪律且 matcher rule 已发生；shield 下的 matcher 收不到即时取消，长任务不能靠取消接纳新策略修订。
- **万能 guarded effect wrapper / 中央 admission jobs 表**：被拒绝——准入时点必须由理解业务目的并持有归属的效果所有者放置，`admitted_send()`、decorator、context manager 或效果网关会把效果语义重新泄漏回准入模块；中央 jobs 表无法承载各模块的安全进度与归属，并形成第二调度单点。
- **caller 自己解释 mode/list，或低层 Adapter 猜群**：被拒绝——策略解释会散回所有调用方，持有旧快照与跳过刷新不可控；低层 LLM/Embedding/OneBot/Redis/Repository Adapter 没有业务目的与归属，只能猜测。
- **SUPERUSER/管理身份通用 bypass**：被拒绝——准入是最外层必要条件，bypass 会使受限群状态失去意义；管理身份只带来策略修改能力，面向群的管理行为仍按关联群裁决。
- **旧名单 alias/双读/fallback 与 rolling deployment**：被拒绝——八组旧名单互相冲突，任何自动合并都是猜测；双真源与双读延长危险窗口；新旧 worker 并行写同一存储无法维持一致的准入状态，切换必须停旧 worker 并向前跨越 barrier。
- **把 LKG 持久化成第二真源**：被拒绝——进程重启必须从 PostgreSQL 重建合法策略；第二真源会冒充冷启动成功，并与持久真源脑裂。
- **多 worker 下继续把轮询当强一致**：被拒绝——「管理响应前本进程已发布」的保证建立在单 worker 之上；未来放开多 worker 必须重新设计跨 worker 发布确认，不能复用现有轮询并宣称强一致。

## Consequences

- 基础服务层新增 `group_admission` 插件；所有产生群行为的插件必须 `require("group_admission")` 并只 import 顶层暴露面，在每个不可分效果前同步裁决；新增效果必须相应扩展效果矩阵。其他插件的测试 monkeypatch 顶层 `adjudicate`，不新增公开 Fake/Protocol seam。
- 受限群在平台侧完全静默：message / notice / request 被静默拒绝，不回复、不缓存、不形成业务数据；受限期间的新群事件直接忽略，恢复准入后不追溯补录。
- 准入拒绝是正常控制流：不包装为 provider 异常、不进入普通失败通知、不消耗重试；准入拒绝明确呈现为准入拒绝，不伪装成资源不存在。
- 策略只管角色行为，不自动进群、退群或改变群成员关系。控制面始终可达：策略查询/修改、健康查询、对既成事实的受限收尾与不产生新业务效果的技术清理。
- 本 ADR 是设计依据：生产模块、管理 Adapter、可观测性、各效果接缝与破坏性迁移均由后续实现票落地与验收；写作时本决策的生产实现尚未完成。

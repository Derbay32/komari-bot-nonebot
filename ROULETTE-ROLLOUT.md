# 群绑定与俄罗斯轮盘：升级、运维与终验

## 适用范围与授权

- 面向已有 OneBot V11 接入和具名管理凭据的单 worker 部署执行升级。
- 将本说明作为操作交接，不将代码合并、离线测试或旧原型样本视为生产上线证明。
- 仅在取得目标环境的变更授权后执行部署或生产迁移。
- 不随本次代码交付发送运营通知、真实 QQ 消息或自动部署。
- 统一以 `Asia/Shanghai`（UTC+8）记录操作时间；保留 API 时间值自带的时区信息后再换算。

## 1. 准备双协议接入

1. 固定待上线提交 SHA 或镜像 digest；检查它与通过 PR 验收的版本一致。
2. 保留现有 OneBot V11 接入；检查 OneBot 与官 Bot 位于待验证的同一物理群。
3. 配置非空 `QQ_BOTS` 以启用官 Bot 适配器；检查启动日志没有账号配置或驱动能力错误。
4. 配置每个应用的 `QQ_OFFICIAL_BOT_QQ_BY_APP`；检查值为该官 Bot 的数字 QQ 号，而非 OneBot 登录号。
5. 配置满足服务端与客户端能力的 `DRIVER`；检查官 Bot HTTP 接入成功，使用 WebSocket 的账号还需检查 WebSocket 连接。
6. 保持 `MAX_WORKERS=1`、`WEB_CONCURRENCY=1`；检查实际进程数与启动配置一致。
7. 保留现有群准入策略；检查验收群的可靠 OneBot 群号获准，不用全开放策略替代核验。

使用下列配置形状准备环境值；占位值必须由部署者替换，凭据不得提交到仓库或复制进验收报告：

```dotenv
DRIVER=~fastapi+~aiohttp
MAX_WORKERS=1
WEB_CONCURRENCY=1
# 按实际账号环境选择；不把沙箱样本冒充正式环境样本。
QQ_IS_SANDBOX=false
QQ_BOTS=[{"id":"<APP_ID>","token":"<安全注入>","secret":"<安全注入>","use_websocket":false}]
QQ_OFFICIAL_BOT_QQ_BY_APP={"<APP_ID>":"<官Bot数字QQ号>"}
```

- 使用 `QQ_BOTS` 是否为空控制适配器注册；不存在独立的 `QQ_ENABLE_ADAPTER` 开关。
- 使用组合驱动器提供 FastAPI 服务端与 aiohttp HTTP/WebSocket 客户端；单独 `~fastapi` 不提供官 Bot 所需的客户端能力。
- 按账号实际接入方式配置平台回调或 WebSocket 连接；公网回调映射与账号设置不由本次代码合并自动完成。
- 保持仅群 @ 事件的接入范围；不因此开启全量群消息、C2C 业务、频道业务或 interaction callback。
- 修改账号、可信身份或接入设置后重启；不要复用重启前的绑定会话。
- 保持凭据缺失或可信身份非法时失败关闭；不要以昵称、正文相似度或两个平台的消息 ID 相等来补推身份。

参考 [NoneBot 驱动器说明](https://nonebot.dev/docs/advanced/driver) 与 [配置说明](https://nonebot.dev/docs/appendices/config)。

## 2. 按顺序升级并放行业务

1. 让需要操作的现有对局正常结束；检查受权诊断返回 `game_present=false`，不要使用删表或修复接口强行结束对局。
2. 读取 `GET /api/v2/komari-management-config/resources/komari_roulette`；检查当前配置值与生效状态，不把不可观测值当成已生效。
3. 将轮盘业务开关设为关闭；检查资源值为 `false`，并确认状态不再接受新业务。
4. 保存升级前的 PostgreSQL 备份；检查备份清单与恢复方案覆盖绑定关系、对局、结果和胜场。
5. 更新到已验收版本并启动单 worker 服务；检查下面两个启动步骤均成功，任一步失败都应阻止应用启动。
6. 检查绑定快照、群准入及存储恢复状态；检查失败日志，不将数据库读取失败解释为“当前无局”。
7. 将轮盘业务开关设为开启；检查 `GET /api/v2/komari-roulette/status` 的 `runtime_status` 在一次成功恢复后变为 `ready`。
8. 保留该版本、配置变更请求 ID 和状态结果；检查记录中没有账号 secret、Bearer 凭据或修复令牌。

运行容器时，启动脚本依次执行：

```bash
python -m komari_bot.db.orm_bootstrap upgrade head
python -m komari_bot.db.seed_bootstrap
```

执行经授权的本地升级时，可使用相同入口：

```bash
poetry run python -m komari_bot.db.orm_bootstrap upgrade head
poetry run python -m komari_bot.db.seed_bootstrap
poetry run python -m komari_bot.db.orm_bootstrap check
```

- 检查本次迁移链到达 `0021`；检查 `check` 没有新增迁移操作。
- 将初始数据播种限定为场景和 Prompt；不要把它当成管理凭据或正式群关系的创建流程。
- 保持已有 PostgreSQL 动态配置为运行时真源；环境配置只在缺失资源的初始化路径使用，不覆盖已有值。
- 使用受权管理资源修改单个插件；不要设置通用 `PLUGIN_ENABLE=true` 试图只启用一个插件。
- 在升级失败时保持业务关闭并排查错误；不要自动执行 `downgrade`，相关降级会删除业务表。
- 在必须恢复旧版本时使用经过确认的升级前备份方案；不要通过删除结果或胜场让旧代码“看起来能启动”。

## 3. 使用现有具名管理凭据

| 操作 | 权限 | 请求 |
|---|---|---|
| 读取配置 | `config:read` | `GET /api/v2/komari-management-config/resources/komari_roulette` |
| 修改业务开关 | `config:write` | `PATCH /api/v2/komari-management-config/resources/komari_roulette/fields/plugin_enable` |
| 观察运行状态 | `roulette:read` | `GET /api/v2/komari-roulette/status` |
| 校验本群排行榜 | `roulette:read` | `POST /api/v2/komari-roulette/leaderboards/inspect` |
| 按胜利结果重建排行榜 | `roulette:manage` | `POST /api/v2/komari-roulette/leaderboards/rebuild` |
| 诊断群绑定 | `character_binding:read` | `GET /api/v2/character-bindings/repair/diagnose` |
| 预览或确认清除 | `character_binding:read` 与 `character_binding:manage` | `POST /api/v2/character-bindings/repair/preview`、`/confirm` |

- 为管理请求提供 `Authorization: Bearer …`；使用已配置、未撤销且权限匹配的具名凭据。
- 为变更请求提供 `X-Komari-Change-Reason`；使用非空、可打印且不超过 200 字的理由。
- 为重建和修复请求提供 `X-Request-ID`；保存返回的请求标识供审计关联。
- 为通用配置 PATCH 使用 `{"value": true}` 或 `{"value": false}`；不要加入该接口未定义的 `revision` 或 `If-Match`。
- 为排行榜校验和重建使用 `{"app_id":"…","group_openid":"…"}`；不要加入修复 `token` 或臆造的重建版本参数。
- 检查重建结果的 `consistent`、`entry_count`、`total_wins`；不要把重建当成任意加分、清零或修改比赛结果的入口。
- 区分 Bearer 凭据、绑定向导会话码和修复确认令牌；三者不能互换。
- 查看 `/api/docs` 或 `/api/openapi.json` 获取当前精确请求结构；不要使用退役的 v1 管理路由。

## 4. 完成用户自己的首次绑定

1. 由用户在目标群实际 @官 Bot 并发送 `/bind`；检查官 Bot 的首次挑战原生引用了该用户本次消息。
2. 让 OneBot 读取同群的原始消息与官 Bot 挑战引用；检查取证期间没有额外 OneBot 发送。
3. 由用户点击“继续绑定”后手动发送填入的 `/bind`；检查进入姓名输入或旧名选择阶段。
4. 由用户通过公开按钮填写 `/bind name <会话码> <角色名>`；检查此时只显示确认提示，正式名字尚未改变。
5. 由同一用户发送 `/bind confirm <会话码>`；检查成功提示与该应用、该群的当前角色名一致。
6. 由第二名用户重复自身核验；检查复用群关系但不复用第一人的身份或会话。

- 允许未知群在有效策略与取证配置齐备时获得同一会话的一次初始挑战；不要把挑战当成群业务准入或正式绑定成功。
- 在可靠群归属尚未获准时保持静默；重复 `/bind` 不得让未知会话无限续流。
- 保持两端原生引用各自指向其平台原消息；不要比较 QQ 字符串消息 ID 与 OneBot 整数消息 ID 来认定同一消息。
- 保持会话 10 分钟绝对期限；过期或重启后重新发起，不复用旧码或迟到证据。
- 仅把旧全局角色名展示为本人主动迁移候选；用户选择“沿用旧名”会填入 `/bind reuse <会话码>`，仍须最终确认。
- 不批量建立正式群映射或成员关系；不把旧 JSON、旧全局表或昵称当成自动生效的新绑定。
- 使用 QQ 官 Bot `/bind` 作为普通用户入口；原 OneBot `.bind` 系列已退役。
- 保留既有 SUPERUSER `.debug bind set|del|list` 的已确认群内角色名管理；不要将它用于改指身份关系或代替 REST 预览清除。

## 5. 改名、解绑与取消

- 使用 `/bind rename` 打开改名草稿；从公开键盘取得会话码，填写姓名后再确认。
- 使用 `/bind unbind` 打开解绑确认；确认前保留原名，确认后仅清除当前群角色名。
- 使用公开“取消”按钮填入的 `/bind cancel <会话码>` 取消草稿；检查原有绑定与当前局均未改变。
- 保留普通解绑后的已确认群、成员身份关联；不要把普通解绑与运营清除关系混为一谈。
- 保留当前局入席时冻结的编号和名字；改名或解绑不等于退出、弃权或撤销在席资格。
- 拒绝无当前角色名的用户新开局或重新加入；不要从旧全局名字自动回填。
- 由等候局房主使用 `/轮盘 取消` 正常取消；检查不增加胜场，随后仍可正常重新开局。
- 使用稳定玩家编号选择对局目标；不要用第二个 @、昵称、QQ 号或回复关系作替代目标。

## 6. 先预览清除，再独立重绑

1. 使用诊断接口查询明确的 `app_id` 与 `group_openid`；检查目标范围与当前对局状态。
2. 在成员级清除时向预览请求提供 `member_openid`；检查 `scope`、`affected_count` 与将清除的名字仅覆盖目标成员。
3. 在群级清除时明确省略 `member_openid`；检查预览覆盖该群映射及依赖它的全部成员，避免误把群级操作当成员级操作。
4. 使用同一操作者及返回令牌提交 `{"app_id":"…","group_openid":"…","token":"…"}`；检查确认返回的版本与预期清除数量一致。
5. 检查清除后的准确未绑定状态；检查缓存不再返回已删除身份或角色名，不应出现自动重绑或群通知。
6. 由受影响用户稍后独立执行新的 `/bind`；检查新的原生引用核验、姓名确认与新角色名生效。
7. 检查历史比赛和本群胜场保持不变；不要把清除、重绑与历史改写包装成一笔自动操作。

- 在目标群有 `waiting` 或 `active` 对局时拒绝修复；先让对局正常结束，不增设强制终局或删局捷径。
- 保持修复令牌 10 分钟绝对有效期与一次性使用；重启、版本或依赖变化后重新预览。
- 在冲突或失败时保留原关系；不要自动改指另一个身份或修改群准入策略。
- 将受权预览中的身份信息保留在管理界面；不要将完整响应、令牌或身份明文复制到普通日志。

## 7. 观察恢复、期限和清理

| 字段 | 读取含义 |
|---|---|
| `runtime_status` | 区分 `ready`、`disabled`、`failed`，不把存在服务对象视为就绪。 |
| `runtime_reason` | 读取闭集原因码，不自行补造恢复原因。 |
| `latest_scan` | 读取扫描、推进、受限跳过和失败计数；不是“最后运行时间”。 |
| `latest_cleanup` | 读取删除数量与 `more_pending`；不是清理时间戳。 |
| `pending_receipts` | 读取待处理计数；无法观测时不能当成零。 |
| `fault_counts` | 读取固定原因的累计计数，不当成消息正文或个人通知。 |

- 关闭业务开关时拒绝新命令和未开始发送；不撤回已发出的请求，也不回滚已提交事实。
- 保持绝对期限继续流逝；关闭开关不是暂停游戏，受限群也不会获得新的宽限期。
- 在群准入恢复后只处理旧当前玩家一次；为新的当前玩家从补结算时起给足 15 分钟，不按停机时长连续淘汰。
- 每 60 秒按小批次扫描到期局；命令仍按 PostgreSQL 时间惰性判定，不额外宽限 60 秒。
- 每天按部署调度器时区的 04:00 清理；镜像使用 `Asia/Shanghai`，部署者应检查调度器实际时区一致。
- 按 PostgreSQL UTC 时间比较保留边界；收据与履约元数据保留 7 天，非胜利终态 `cancelled/expired/failed` 保留 30 天。
- 长期保留 `completed`、其结果玩家及胜场；不要清理 `waiting/active` 或把历史证据当临时缓存。
- 仅观察 `PENDING_CONFIRMATION`；不要猜测已送达、手工补发、重抽或以新幂等键重做已提交动作。
- 在后台没有合法入站消息 ID 时只做允许的数据库维护；不恢复群消息发送。

## 8. 自动化范围与最终 QQ 样本

记录本次新增集成覆盖：

| 范围 | 可追溯测试文件 |
|---|---|
| 首次原生取证、确认提交、初始化失败清理 | `tests/character_binding/test_tsk281_native_binding_chain_pg.py` |
| 开局/加入/开始、道具/满仓奖励、终局/榜单、取消/改名/解绑、UNKNOWN 重投 | `tests/komari_roulette/test_tsk281_binding_to_game_chain_pg.py` |
| 成员/群 REST 清除后独立重绑、历史不变、窗口补丁隔离 | `tests/character_binding/test_tsk281_repair_then_rebind_pg.py` |
| 实际 `.docs 轮盘` 查询、到期游戏与收据不变、无轮盘业务读取、隔离库所有权 | `tests/komari_roulette/test_tsk281_help_isolation_pg.py` |

- 将真实 PostgreSQL、真实绑定与轮盘装配、实际 handler/存储路径作为组合证据。
- 披露平台传输、受限准入策略存储、封禁查询、调度器与配置注册表获取接缝的测试替身。
- 披露确定性随机与 embedding 端口替身；不声称调用了真实外部模型或随机统计验证。
- 披露 REST 用例装配真实路由及服务但不是完整管理进程；披露帮助用例的准入裁决替身。
- 保留已有各实施票的领域、权限、并发和故障检查；组合测试不代替这些检查。
- 将全仓真实服务测试中的 Redis 验收与本条 PG 游戏链分开报告；轮盘本身没有专用 Redis 依赖。

执行下列人工样本前，先完成全部自动化与 CI 验收，并固定已获授权的实际运行版本：

| 样本 | 操作与检查 |
|---|---|
| 首次群绑定 | 由两名不同用户分别 @官 Bot 发起 `/bind`；检查挑战各自原生引用正确，OneBot 不额外发言，确认前无正式名字。 |
| 手动填充按钮 | 点击姓名、确认和游戏按钮；检查只填入输入框，未手动发送前不执行动作。 |
| 编号显示区域 | 开局并查看转让、锁目标区；检查这些区域显示稳定玩家编号，普通正文不显示编号，排行榜数字仅表示名次。 |
| 回合轮转提及 | 通过正常游戏切换当前玩家；检查提醒对象与实际下一玩家一致，每条最多一名真实提及。 |
| 奖励选择提及 | 通过正常游戏触发满仓奖励选择；检查提醒对象与待选奖励所属玩家一致，每条最多一名真实提及。 |
| 上锁成功提及 | 对合法目标使用锁；检查提醒对象与实际目标一致，不从入站第二个 @ 推断目标。 |
| 单条完整消息 | 查看状态、道具或奖励消息；检查没有拆成多条、截断名单或丢失正文。 |
| 最终终局 | 由非胜者弃权触发终局；检查只有一条完整单段正文，无引用、粗体、分割线、玩家名单或按钮，恰好提及胜者而非弃权者。 |
| 本群排行榜 | 发送 `/轮盘 排行榜`；检查该次胜利只增加一次，展示本群榜单。 |

- 通过正常游戏产生待验状态；不要改库、伪造库存或强制补发来制造人工样本，未出现的必需样本保持待验证。
- 在能区分触发者与提醒对象的样本中分别核对两人；奖励属于当前操作者时按实际归属核对，不照搬原型的演示账号关系。
- 记录实际提交或镜像 digest、账号环境、客户端系统与版本、群内场景和 UTC+8 时间。
- 记录每个样本的结果与必要脱敏截图；不保存 Bearer、修复令牌或完整身份明文。
- 将尚未测试的客户端、系统通知设置、静音/免打扰、沙箱与正式环境差异、网络中断和上游异常引用列为未覆盖条件。
- 不承诺全客户端必然通知或消息永远送达；API 返回成功与离线 sender 测试都不能证明用户端实际展示。
- 保持最终 QQ 记录为待人工状态，直到实际运行版本完成以上检查；旧原型样本不自动转为本次通过。

## 9. 追溯依据

- 对照正式验收矩阵 `TSK-268:6a9eebe01af305fd1c073f0e`，其转正记录为 `6a9eec861af305fd1c073f10`。
- 对照配置与运维决议 `TSK-269:6a9ed8f71af305fd1c073f04`。
- 对照交互最终索引 `TSK-266:6a9ec4831af305fd1c073eac` 与终局人工修订 `6a9ece961af305fd1c073eb4`。
- 保留 PR 的 lock、Ruff、Pyright、无服务全量、真实服务全量及迁移零漂移结果作为可追溯证据。
- 区分发布与部署：仓库只在发布 Release 或推送 beta/rc 标签时构建镜像，合入 `dev` 不会自动部署服务器。

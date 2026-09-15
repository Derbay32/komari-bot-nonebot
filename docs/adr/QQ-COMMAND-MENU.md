# QQ 群指令面板：配置与验收

## 范围

- 配置群输入框的指令面板，不修改消息下方键盘。
- 限定本轮配置为已授权测试应用的一个已确认测试群，不使用全局生效范围。
- 保留手动发送才执行的边界；菜单可见性不代替服务端身份、准入或回合权限。
- 保留绑定向导中的动态会话码，不把确认、取消草稿或提交名字动作静态注册。
- 保留转让编号、道具使用/丢弃及奖励替换的当前交互入口，不在静态菜单预填玩家编号或道具选择。
- 保持菜单配置与 Bot 启停独立，不在 Bot 启动时自动覆盖平台设置。

## 官方依据

截至 2026-09-15（Asia/Shanghai），第一方文档提供以下能力：

| 能力 | 官方来源 |
|---|---|
| 群指令面板的创建、列表、详情、修改和删除 | [API 变更记录](https://bot.q.qq.com/wiki/develop/api-v2/changelog.html) |
| `group` + `specific` 可限定群 OpenID；每面板最多 20 项 | [创建指令面板](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_panels.post.html) |
| 按场景分页读取面板；必须处理 `next_cursor` / `is_end` | [查询列表](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_panels.get.html) |
| 读取关联群、版本和面板内容 | [查询详情](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_panels_panel_id.get.html) |
| 修改会覆盖元素列表和备注，但不改变关联对象 | [修改面板](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_panels_panel_id.put.html) |
| 删除后不再对关联对象生效 | [删除面板](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_panels_panel_id.delete.html) |
| 统一 API 地址与 AccessToken 鉴权 | [接口调用与鉴权](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/api-use.html) |

新版群指令面板直接通过上述 API 创建、更新或删除，不需要额外的控制台提交或菜单提审步骤。
本轮已通过 API 配置并由客户端确认生效，不把旧控制台发布流程作为前置条件。
应用的场景权限与 API 鉴权仍需满足；不据此推断所有应用均已获权限。
仅限 C2C 的全局自定义菜单不是群指令面板，不使用 `/v2/menu` 完成本任务。

## 静态命令清单

使用 [菜单资源](../../resources/qq/command-panel.json) 保存可复用的 API 请求内容模板。
该文件不需要上传给控制台或提审，Bot 启动也不会自动读取它；实际生效配置由 API 写入并回读核对。
该文件不包含应用凭据、群 OpenID、面板 ID 或动态会话码。

| 面板名称 | 对应处理器命令 | 用途 |
|---|---|---|
| `bind` | `/bind` | 开始或继续本群绑定 |
| `bind rename` | `/bind rename` | 本群改名向导 |
| `bind unbind` | `/bind unbind` | 本群解绑向导 |
| `轮盘 开局` | `/轮盘 开局` | 创建等候局 |
| `轮盘 加入` | `/轮盘 加入` | 加入等候局 |
| `轮盘 开始` | `/轮盘 开始` | 局主开始对局 |
| `轮盘 开枪` | `/轮盘 开枪` | 当前玩家射击 |
| `轮盘 结束` | `/轮盘 结束` | 结束当前回合 |
| `轮盘 装填` | `/轮盘 装填` | 补空弹并结束回合 |
| `轮盘 道具` | `/轮盘 道具` | 打开道具面板 |
| `轮盘 排行榜` | `/轮盘 排行榜` | 查询本群胜场 |
| `轮盘 退出` | `/轮盘 退出` | 离开等候局 |
| `轮盘 取消` | `/轮盘 取消` | 局主取消等候局 |
| `轮盘 弃权` | `/轮盘 弃权` | 主动弃权 |

实际回读会去掉提交名称中的前导 `/`，因此配置保存平台返回的规范名称。
`only_admin=false` 在请求中显式提交，实际回读省略该 false 字段；不能因此把原始 JSON 字节不相等判为创建失败后重复创建。
本轮客户端已确认正确填入单个 `/` 并保留群 @ 上下文；该结论来自下文两端真人反馈，不是仅由接口成功推导。
所有入口使用 `type=command`，不使用自动发消息类型、链接或 interaction callback。

## 准备操作环境

1. 确认授权应用及目标测试群，检查群 OpenID 来自同一应用的已确认群绑定；不要把数字 QQ 群号当成 OpenID。
2. 建立仓库外的私有工作目录，检查目录权限为 `0700`、文件权限为 `0600`。
3. 保存当前应用 ID 与 AppSecret 到私有 `auth-request.json`，检查字段为 `appId`、`clientSecret`；不要放进终端历史、提交或工单。
4. 保存目标到私有 `scope.json`，检查形状为 `{"scope":"group","target_type":"specific","group_openids":["<已确认群OpenID>"]}`，且只包含获准目标。
5. 获取调用凭证，检查响应包含非空 `access_token`；不要打印或提交响应。

```bash
# PRIVATE 必须指向上一步建立的私有目录；以下请求均不跟随重定向、不自动重试。
curl --silent --show-error --fail-with-body --connect-timeout 5 --max-time 20 \
  -H 'Content-Type: application/json' \
  --data-binary "@$PRIVATE/auth-request.json" \
  -o "$PRIVATE/auth-response.json" \
  https://api.bot.qq.com/app/getAppAccessToken
```

6. 将凭证写入私有 `auth.curl`，检查文件只有 `header = "Authorization: QQBot <ACCESS_TOKEN>"` 这一项，权限仍为 `0600`。
7. 获取群面板列表，检查所有分页；发现既有配置时先核对归属，不创建重复面板或覆盖其他人的菜单。

```bash
curl --silent --show-error --fail-with-body --connect-timeout 5 --max-time 20 \
  --config "$PRIVATE/auth.curl" \
  -o "$PRIVATE/list.json" \
  'https://api.bot.qq.com/v2/panels?scope=group&limit=50'
```

列表的空结果在本轮返回为 `{"is_end":true}`；有游标时继续读取，不把第一页没有目标当成不存在。
遇到鉴权、IP 白名单、场景权限或内容安全错误时停止，按明确错误处理；不切到全局范围或改用其他账号绕过。

## 创建与回读

1. 合并私有 `scope.json` 与仓库面板配置，检查私有 `create.json` 的顶层为 `scope`、`target_type`、`group_openids` 和 `panel`；`panel` 的值为完整静态配置。
2. 记录创建开始时间与配置摘要，检查没有未核实的先前创建尝试。
3. 创建一次指定群面板，检查 HTTP 成功且响应包含非空 `panel_id`；保存该 ID 到私有部署记录。

```bash
curl --silent --show-error --fail-with-body --connect-timeout 5 --max-time 20 \
  --config "$PRIVATE/auth.curl" -H 'Content-Type: application/json' \
  --data-binary "@$PRIVATE/create.json" \
  -o "$PRIVATE/create-response.json" \
  https://api.bot.qq.com/v2/panels
```

若超时、断线或响应无法确认，先按列表及详情核实；不要自动再次 POST，也不要把不确定结果当成未创建。

4. 回读新面板，检查应用、`group`/`specific` 范围、关联群、14 个名称及说明与预期一致；保留平台规范化差异。

```bash
# PANEL_ID 必须取自已核实的私有部署记录，不从备注猜测。
curl --silent --show-error --fail-with-body --connect-timeout 5 --max-time 20 \
  --config "$PRIVATE/auth.curl" \
  -o "$PRIVATE/detail.json" \
  "https://api.bot.qq.com/v2/panels/$PANEL_ID"
```

5. 执行下节的代表性客户端检查，确认菜单可见与实际输入，不仅查看 API 回执。

## 更新与撤回

1. 停止同一面板的并行修改，重新查询详情，检查面板归属、作用群和版本未被其他人改变。
2. 保存完整更新前快照，检查快照不在仓库中且能还原元素与备注。
3. 生成私有 `update.json`，检查只包含 `{"panel": <完整的新面板配置>}`，不假定局部项会被合并。
4. 向 `/v2/panels/{panel_id}` 发送一次 PUT，回读确认内容；若结果不确定，先核实，不直接重放。

```bash
curl --silent --show-error --fail-with-body --connect-timeout 5 --max-time 20 \
  --config "$PRIVATE/auth.curl" -H 'Content-Type: application/json' \
  -X PUT --data-binary "@$PRIVATE/update.json" \
  -o "$PRIVATE/update-response.json" \
  "https://api.bot.qq.com/v2/panels/$PANEL_ID"
```

更新失败时保留当前状态并核实版本；需要回退时，仅在归属及并发检查通过后，用旧快照的完整 `panel` 执行同样的 PUT 并回读。
官方文档未承诺 `version` 是条件写入令牌，不把传入版本号当成 CAS 保护。

5. 撤回本次新建面板前，重新核实 ID、备注与关联群均属于本次操作；检查不存在他人的后续变更。
6. 对该面板发送 DELETE，检查列表和详情确认已撤回；不要删除其他面板。

```bash
curl --silent --show-error --fail-with-body --connect-timeout 5 --max-time 20 \
  --config "$PRIVATE/auth.curl" -X DELETE \
  -o "$PRIVATE/delete-response.json" \
  "https://api.bot.qq.com/v2/panels/$PANEL_ID"
```

删除结果不确定时只读核实；不要用创建另一个面板补偿未知删除结果。

## 当前验收状态

2026-09-15 18:09（Asia/Shanghai）已在获准测试应用创建一个指定测试群面板。
平台接受 14 项配置，详情确认 `group`/`specific` 与唯一目标群一致，版本为 1。
首次严格 JSON 对比因名称去斜杠和 false 字段省略而不相等；逐项核对后确认名称、说明、类型与规范配置一致，没有重复创建。
测试服务在上述注册过程中保持停止；没有发 QQ 消息或修改游戏数据。

- 确认 macOS QQ `7.0.1-52892` 与 iOS QQ `9.3.60.612` 的菜单显示和填入均获得用户通过反馈。
- 确认代表性中英文复合命令只填入输入框，具有单个 `/` 和正确的 @官 Bot 上下文。
- 记录本轮没有逐个执行全部 14 项，也没有重跑完整对局；显示与填入通过不代替处理器接管验证。
- 记录 18:56:42 测试服务按原配置启动完成，QQ 与 OneBot 均连接，启动观测窗口 ERROR 为 0。
- 确认用户从菜单发送 `/bind` 与 `/轮盘 排行榜` 后反馈“没问题”，代表性处理器接管验收通过。
- 保留样本边界：没有要求逐端逐项执行全部命令，也没有执行改名、解绑或重开对局来重复验证菜单通道。
- 完成本轮指定测试群的菜单验收；其他应用、群与生产环境仍须独立授权配置，不自动推广。

自动化清单检查仅验证静态名称、公共 command 类型及真实轮盘解析器映射，不模拟 QQ 客户端，不代替上述真人反馈。

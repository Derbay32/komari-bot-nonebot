# QQ 按钮协议与权限约定

记录日期：2026-09-14（Asia/Shanghai）。设计已确认；现有实现缺口由 TSK-298 / TSK-299 修复，本记录不表示代码已经修复。

## 设计决定

显式遵守 QQ 官方消息按钮字段表，不依赖客户端对缺失字段或空名单的宽松处理。

- 使用 `permission.type=2` 表达现有绑定、轮盘等公开群命令快捷入口的“所有人可填入命令”。
- 使用 `permission.type=0` 时，必须提供真实 QQ 群成员 OpenID 的非空 `specify_user_ids`；此类型只用于明确的指定用户入口。
- 禁止用 OneBot 数字 QQ 号猜测 QQ 群成员 OpenID。
- 禁止把缺失权限或 `type=0 + []` 当成“所有人”。
- 禁止在指定用户身份缺失时静默改成 `type=2`。
- 保留服务端群准入、身份、封禁、会话所有者、绝对期限、连接代次及业务授权校验；按钮能点不等于操作被授权。
- 保持群命令按钮 `action.type=2`、`enter=false`、`reply=false`，只填入命令，由用户手动发送。
- 禁止按客户端版本省略权限、切换宽松名单或自动重发。

当前普通按钮的完整编码约定：

| 字段 | 项目取值 | 官方要求 |
|---|---|---|
| `render_data.label` | 已确定的按钮文字 | 必填 |
| `render_data.visited_label` | 与该按钮 label 相同 | 必填 |
| `render_data.style` | `0`，灰色线框 | 必填 |
| `action.type` | `2` | 必填 |
| `action.permission.type` | 当前公开群命令入口为 `2` | 必填 |
| `action.data` | 已确定的原命令 | 必填 |
| `action.unsupport_tips` | `当前客户端不支持此按钮。` | 必填 |
| `action.enter` / `action.reply` | 显式 `false` | 官方可选、默认 false；项目显式固定 |

`button.id` 为官方可选字段，不因本次问题增加强制要求。权限类型 `3` 及身份组名单仅限频道，不用于群聊。

验收最终真实 SDK 序列化输出，不能只检查中间业务对象或依靠测试 helper 的默认值。轮盘仍从冻结收据物化，不读当前配置重渲染，不修改旧收据或重发旧消息；若修复涉及冻结格式演进，先明确历史数据处理方案。

## 实测依据

TSK-297 在同一条三按钮卡上对比显式权限，所有按钮都是填入命令、不自动发送。

| 实验按钮 | 配置 | iOS | macOS |
|---|---|---|---|
| A 仅本人 | `type=0` + 触发用户 | 可点击 | 可点击 |
| B 空名单 | `type=0` + `[]` | 仍可点击 | 无法点击，提示“无权限操作” |
| C 所有人 | `type=2` | 可点击 | 可点击 |

结果来自用户真人反馈。基线为原版 QQ 9.33.55.609 / iOS 26.6.2，以及 QQ 7.0.0-52194 / macOS 26.6.2。用户已排除账号不同与过期，并报告更新后结果不变；未提供更新后的具体版本。

PC 拒绝指定用户空名单符合限制预期；iOS 放行表现出客户端权限执行差异。用户据此判断为 QQ 自身 bug。项目不依赖这一宽松行为，也不推断 QQ 内部实现或上游修复时间。

服务日志仅确认测试命令到达，未记录 A/B/C 手动回执命令。点击结论不是服务端回执推断，亦不替代正式业务新卡片的实机验收。

## 现有代码审计

审计基线为 `4cbc7fc`。已搜索生产代码、启动入口与脚本的按钮构造及键盘 JSON 物化路径；排除隔离 worktree 和一次性原型，找到以下两个生产入口。

| 入口 | 实际 SDK 输出 | 缺口与修复票 |
|---|---|---|
| `character_binding/qq_commands.py::_qq_message` | render 只有 label，action 只有 type/data | 缺 permission、visited_label、style、unsupport_tips；TSK-298 |
| `komari_roulette/qq/keyboard.py::keyboard_from_spec` | 显式 permission.type2，enter/reply false | 缺 visited_label、style、unsupport_tips；TSK-299 |

绑定测试 helper 当前只提取 label/data/type，未覆盖权限和完整必填字段。轮盘键盘与送达测试已断言权限 type2，但未覆盖上述三个缺失字段。

轮盘权限本身正确；缺少其他必填字段是独立的协议完整性问题，不宣称已复现相同的 macOS 故障。TSK-299 在实施前还须核查冻结物化契约，不直接改历史记录。

## 来源

- [QQ 官方消息按钮字段表](https://bot.qq.com/wiki/develop/api-v2/server-inter/message/trans/msg-btn.html)：本次核对了必填列和权限类型语义。
- [QQ 群消息自动生成文档](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)：可选标记与手册必填列存在不一致；示例和 SDK Optional 不作为省略必填字段的依据。
- TSK-296：客户端差异调查与项目设计结论。
- TSK-297：一次性真实客户端对照；代码不进入正式分支。
- TSK-298 / TSK-299：绑定与轮盘修复子票；本次只审计建票，不实现修复。

# Issue tracker: Plane

Issues、PRD、spec 与 wayfinder 寻路图全部存放在 **Plane 官方云**项目 **KOMARIBOT**（"komari bot"）。所有操作经 Kilo 已配置的 **Plane MCP 服务器**（`plane_*` 工具族）完成——**不要**用 `gh` / `glab` CLI 操作 ticket；`gh` 仅用于 GitHub PR 与仓库操作。

## 坐标

| 项 | 值 |
|---|---|
| 实例 | Plane 官方云（app.plane.so） |
| Workspace ID | `23ddd34b-c5d6-4a0e-90b9-c9586be8dc97` |
| 项目名 | komari bot |
| 项目 identifier | `KOMARIBOT` |
| 项目 ID（project_id，所有 MCP 调用必填） | `c3c4a0f3-6bc2-4a09-b80d-bda803342f7a` |

Work item 的人读标识是 `KOMARIBOT-<sequence_id>`（如 KOMARIBOT-3）；MCP 调用一律用 UUID。叙述与引用时用**名字**，identifier 随行。

## 常用操作（MCP 工具对照）

- **创建**：`plane_create_work_item`（project_id + name；正文用 `description_html`，或 `description_stripped` 纯文本便捷入口）
- **读取**：`plane_retrieve_work_item`（按 UUID）或 `plane_retrieve_work_item_by_identifier`（按 `KOMARIBOT-N`）；评论用 `plane_list_work_item_comments`
- **列表/筛选**：`plane_list_work_items`（project_id + PQL）；PQL 中 state/label/assignee 等 UUID 字段必须用 UUID——先查下面的参照表，或直接 `plane_list_labels` / `plane_list_states`
- **评论**：`plane_create_work_item_comment`（`comment_html`）
- **打/摘标签**：`plane_manage_work_item_label`（add_label_id / remove_label_id）
- **改状态**：`plane_update_work_item`（state=<状态 UUID>）——Plane 没有 "close" 动作，关闭 = 状态置 Done 或 Cancelled
- **认领**：`plane_update_work_item`（assignees=[<用户 UUID>]）或 `plane_manage_work_item_assignee`；当前用户 UUID 用 `plane_get_me`
- **父子关系**：创建时传 `parent=<父 work item UUID>`，或 `plane_update_work_item`（parent=...）

**语言约定**：work item 标题与正文使用简体中文。

## 状态参照表

| 状态 | UUID | group | 用途 |
|---|---|---|---|
| Backlog | `67f8ee2e-4e8f-43fb-803d-144e3c0be5eb` | backlog | 默认，未评估 |
| Todo | `6636aa7e-5dd4-4cc3-8c3b-e0f7619cf021` | unstarted | 已评估待做 |
| In Progress | `6df18c81-e009-4018-9f17-20b2765e01ba` | started | 进行中（认领后置此） |
| Done | `c467719b-4c84-4de3-9604-e55a99862e35` | completed | 完成（= 关闭） |
| Cancelled | `0e52dead-fa92-4861-a59a-97ac8e9bac58` | cancelled | 取消（= wontfix/误报关闭） |

## 标签参照表

Triage 五标签（词汇见 `docs/agents/triage-labels.md`）：

| 标签 | UUID |
|---|---|
| `needs-triage` | `40bfe7ac-d279-4543-9d8e-157fae44955a` |
| `needs-info` | `a09c39f8-7902-4880-be8b-2bd7d6b0e751` |
| `ready-for-agent` | `c6e76b86-8b4f-4684-97b8-878a1ac05056` |
| `ready-for-human` | `7dd9db41-5c0e-4505-a649-b7287f487c27` |
| `wontfix` | `12a6964c-f279-476e-ba10-f92758514348` |

Wayfinder 五标签：

| 标签 | UUID |
|---|---|
| `wayfinder:map` | `ceacbfdf-ba88-407b-bb85-f9b801b8fd47` |
| `wayfinder:research` | `2da6f427-b6c7-424a-ad2d-ff060033ddef` |
| `wayfinder:grilling` | `670044a8-6c48-42ab-9ed3-71ef5bf2b150` |
| `wayfinder:prototype` | `29a424fa-56e8-4028-ab05-18852b040633` |
| `wayfinder:task` | `5758ced2-be65-4234-81b9-673089868e4d` |

新标签用 `plane_create_label`（project_id + name + color），创建后把 UUID 补进本表。

## Pull requests as a triage surface

**PRs as a request surface: no.** PR 留在 GitHub（`gh pr`），不进 Plane triage 队列。GitHub 外部 issue 若出现，人工搬运进 Plane。

## 当 skill 说「publish to the issue tracker」

按产物类型路由：

- **PRD / spec / bug report / feature request** → `plane_create_work_item`（标题简体中文，正文 description_html）。
- **实施 ticket（/to-tickets 产物）** → 父 spec work item 的 **sub-issue**：创建时传 `parent=<父 UUID>`，标题带两位序号前缀（`01 — <标题>`）保持排序可见，正文按 to-tickets 模板（Parent / What to build / Acceptance criteria / Blocked by），打 `ready-for-agent` 标签，状态 Todo。
- **完成 ticket** → `plane_create_work_item_comment` 记完成说明 + 状态置 Done；父 spec 由人类在全部子 ticket 完成后关闭，agent 不动父项。

## 当 skill 说「fetch the relevant ticket」

- `KOMARIBOT-<n>` → `plane_retrieve_work_item_by_identifier`；UUID → `plane_retrieve_work_item`。
- 列出某 spec 的全部子 ticket：`plane_list_work_items` + PQL（parent 字段为父 UUID）。

## Wayfinding operations

供 `/wayfinder` 使用。**map** 是单个带 `wayfinder:map` 标签的 work item，ticket 是它的 sub-issue。

- **Map**：`plane_create_work_item` + `wayfinder:map` 标签，正文含 Destination / Notes / Decisions so far / Not yet specified / Out of scope 五节。
- **Child ticket**：`plane_create_work_item`（`parent=<map UUID>`，`## Question` 正文），打 `wayfinder:<type>` 标签（research / prototype / grilling / task），状态 Todo。
- **Blocking**：⚠️ Plane 原生 relation（blocked_by）在当前工作区**不可用**（`plane_list_work_item_relation_definitions` 返回 HTTP 402，付费功能）——一律使用**正文声明 fallback**：ticket 正文顶部写 `Blocked by: KOMARIBOT-N, KOMARIBOT-M`。ticket unblocked 当且仅当声明的每个 blocker 状态为 Done/Cancelled。
- **Frontier 查询**：`plane_list_work_items` 取 map 的子项，保留状态不在 completed/cancelled、无 assignee、且正文 `Blocked by` 中无未关闭项的 ticket；map 顺序第一个优先。
- **Claim**：`plane_manage_work_item_assignee`（add_user_id=<driver UUID>）+ 状态置 In Progress——会话的第一次写操作。
- **Resolve**：`plane_create_work_item_comment` 记答案 → 状态置 Done → 把 gist + 链接追加到 map 的 Decisions-so-far（`plane_update_work_item` 改 description_html）。

## GitHub 封存说明

2026-08-08 前的 spec/ticket 历史留在 GitHub Issues（Derbay32/komari-bot-nonebot）原地封存，不删不改；已迁移的条目在两侧互留指针（`external_source=github` + `external_id=<原 issue 号>` 也已记录在 Plane work item 上）。新工作一律从 Plane 开始。

# Issue tracker: GitHub

Issues and PRDs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`
- **语言约定**：本仓库 issue 的标题与正文统一使用简体中文撰写。

Infer the repo from `git remote -v` — `gh` does this automatically when run inside a clone.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

When set to `yes`, PRs run through the same labels and states as issues, using the `gh pr` equivalents:

- **Read a PR**: `gh pr view <number> --comments` and `gh pr diff <number>` for the diff.
- **List external PRs for triage**: `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments` then keep only `authorAssociation` of `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE` (drop `OWNER`/`MEMBER`/`COLLABORATOR`).
- **Comment / label / close**: `gh pr comment`, `gh pr edit --add-label`/`--remove-label`, `gh pr close`.

GitHub shares one number space across issues and PRs, so a bare `#42` may be either — resolve with `gh pr view 42` and fall back to `gh issue view 42`.

## When a skill says "publish to the issue tracker"

按产出类型分流：

- **PRD / spec / 缺陷报告 / 需求** → 创建 GitHub issue。
- **实施 ticket（`/to-tickets` 的产出）** → 发布为 GitHub Projects **草稿项（draft items）**，不进 issue 库。见下一节。

## Ticket 管理：GitHub Projects 草稿项

`/to-tickets` 拆出的实施 ticket 一律以 draft items 管理，避免污染 issue 库。

- **载体**：每个 feature/spec 一个 GitHub Project，标题与 spec 同名（如 `LLM 双请求 API 与流式传输 tickets`），创建后 `gh project link <number> --owner Derbay32 --repo komari-bot-nonebot` 关联仓库。
- **权限**：`gh` token 需具备 `read:project,project` scope；缺失时让操作者执行 `gh auth refresh -s read:project,project`。
- **创建草稿项**：按依赖顺序（blockers 先行）逐个执行
  `gh project item-create <project-number> --owner Derbay32 --title "<NN> — <标题>" --body "<正文>"`。
  标题带两位序号前缀保持顺序可见；正文沿用 to-tickets 模板（Parent / What to build / Acceptance criteria / Blocked by），简体中文。
- **Blocking 边**：draft items 无原生依赖关系，以正文 `## Blocked by` 一节的文字声明为准（列序号 + 标题）；frontier = 所有 blocker 均已完成的 ticket。
- **状态推进**：用 Project 的 Status 字段流转；完成一张就把对应草稿项标记为 Done。
- **升级正式 issue**：仅当某张 ticket 需要被外部协作、挂 PR 或进入 triage 流程时，才在 Project 面板将其 "Convert to issue"；转换后按 issue 惯例补 `ready-for-agent` 标签与原生 blocked-by 依赖。默认不转换。
- **父 spec issue**：发布 ticket 时绝不关闭或修改父 issue。

## When a skill says "fetch the relevant ticket"

- 工单编号（`#<n>`）→ `gh issue view <n> --comments`。
- 实施 ticket → `gh project item-list <project-number> --owner Derbay32 --format json` 找到对应草稿项，再 `gh project item-view` 或面板查看正文。

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single issue with **child** issues as tickets.

- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `gh issue create --label wayfinder:map`.
- **Child ticket**: an issue linked to the map as a GitHub sub-issue (`gh api` on the sub-issues endpoint). Where sub-issues aren't enabled, add the child to a task list in the map body and put `Part of #<map>` at the top of the child body. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: GitHub's **native issue dependencies** — the canonical, UI-visible representation. Add an edge with `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`, where `<blocker-db-id>` is the blocker's numeric **database id** (`gh api repos/<owner>/<repo>/issues/<n> --jq .id`, _not_ the `#number` or `node_id`). GitHub reports `issue_dependencies_summary.blocked_by` (open blockers only — the live gate). Where dependencies aren't available, fall back to a `Blocked by: #<n>, #<n>` line at the top of the child body. A ticket is unblocked when every blocker is closed.
- **Frontier query**: list the map's open children (`gh issue list --state open`, scoped to the map's sub-issues / task list), drop any with an open blocker (`issue_dependencies_summary.blocked_by > 0`, or an open issue in the `Blocked by` line) or an assignee; first in map order wins.
- **Claim**: `gh issue edit <n> --add-assignee @me` — the session's first write.
- **Resolve**: `gh issue comment <n> --body "<answer>"`, then `gh issue close <n>`, then append a context pointer (gist + link) to the map's Decisions-so-far.

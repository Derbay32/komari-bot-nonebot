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
- **实施 ticket（`/to-tickets` 的产出）** → 创建为父 spec issue 的 **sub-issue**（GitHub 原生父子关系）。见下一节。

## Ticket 管理：sub-issue

`/to-tickets` 拆出的实施 ticket 一律作为父 spec issue 的 sub-issue 管理，父子关系与 blocking 边都使用 GitHub 原生能力，UI 直接可见。

- **创建 ticket**：按依赖顺序（blockers 先行）逐个执行
  `gh issue create --title "<NN> — <标题>" --body "<正文>" --label ready-for-agent`。
  标题带两位序号前缀保持顺序可见；正文沿用 to-tickets 模板（Parent / What to build / Acceptance criteria / Blocked by），简体中文。
- **挂载为 sub-issue**：`gh api --method POST repos/<owner>/<repo>/issues/<parent-number>/sub_issues -F sub_issue_id=<child-db-id>`，其中 `<child-db-id>` 是子 issue 的数字 **database id**（`gh api repos/<owner>/<repo>/issues/<n> --jq .id`，不是 `#number` 或 `node_id`）。
- **Blocking 边**：GitHub 原生 issue 依赖——`gh api --method POST repos/<owner>/<repo>/issues/<child-number>/dependencies/blocked_by -F issue_id=<blocker-db-id>`（同为 database id）。正文 `## Blocked by` 一节的文字声明（序号 + 标题）保留为兜底与速读。frontier = 所有 blocker 均已关闭的 ticket。
- **列出子 ticket**：`gh api repos/<owner>/<repo>/issues/<parent-number>/sub_issues`。
- **状态推进**：完成一张 ticket 即 `gh issue close <n> --comment "<完成说明>"`；父 spec issue 在全部子 ticket 完成后才由人决定是否关闭。
- **父 spec issue**：发布 ticket 时绝不关闭或修改父 issue。

## When a skill says "fetch the relevant ticket"

- 工单编号（`#<n>`）→ `gh issue view <n> --comments`。
- 实施 ticket → 同为 issue，直接 `gh issue view <n> --comments`；父 spec issue 的全部子 ticket 用 `gh api repos/<owner>/<repo>/issues/<n>/sub_issues` 列出。

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single issue with **child** issues as tickets.

- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `gh issue create --label wayfinder:map`.
- **Child ticket**: an issue linked to the map as a GitHub sub-issue (`gh api` on the sub-issues endpoint). Where sub-issues aren't enabled, add the child to a task list in the map body and put `Part of #<map>` at the top of the child body. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: GitHub's **native issue dependencies** — the canonical, UI-visible representation. Add an edge with `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`, where `<blocker-db-id>` is the blocker's numeric **database id** (`gh api repos/<owner>/<repo>/issues/<n> --jq .id`, _not_ the `#number` or `node_id`). GitHub reports `issue_dependencies_summary.blocked_by` (open blockers only — the live gate). Where dependencies aren't available, fall back to a `Blocked by: #<n>, #<n>` line at the top of the child body. A ticket is unblocked when every blocker is closed.
- **Frontier query**: list the map's open children (`gh issue list --state open`, scoped to the map's sub-issues / task list), drop any with an open blocker (`issue_dependencies_summary.blocked_by > 0`, or an open issue in the `Blocked by` line) or an assignee; first in map order wins.
- **Claim**: `gh issue edit <n> --add-assignee @me` — the session's first write.
- **Resolve**: `gh issue comment <n> --body "<answer>"`, then `gh issue close <n>`, then append a context pointer (gist + link) to the map's Decisions-so-far.

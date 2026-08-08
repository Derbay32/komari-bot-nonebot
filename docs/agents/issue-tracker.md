# Issue tracker: Plane

Issues, PRDs, specs, and wayfinder maps for this repo live in the **KOMARIBOT** project ("komari bot") on **Plane cloud**. All operations go through the **Plane MCP server** configured in Kilo (the `plane_*` tool family) — do **not** use `gh` / `glab` for ticket management; `gh` is reserved for GitHub PRs and repo operations.

## Coordinates

| Item | Value |
|---|---|
| Instance | Plane cloud (app.plane.so) |
| Workspace ID | `23ddd34b-c5d6-4a0e-90b9-c9586be8dc97` |
| Project name | komari bot |
| Project identifier | `KOMARIBOT` |
| Project ID (`project_id`, required by every MCP call) | `c3c4a0f3-6bc2-4a09-b80d-bda803342f7a` |

A work item's human-readable identity is `KOMARIBOT-<sequence_id>` (e.g. KOMARIBOT-3); MCP calls always take the UUID. In narration, refer to items by **name**, with the identifier riding alongside.

## Common operations (MCP tool mapping)

- **Create**: `plane_create_work_item` (project_id + name; body via `description_html`, or `description_stripped` as a plain-text convenience)
- **Read**: `plane_retrieve_work_item` (by UUID) or `plane_retrieve_work_item_by_identifier` (by `KOMARIBOT-N`); comments via `plane_list_work_item_comments`
- **List / filter**: `plane_list_work_items` (project_id + PQL); UUID fields in PQL (state, label, assignee, parent) require UUIDs — use the reference tables below, or call `plane_list_labels` / `plane_list_states` first
- **Comment**: `plane_create_work_item_comment` (`comment_html`)
- **Add / remove label**: `plane_manage_work_item_label` (add_label_id / remove_label_id)
- **Change state**: `plane_update_work_item` (state=<state UUID>) — Plane has no "close" action; closing means setting state to Done or Cancelled
- **Claim / assign**: `plane_update_work_item` (assignees=[<user UUID>]) or `plane_manage_work_item_assignee`; get the current user's UUID with `plane_get_me`
- **Parent / child**: pass `parent=<parent work item UUID>` at creation, or `plane_update_work_item` (parent=...)

**Language convention**: work item titles and bodies are written in Simplified Chinese (this operations doc itself is English).

## State reference table

| State | UUID | Group | Use |
|---|---|---|---|
| Backlog | `67f8ee2e-4e8f-43fb-803d-144e3c0be5eb` | backlog | Default, unevaluated |
| Todo | `6636aa7e-5dd4-4cc3-8c3b-e0f7619cf021` | unstarted | Evaluated, ready to do |
| In Progress | `6df18c81-e009-4018-9f17-20b2765e01ba` | started | In progress (set on claim) |
| Done | `c467719b-4c84-4de3-9604-e55a99862e35` | completed | Finished (= closed) |
| Cancelled | `0e52dead-fa92-4861-a59a-97ac8e9bac58` | cancelled | Cancelled (= wontfix / invalid close) |

## Label reference table

The five triage labels (vocabulary documented in `docs/agents/triage-labels.md`):

| Label | UUID |
|---|---|
| `needs-triage` | `40bfe7ac-d279-4543-9d8e-157fae44955a` |
| `needs-info` | `a09c39f8-7902-4880-be8b-2bd7d6b0e751` |
| `ready-for-agent` | `c6e76b86-8b4f-4684-97b8-878a1ac05056` |
| `ready-for-human` | `7dd9db41-5c0e-4505-a649-b7287f487c27` |
| `wontfix` | `12a6964c-f279-476e-ba10-f92758514348` |

The five wayfinder labels:

| Label | UUID |
|---|---|
| `wayfinder:map` | `ceacbfdf-ba88-407b-bb85-f9b801b8fd47` |
| `wayfinder:research` | `2da6f427-b6c7-424a-ad2d-ff060033ddef` |
| `wayfinder:grilling` | `670044a8-6c48-42ab-9ed3-71ef5bf2b150` |
| `wayfinder:prototype` | `29a424fa-56e8-4028-ab05-18852b040633` |
| `wayfinder:task` | `5758ced2-be65-4234-81b9-673089868e4d` |

Create new labels with `plane_create_label` (project_id + name + color), then record the UUID in this table.

## Pull requests as a triage surface

**PRs as a request surface: no.** PRs stay on GitHub (`gh pr`) and never enter the Plane triage queue. If an external GitHub issue appears, a human moves it into Plane manually.

## When a skill says "publish to the issue tracker"

Route by artifact type:

- **PRD / spec / bug report / feature request** → `plane_create_work_item` (title in Simplified Chinese, body as description_html).
- **Implementation ticket (output of `/to-tickets`)** → a **sub-issue** of the parent spec work item: pass `parent=<parent UUID>` at creation, prefix the title with a two-digit sequence (`01 — <title>`) to keep ordering visible, follow the to-tickets body template (Parent / What to build / Acceptance criteria / Blocked by), apply the `ready-for-agent` label, state Todo.
- **Completing a ticket** → `plane_create_work_item_comment` with a completion note + state to Done; the parent spec is closed by a human only after all child tickets complete — agents never touch the parent.

## When a skill says "fetch the relevant ticket"

- `KOMARIBOT-<n>` → `plane_retrieve_work_item_by_identifier`; UUID → `plane_retrieve_work_item`.
- List all child tickets of a spec: `plane_list_work_items` + PQL (parent field = parent UUID).

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single work item labelled `wayfinder:map`; its tickets are sub-issues of the map.

- **Map**: `plane_create_work_item` + the `wayfinder:map` label, body holding the five sections: Destination / Notes / Decisions so far / Not yet specified / Out of scope.
- **Child ticket**: `plane_create_work_item` (`parent=<map UUID>`, body with a `## Question` section), labelled `wayfinder:<type>` (research / prototype / grilling / task), state Todo.
- **Blocking**: Plane's native **built-in dependency** — `plane_create_work_item_relation` with `relation_type="blocking"`; calling it with `work_item_id=A` and `work_item_ids=[B]` means A blocks B. This is the canonical, UI-visible representation. Keep the textual `Blocked by: KOMARIBOT-N` line at the top of the ticket body as a quick-reading fallback. A ticket is unblocked when every blocker is Done/Cancelled (read relations with `plane_list_work_item_relations`). Note: `plane_list_work_item_relation_definitions` returns HTTP 402 on this workspace — only **custom relation definitions** are a paid feature; the built-in blocking/blocked_by/start/finish dependencies work. Do not misread the 402 as "blocking is paid".
- **Frontier query**: `plane_list_work_items` for the map's children; keep tickets whose state is not in completed/cancelled groups, with no assignee, and no open blocker; first in map order wins.
- **Claim**: `plane_manage_work_item_assignee` (add_user_id=<driver UUID>) + state to In Progress — the session's first write.
- **Resolve**: `plane_create_work_item_comment` with the answer → state to Done → append a gist + link to the map's Decisions so far (`plane_update_work_item` on description_html).

## GitHub freeze note

Spec/ticket history predating 2026-08-08 stays archived in place on GitHub Issues (Derbay32/komari-bot-nonebot) — untouched. Migrated entries carry pointers on both sides (the Plane work item also records `external_source=github` + `external_id=<original issue number>`). All new work starts on Plane.

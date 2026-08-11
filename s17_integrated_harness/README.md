# s17: Integrated Harness — Many Mechanisms, One Loop

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md)

s01 → ... → s15 → [s16](../s16_mcp_plugin/) → `s17` → [s18](../s18_workflow_runtime/) → s19

> *"Many mechanisms, one loop"* — tools, permissions, memory, tasks, teams, and plugins all hang off the same `while True`.
>
> **Harness layer**: Integration — put the mechanisms used by this example into one runnable system.

---

## Problem

The first 16 chapters add one mechanism at a time so each boundary stays visible. This chapter connects them in one runtime.

A long-running coding agent needs all of these at once:

- tool dispatch and permission boundaries
- hook extension points
- todo planning and task graphs
- skills, memory, and runtime system prompt assembly
- compaction and error recovery
- background tasks and cron scheduling
- teams, protocols, autonomous claiming
- task-bound worktrees
- MCP external tool integration

The hard part is not piling up features. The hard part is seeing where each mechanism belongs around the loop. S17 is the integration checkpoint: the mechanisms retained by this runnable example are placed into one harness. S18 extends it with workflow orchestration; s19 uses a smaller loop to study goal closure on its own.

---

## Solution

![System Architecture](images/system-architecture.en.svg)

S17 does not introduce a new mechanism. It connects the components from the earlier chapters in one integrated harness:

```text
user input
  → UserPromptSubmit hooks
  → cron/background notification injection
  → context compact
  → memory + skills + MCP state assemble the system prompt
  → LLM
  → has tool_use block?
      no  → Stop hooks → return
      yes → PreToolUse hooks + permission
          → TOOL_HANDLERS / MCP handlers / background dispatch
          → PostToolUse hooks
          → tool_result / task_notification back to messages
          → next round
```

The loop keeps the same structure: call the model, check whether the response contains a `tool_use` block, execute tools, and append results to `messages`. The presence of a `tool_use` block decides whether tool execution continues.

---

## Where Each Component Sits

| Position | Component | Role |
|----------|-----------|------|
| Around user input | `UserPromptSubmit` hooks | Log, inject, or audit user input |
| Before LLM | cron queue | Inject scheduled prompts into `messages` |
| Before LLM | background notifications | Inject completed background work as `<task_notification>` |
| Before LLM | compaction pipeline | Budget large outputs, trim history, compact old tool results, summarize when needed |
| Before LLM | memory / skills / MCP state | Assemble the system prompt so the model sees current capabilities and long-term context |
| LLM call | error recovery | Retry 429/529, escalate `max_tokens`, compact on prompt-too-long |
| Before tool execution | `PreToolUse` hooks + permission | Block dangerous commands, out-of-bounds writes, destructive MCP tools |
| Tool dispatch | `assemble_tool_pool` | Assemble built-in tools and dynamic MCP tools |
| During tool execution | background dispatch | Move slow bash work into a daemon thread and return a placeholder result |
| After tool execution | `PostToolUse` hooks | Large-output warnings, logs, post-processing |
| Back to loop | tool_result | One `tool_result` per `tool_use`, then the next model round |
| No tool_use this round / on stop | `Stop` hooks | Stats, cleanup, audit |

---

## What code.py Contains

### Tools and Dispatch

The built-in tool pool contains 24 tools:

```text
bash, read_file, write_file, edit_file, glob
todo_write, task, load_skill, compact
create_task, list_tasks, get_task, claim_task, complete_task
schedule_cron, list_crons, cancel_cron
spawn_teammate, send_message
request_shutdown, request_plan, review_plan
create_worktree
connect_mcp
```

`assemble_tool_pool()` assembles these every round:

```text
BUILTIN_TOOLS + connected MCP tools
BUILTIN_HANDLERS + mcp__server__tool handlers
```

After `connect_mcp("docs")`, the next round exposes tools like `mcp__docs__search`.

### Permissions and Hooks

Permission is not hardcoded into the tool execution line. It is a `PreToolUse` hook:

```python
blocked = trigger_hooks("PreToolUse", block)
if blocked:
    results.append(tool_result(block.id, blocked))
    continue
```

That means permission, logging, and audit logic all attach to the same hook point. Lead tools, one-shot subagent tools, and teammate tools all pass through `PreToolUse`; an allowed call then runs `PostToolUse` after its handler.

The policy does not trust an MCP server's own description as authorization. The host owns a small exact allowlist for known read-only calls; every other MCP tool asks the user. File tools are denied outside `WORKDIR`, and every bash command asks before execution. Only the foreground user turn may open an interactive approval prompt; asynchronous turns fail closed instead of competing with the main CLI for stdin.

### Planning and Tasks

S17 keeps two planning layers:

- `todo_write`: lightweight plan for the current session, kept in memory
- task graph: cross-session, dependency-aware, claimable task files under `.tasks/task_*.json`

The first keeps a single agent from drifting. The second supports team coordination.

They share an intent, not an implementation: `todo_write` replaces one session checklist, while task records have stable IDs and individual lifecycle updates. The separate `task` tool below means "dispatch one isolated subagent"; it is not the Task System.

### Subagents and Teams

S17 has two kinds of delegation:

- `task`: one-shot subagent. It uses an isolated `messages[]`, discards intermediate context, and returns only a final summary.
- `spawn_teammate`: persistent teammate thread. It follows `WORK → result → IDLE` without a fixed tool-round cap; model or dispatch failures emit an `error`, and thread cleanup releases an unfinished assignment back to the task board. It drains its inbox before every model call, so direct messages and shutdown requests cannot wait behind an unbroken tool-use sequence. While idle it waits for `MessageBus` delivery first, then scans ready tasks only after the wait times out and atomically claims at most one.

One-shot subagents solve context isolation. Persistent teammates solve long-running parallel collaboration.

### Memory, Skills, and Prompt

`assemble_system_prompt(context)` assembles each round from:

- identity and tool guidance
- workspace
- skills catalog
- `.memory/MEMORY.md`
- connected MCP servers

Skills only put their catalog into the system prompt. Full content is loaded on demand through `load_skill(name)`.

### Compaction and Recovery

Before the LLM call, S17 runs the compaction pipeline:

```text
tool_result_budget → snip_compact → micro_compact → compact_history
```

The model call is wrapped with recovery:

- 429: exponential backoff retry
- 529: exponential backoff, optionally switch to fallback model after repeated failures
- `max_tokens`: raise max tokens, then request continuation
- prompt too long: reactive compact and retry

### Background and Cron

Slow bash work does not block the main loop:

```text
should_run_background → start_background_task → placeholder tool_result
background done → task_notification → next round injects messages
```

Only bash can enter the background path. A non-zero exit or worker exception produces a `failed` notification instead of a false success. Each shell runs in its own process group, which the runtime stops when the command or Agent process ends through the normal or `SIGTERM` path. That cleanup covers the original group; a process that creates another session can escape it.

The cron scheduler runs as a daemon thread and checks once per second. A durable one-shot job is persisted as `pending_delivery` before entering the queue and remains there until the model call containing its prompt succeeds; a failed call restores it to the queue, and a restart queues it again. Delivery is therefore at-least-once. The CLI watches `cron_queue`, Lead's inbox, and terminal background work; any of them can wake one automatic agent turn.

### Worktree and MCP

The task-scoped worktree behavior inherited from s15 manages working directories:

- a pending, unowned task may remain in the main workspace or be bound by `create_worktree(name, task_id)` to a separate branch and directory
- creation prevalidates the task, name, path, branch, and Git registry; a failed Git command is reconciled against the registry and branch state, and any partial checkout remains unbound and preserved for manual recovery
- an idle teammate atomically claims one ready task; the assignment records both `task_id` and its effective `cwd`
- all teammate file tools use that `cwd`; only the owning teammate can complete the task, and the assignment stays selected until that model turn ends
- removal stays in the host-side `remove_worktree()` helper. The model cannot call it. The user or host first checks task ownership, assignment leases, background work, and Git state; destructive removal requires separate user confirmation

The worktree changes tool default directories. It separates working copies; it is not a sandbox, and process-group cleanup does not contain a process that starts another session. This is why deletion remains host-owned.

MCP owns external capability:

- `connect_mcp(name)` connects a mock server
- `assemble_tool_pool()` assembles MCP tools and rejects normalized name collisions
- tool names use `mcp__server__tool`

---

## Changes from s16

| Component | s16 MCP | s17 Integrated Harness |
|-----------|-----|-----|
| tool pool | built-in + MCP | built-in + MCP, with s01-s15 mechanisms restored |
| permission | outside s16's focus | runs inside `PreToolUse` hook |
| hooks | outside s16's focus | UserPromptSubmit / PreToolUse / PostToolUse / Stop |
| todo | outside s16's focus | `todo_write` + reminder |
| skill | outside s16's focus | catalog in system prompt + `load_skill` |
| compact | outside s16's focus | pre-LLM compaction + `compact` tool + reactive compact |
| error recovery | simple try/except | retry / max_tokens / prompt too long |
| background | background bash + notifications | same lifecycle, with permission hooks in the execution path |
| cron | daemon scheduler + durable jobs | same scheduler inside the integrated event loop |
| multi-agent | inherited from s15 | preserved with atomic task ownership and task-scoped `cwd` |
| worktree | optional task binding | model creates; host reviews and removes |
| MCP | introduced | preserved as part of the integrated tool pool |

---

## Try It

```sh
cd learn-claude-code
python s17_integrated_harness/code.py
```

Try:

1. `Inspect this repository and tell me which Python files matter most.`
2. `Search the connected documentation for agent loop guidance.`
3. `Refactor the authentication module and login page in parallel in separate worktrees. Show me each plan before editing.`
4. `Remind me about the meeting in 3 minutes.`
5. `Install the dependencies in the background while you read README.md.`

Watch for:

- whether each tool call passes through hooks/permission
- whether MCP tools appear on the next round after `connect_mcp`
- whether slow operations return a background placeholder
- whether cron automatically reminds you when the time arrives
- whether teammates submit plans and pause before approval
- whether an idle teammate atomically claims only one ready task
- whether every teammate file tool switches to the claimed task's `cwd`
- whether completion keeps the task `cwd` through the rest of the turn and releases it at IDLE

---

## The End Is the Beginning

From s01 to s17, the code gets more capable, but the core remains unchanged:

```python
while True:
    response = LLM(messages, tools)
    if not has_tool_use(response.content):
        return
    results = execute_tools(response.content)
    messages.append(tool_results)
```

A mature harness gets its complexity from coordination around the model. The model chooses actions; the harness organizes the environment, tools, permissions, memory, teams, and external capabilities.

This is the course's integration checkpoint: many mechanisms, one loop.

Next: [s18 Workflow Runtime](../s18_workflow_runtime/) — when the orchestration shape is fixed, move it out of chat turns and into deterministic, resumable code.

<!-- translation-sync: zh@v8, en@v8, ja@v8 -->

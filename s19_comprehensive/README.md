# s19: Comprehensive Agent — All Mechanisms, One Loop

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md)

s01 → ... → s17 → s18 → `s19` → [s20](../s20_workflow_runtime/) → s21

> *"Many mechanisms, one loop"* — tools, permissions, memory, tasks, teams, and plugins all hang off the same `while True`.
>
> **Harness layer**: Comprehensive — put the mechanisms from s01-s18 into one runnable system.

---

## Problem

The first 18 chapters add one mechanism at a time so each boundary stays visible. This chapter connects them in one runtime.

A long-running coding agent needs all of these at once:

- tool dispatch and permission boundaries
- hook extension points
- todo planning and task graphs
- skills, memory, and runtime system prompt assembly
- compaction and error recovery
- background tasks and cron scheduling
- teams, protocols, autonomous claiming
- worktree isolation
- MCP external tool integration

The hard part is not piling up features. The hard part is seeing where each mechanism belongs around the loop. S19 is the integration checkpoint: every earlier component is placed back into one harness before s20-s21 add orchestration and goal closure around it.

---

## Solution

![System Architecture](images/system-architecture.en.svg)

S19 does not introduce a new mechanism. It connects the components from the earlier chapters in one complete harness:

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

The built-in tool pool contains 26 tools:

```text
bash, read_file, write_file, edit_file, glob
todo_write, task, load_skill, compact
create_task, list_tasks, get_task, claim_task, complete_task
schedule_cron, list_crons, cancel_cron
spawn_teammate, send_message
request_shutdown, request_plan, review_plan
create_worktree, remove_worktree, keep_worktree
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

That means permission, logging, and audit logic all attach to the same hook point. After execution, `PostToolUse` hooks run.

### Planning and Tasks

S19 keeps two planning layers:

- `todo_write`: lightweight plan for the current session, kept in memory
- task graph: cross-session, dependency-aware, claimable task files under `.tasks/task_*.json`

The first keeps a single agent from drifting. The second supports team coordination.

They share an intent, not an implementation: `todo_write` replaces one session checklist, while task records have stable IDs and individual lifecycle updates. The separate `task` tool below means "dispatch one isolated subagent"; it is not the Task System.

### Subagents and Teams

S19 has two kinds of delegation:

- `task`: one-shot subagent. It uses an isolated `messages[]`, discards intermediate context, and returns only a final summary.
- `spawn_teammate`: persistent teammate thread. The runtime delivers `MessageBus` events, and the teammate scans the task board while idle so it can claim work autonomously.

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

Before the LLM call, S19 runs the compaction pipeline:

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

The cron scheduler runs as a daemon thread and checks once per second. The CLI watches `cron_queue`; when a job fires, it injects `[Scheduled] ...` and runs one agent turn automatically.

### Worktree and MCP

Worktree isolation owns directories:

- `create_worktree(name, task_id)` creates an isolated branch and directory
- the task `worktree` field binds a task to that directory
- when a teammate claims a task with a worktree, its bash/read/write tools run in that directory

MCP owns external capability:

- `connect_mcp(name)` connects a mock server
- `assemble_tool_pool()` assembles MCP tools into the tool pool
- tool names use `mcp__server__tool`

---

## Changes from s18

| Component | s18 | s19 |
|-----------|-----|-----|
| tool pool | built-in + MCP | built-in + MCP, with s01-s17 tools restored |
| permission | outside s18's scope | runs inside `PreToolUse` hook |
| hooks | outside s18's scope | UserPromptSubmit / PreToolUse / PostToolUse / Stop |
| todo | outside s18's scope | `todo_write` + reminder |
| skill | outside s18's scope | catalog in system prompt + `load_skill` |
| compact | outside s18's scope | pre-LLM compaction + `compact` tool + reactive compact |
| error recovery | simple try/except | retry / max_tokens / prompt too long |
| background | outside s18's scope | slow-operation thread + task notification |
| cron | outside s18's scope | daemon scheduler + durable jobs |
| multi-agent | kept | kept; teammates use basic tools in isolated directories |
| worktree | kept | kept |
| MCP | new | kept as part of the final tool pool |

---

## Try It

```sh
cd learn-claude-code
python s19_comprehensive/code.py
```

Try:

1. `Inspect this repository and tell me which Python files matter most.`
2. `Search the connected documentation for agent loop guidance.`
3. `Refactor the authentication module and login page in parallel in isolated worktrees. Show me each plan before editing.`
4. `Remind me about the meeting in 3 minutes.`
5. `Install the dependencies in the background while you read README.md.`

Watch for:

- whether each tool call passes through hooks/permission
- whether MCP tools appear on the next round after `connect_mcp`
- whether slow operations return a background placeholder
- whether cron automatically reminds you when the time arrives
- whether teammates submit plans and pause before approval
- whether teammates can claim tasks after plan approval
- whether teammates switch to the bound worktree directory

---

## The End Is the Beginning

From s01 to s19, the code gets more capable, but the core remains unchanged:

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

Next: [s20 Workflow Runtime](../s20_workflow_runtime/) — when the orchestration shape is fixed, move it out of chat turns and into deterministic, resumable code.

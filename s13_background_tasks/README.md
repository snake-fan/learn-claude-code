# s13: Background Tasks — Slow Operations Go to the Background

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md)

s01 → ... → s11 → s12 → `s13` → [s14](../s14_cron_scheduler/) → s15 → ... → s20 → s21

> *"Slow operations go to the background, agent continues processing"* — Background threads run commands, inject notifications when done.
>
> **Harness Layer**: Background — Async execution, doesn't block the main loop.

---

## The Problem

Ever used a washing machine? Throw clothes in, press start, then go do other things — cook, reply to messages, read papers. 30 minutes later the machine beeps: done. You don't stand there waiting for 30 minutes.

The agent's bash tool is the same. `pip install torch` takes 10 minutes, `npm run build` takes 3 minutes. While these commands run, the agent waits for bash to return, unable to use that time to process other tasks.

Reading files is milliseconds, no wait. `git status` returns in under a second, no wait. But `npm install`? Minutes. The agent waits 10 minutes doing nothing, and LLM calls are billed by token — idle time is waste.

---

## The Solution

![Background Tasks Overview](images/background-tasks-overview.en.svg)

This chapter sends slow operations to background threads so the agent can keep running the loop. When a task finishes, its result is injected as a notification.

Sync vs Background:

| | Sync (s12) | Background (s13) |
|---|---|---|
| Slow operations | Agent waits | Background thread executes |
| Agent idle | Yes | No, continues processing |
| Result | Immediate return | Notification injected next turn |
| Decision criteria | — | `run_in_background` param (model explicit request), heuristic fallback |

---

## How It Works

### should_run_background: Explicit Request First, Heuristic Fallback

The model explicitly requests background execution via the bash tool's `run_in_background` parameter. If the model does not specify it, keyword heuristics decide:

```python
def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """Fallback heuristic: commands likely to take > 30s."""
    if tool_name != "bash":
        return False
    cmd = tool_input.get("command", "").lower()
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    return any(kw in cmd for kw in slow_keywords)

def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """Model explicit request takes priority; fallback to heuristic."""
    if tool_input.get("run_in_background"):
        return True
    return is_slow_operation(tool_name, tool_input)
```

### start_background_task: Background Execution and Lifecycle

Wraps the tool call in a worker function, dispatches to a daemon thread. Each background task gets a unique ID, with state tracked in the `background_tasks` dict:

```python
_bg_counter = 0
background_tasks: dict[str, dict] = {}   # bg_id → {tool_use_id, command, status}
background_results: dict[str, str] = {}   # bg_id → output
background_lock = threading.Lock()

def start_background_task(block) -> str:
    """Run tool in a daemon thread. Returns background task ID."""
    global _bg_counter
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"

    def worker():
        result = execute_tool(block)
        with background_lock:
            background_tasks[bg_id]["status"] = "completed"
            background_results[bg_id] = result

    with background_lock:
        background_tasks[bg_id] = {
            "tool_use_id": block.id,
            "command": block.input.get("command", ""),
            "status": "running",
        }
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return bg_id
```

`start_background_task()` returns `bg_id`. `daemon=True` ensures the thread exits with the agent process.

### collect_background_results: Notification Collection

When background tasks complete, results are collected and formatted as `<task_notification>` messages:

```python
def collect_background_results() -> list[str]:
    """Collect completed results as task_notification messages."""
    with background_lock:
        ready_ids = [bid for bid, task in background_tasks.items()
                     if task["status"] == "completed"]
    notifications = []
    for bg_id in ready_ids:
        with background_lock:
            task = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, "")
        notifications.append(
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>completed</status>\n"
            f"  <command>{task['command']}</command>\n"
            f"  <summary>{output[:200]}</summary>\n"
            f"</task_notification>")
    return notifications
```

Notifications don't reuse the original `tool_use_id`. The original tool call was already answered with a placeholder `tool_result`; background completion is an independent event, injected in `task_notification` format. This respects Messages API tool pairing: one `tool_use` gets exactly one `tool_result`.

### Loop Integration

In the agent loop, tool execution splits into two paths. Notifications and results merge into a single user message:

```python
results = []
for block in response.content:
    if block.type != "tool_use":
        continue
    if should_run_background(block.name, block.input):
        bg_id = start_background_task(block)
        results.append({"type": "tool_result",
            "tool_use_id": block.id,
            "content": f"[Background task {bg_id} started] "
                       f"Result will be available when complete."})
    else:
        output = execute_tool(block)
        results.append({"type": "tool_result",
            "tool_use_id": block.id, "content": output})

# Merge notifications and tool results into one user message
user_content = []
bg_notifications = collect_background_results()
if bg_notifications:
    for notif in bg_notifications:
        user_content.append({"type": "text", "text": notif})
user_content.extend(results)
messages.append({"role": "user", "content": user_content})
```

Slow operations get a placeholder tool_result with `bg_id`, so the LLM knows this command is still running and can do other things first. When background completes, the notification is injected as an independent text block alongside the current turn's tool_results in one user message.

### Putting It Together

```
Turn 1:
  LLM → bash "npm install" (run_in_background=true)
  → start_background_task → bg_0001
  → tool_result: "[Background task bg_0001 started]..."
  → LLM: "OK, I'll check later. Let me also read the config."

Turn 2:
  LLM → read_file "package.json" (fast, sync)
  → tool_result: file content
  → collect: bg_0001 done! inject <task_notification>
  → LLM sees: config file + install notification in one message
```

The agent didn't wait — while npm install ran in the background, it read the config file.

---

## Changes from s12

| Component | Before (s12) | After (s13) |
|-----------|-------------|-------------|
| Execution model | All synchronous | Slow ops to background thread + notification injection |
| bash schema | `command` | `command` + `run_in_background` |
| New functions | — | `should_run_background`, `is_slow_operation`, `start_background_task`, `collect_background_results` |
| New types | — | `background_tasks: dict`, `background_results: dict`, `background_lock: Lock` |
| Notification format | — | `<task_notification>` (doesn't reuse tool_use_id) |
| Loop behavior | Tools execute serially | Slow ops async, fast ops sync, notifications collected each turn |
| Tools | 8 (s12) | 8 (unchanged, execution strategy changed) |

---

## Try It

```sh
cd learn-claude-code
python s13_background_tasks/code.py
```

Try these prompts:

1. `Run pip list in the background and find all Python files in this directory`
2. `Run npm install (use run_in_background) and while waiting, read package.json`
3. `Create a task to setup the project, then run pip list in the background`

What to observe: Are slow operations dispatched to background? Is a `bg_id` returned? Are background notifications injected in `<task_notification>` format?

---

## What's Next

Background tasks solved "slow operations don't block." But what if you want to do something on a schedule? Like "run tests every morning at 9am" or "check server status every 5 minutes."

s14 Cron Scheduler → Give the agent an alarm clock.


<!-- translation-sync: zh@v1, en@v1, ja@v1 -->

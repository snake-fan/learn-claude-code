# s15: Agent Teams — Runtime and Coordination Protocols

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md)

s01 → ... → s13 → s14 → `s15` → [s16](../s16_autonomous_agents/) → s17 → s18 → s19 → s20 → s21

> *"When one agent cannot hold the whole job, let teammates divide the work."* — Persistent teammates, message delivery, and coordination protocols.
>
> **Harness layer**: Team — how multiple agents work in parallel without losing control.

---

## The Problem

Suppose we ask an agent to refactor an entire backend. The work may cover configuration loading, authentication, and tests. One agent can process those areas sequentially, but it takes longer and earlier details gradually leave its context.

This is a good candidate for parallel work, yet users normally describe the goal rather than design the team:

```text
Refactor this sample backend. Clean up configuration loading,
authentication, and tests, preserve the existing interfaces,
and make sure the tests pass.
```

The harness therefore has to solve four connected problems:

1. Who decides that parallel work is useful, and who confirms the extra agents?
2. How does each teammate keep its identity and context across assignments?
3. How do results return to Lead automatically, without asking the model to poll an inbox?
4. How do shutdown and plan approval become traceable, enforceable protocols?

---

## The Solution

![Agent Teams Overview](images/agent-teams-overview.en.svg)

s15 adds a Lead-managed team runtime around the single-agent harness:

- **Lead** owns the user conversation, proposes a division of work, and waits for confirmation.
- **Teammates** run independent agent loops in background threads and become idle after an assignment.
- **MessageBus** carries ordinary messages, results, and control events through file-backed mailboxes.
- **Runtime delivery** consumes Lead's mailbox and injects team events into the next turn.
- **Coordination protocols** use `type`, `request_id`, and state transitions for shutdown and plan approval.
- **A plan gate** blocks teammate `bash` and `write_file` calls until a required plan is approved.

The model understands tasks and chooses a useful division of work. Code owns delivery, lifecycle, and protocol constraints.

---

## How It Works

### 1. Lead proposes a team and waits for confirmation

Starting teammates changes cost, concurrency, and the set of actors that may edit the workspace. That boundary should not be hidden inside an ordinary tool call. Lead's system prompt says:

```python
"When parallel work would help, first propose a small team with clear "
"responsibilities and wait for the user's confirmation. Do not call "
"spawn_teammate before the user confirms."
```

For the first request, Lead only proposes a split:

```text
I suggest three parallel areas:
- config: clean up configuration loading
- auth: refactor authentication
- tests: add regression coverage

I will start the teammates after you confirm.
```

After the user says "Go ahead," Lead can call `spawn_teammate`. The user states the goal, Lead designs the team, and the user confirms the execution boundary.

### 2. Every teammate owns an independent loop

An s06 subagent is a one-shot call. A teammate is a persistent execution unit:

| | s06 Subagent | s15 Teammate |
|---|---|---|
| Lifecycle | Ends after one call | `WORK → IDLE → WORK` until shutdown |
| Context | Exists for one task | Persists across assignments |
| Communication | Returns one result | Receives messages and emits events |
| Coordination | One-way delegation | Two-way collaboration with Lead |

`spawn_teammate_thread()` gives each teammate its own system prompt, messages, and tools, then runs its loop in a daemon thread. Lead can keep coordinating while teammates work.

### 3. MessageBus keeps communication outside model context

Lead and teammates cannot share one messages array. Otherwise one teammate's tool results would leak into another teammate's reasoning. `MessageBus` gives each agent a `.mailboxes/<name>.jsonl` inbox:

```python
class MessageBus:
    def send(self, from_agent, to_agent, content,
             msg_type="message", metadata=None):
        msg = {
            "from": from_agent,
            "to": to_agent,
            "content": content,
            "type": msg_type,
            "metadata": metadata or {},
        }
        with self._changed:
            append_jsonl(self._path(to_agent), msg)
            self._changed.notify_all()

    def wait_for_messages(self, agent):
        with self._changed:
            while not self.peek(agent):
                self._changed.wait()
            return self._read_unlocked(agent)
```

A lock protects mailbox files from concurrent teammate access. A `Condition` lets idle teammates sleep until an event arrives instead of polling continuously.

### 4. The runtime delivers inbox events automatically

`read_inbox()` consumes messages by reading and deleting the mailbox file, so Lead keeps a single consumer, `consume_lead_inbox()`:

```python
def consume_lead_inbox():
    messages = BUS.read_inbox("lead")
    for message in messages:
        if message["type"].endswith("_response"):
            match_response(...)
    return messages
```

An event thread beside the main loop wakes Lead when a new message arrives:

```text
MessageBus → consume_lead_inbox
           → update protocol state
           → inject [Team events] into history
           → start another Lead turn
```

`check_inbox` is not a model tool. Message arrival belongs to the runtime; the model only handles events that have already been delivered into its context.

### 5. Result and idle are separate events

When a teammate finishes one assignment, the runtime sends two events in order:

```text
result:            "Authentication refactored; related tests pass."
idle_notification: "Waiting for more work."
```

`result` answers "What did this assignment produce?" `idle_notification` answers "Can this teammate accept more work?" A single vague "done" cannot represent both facts.

An idle teammate does not exit. An ordinary message returns it to WORK; a `shutdown_request` starts a graceful shutdown handshake.

### 6. Control messages use types and request IDs

Free-form text is fine for ordinary collaboration, but shutdown and approval should not depend on guessing intent. They use structured messages:

![Team Protocols](images/team-protocols-overview.en.svg)

```python
@dataclass
class ProtocolState:
    request_id: str
    type: str
    sender: str
    target: str
    status: str
    payload: str


pending_requests: dict[str, ProtocolState] = {}
```

The shutdown path is:

```text
Lead creates a pending shutdown request
  → shutdown_request(request_id) enters the teammate inbox
  → the teammate finishes its current step
  → shutdown_response(request_id) returns to Lead
  → request_id locates the original request
  → pending becomes approved and the teammate loop exits
```

The ID correlates one reply with one request, the type prevents a mismatched reply from changing state, and the status prevents duplicate responses from being applied twice.

### 7. Plan approval constrains execution

The plan protocol runs in the opposite direction:

```text
Lead → plan_request
teammate → plan_approval_request(request_id, plan)
Lead → plan_approval_response(request_id, approve, feedback)
```

Merely telling a teammate to wait is not a reliable gate, so tool dispatch checks the plan state:

```python
def _run_teammate_tool(name, block, handlers):
    gate = plan_gates.get(name, "not_required")
    if block.name in {"bash", "write_file"} and gate not in {
        "not_required", "approved"
    }:
        return f"Blocked: plan status is {gate}."
    return handlers[block.name](**block.input)
```

While the state is `required`, `pending`, or `rejected`, the teammate can read files and submit or revise a plan, but it cannot run Shell commands or write files. The tools are released only after an approval response changes the state to `approved`.

---

## One Complete Run

```text
s15 >> Refactor this sample backend. Clean up configuration loading,
       authentication, and tests, preserve existing interfaces,
       and make sure the tests pass.

Lead: I suggest config, auth, and tests as three parallel areas.
      Shall I start the team?

s15 >> Go ahead.

[teammate] config spawned
[teammate] auth spawned
[teammate] tests spawned
[bus] auth → lead (result) ...
[bus] auth → lead (idle_notification) ...
[wake: 2 team events → new turn]
Lead: I received the authentication result and will coordinate the rest.
```

The terminal exposes the user request, Lead's split, teammate startup, messages, results, idle transitions, and shutdown events. The user does not have to name a Lead or ask it to check an inbox.

---

## What Changed from s14

| Component | s14 | s15 |
|---|---|---|
| Agents | One agent | One Lead plus persistent teammates |
| User flow | Execute the request | Propose a team, then confirm startup |
| Communication | None | File mailboxes plus automatic delivery |
| Lifecycle | One loop | Teammate `WORK / IDLE / shutdown` |
| Reporting | Current agent output | Separate `result` and `idle_notification` |
| Control | None | Shutdown and plan approval protocols |
| Enforcement | No team constraint | Required plans gate mutating tools |

---

## Try It

```sh
cd learn-claude-code
python s15_agent_teams/code.py
```

Start with an ordinary request:

```text
Refactor this sample backend. Clean up configuration loading,
authentication, and tests, preserve the existing interfaces,
and make sure the tests pass.
```

After Lead proposes the team, reply:

```text
Go ahead.
```

Watch for `spawned`, `result`, `idle_notification`, `plan_approval_*`, and `shutdown_*` events, along with mailbox files appearing and being consumed under `.mailboxes/`.

---

## Next

In s15, Lead still assigns each teammate explicitly. The next lesson gives idle teammates access to the shared task board so they can discover and claim ready work themselves.

Next: [s16 Autonomous Agents](../s16_autonomous_agents/).

<!-- translation-sync: zh@v2, en@v2, ja@v2 -->

# s18: Workflow Runtime — The Model Decides Each Step; a Script Decides the Orchestration

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md)

s01 → ... → s16 → [s17](../s17_integrated_harness/) → `s18` → [s19](../s19_goal_loop/)

> *"One tool_use runs an entire orchestration"* — The `Workflow` tool starts a deterministic, recoverable script runtime that coordinates many agent calls.
>
> **Harness layer**: Orchestration — a deterministic multi-agent script runtime above the single-agent loop.

---

From s01 through s17, our loop has always been model-driven and step-by-step: the model chooses one tool each round, its result enters `messages[]`, and another round begins. That is ideal for open-ended tasks because the model can inspect the current context and decide the next step on the spot.

Some jobs, however, require deterministic command of a group of agents. Consider reviewing a large change: inspect ten dimensions in parallel → send each finding to a separate agent for adversarial verification → combine and deduplicate the results → sort by severity. The shape is fixed, and you really need three properties:

- **Parallelism**, rather than waiting for one item at a time;
- **Determinism**, so the same input produces the same result structure;
- **Recoverability**, so an interruption does not rerun work that is already complete.

Making the model drive this process one round at a time in the main loop is slow and nondeterministic, and an interruption starts everything over. At that point, you do not need "one more conversation turn." You need to encode the orchestration directly as code.

## Put the Plan in Code, Not in a Sequence of Chat Turns

Add a `Workflow` tool to the harness tool pool. The host registers trusted scripts built from `agent()`, `parallel()`, `pipeline()`, and `phase()`. The model supplies only a saved workflow name, arguments, and an optional run ID to resume; it does not send executable code or metadata.

The main loop sees only one `tool_use`. As the script runs, the runtime emits lifecycle and progress events and records every step in a journal on disk. When the script finishes, the call returns the launch envelope, result, and task state. Intermediate script results live in variables instead of taking space in conversation history. When restarted with `resume_from_run_id`, unchanged `agent()` calls hit the journal cache and reuse previous results, resuming from the checkpoint.

![Workflow Runtime Overview](images/workflow-runtime-overview.svg)

```python
SAMPLE_META = {"name": "review-changes", "description": "Review code changes", "phases": ["Review", "Verify"]}

async def sample_workflow(ctx, args):
    ctx.phase("Review")
    results = await ctx.pipeline(DIMENSIONS, audit, verify)   # Each dimension independently runs audit → verify
    confirmed = [f for r in results if r for f in r["confirmed"]]
    ctx.log(f"Confirmed {len(confirmed)} real issues")
    return {"confirmed": confirmed}
```

## The Workflow Tool: One Call, One Complete Run

`Workflow` is added to the s17 host's existing tool pool. The user can request a saved workflow, or the model can select it when a task matches a known orchestration. The adapter resolves the name through the host-owned `WORKFLOWS` registry, then passes its trusted metadata and function to the runtime. The other s17 tools remain available in the same loop.

The model-facing schema accepts `name`, `args`, and `resume_from_run_id`. Unknown names and malformed arguments become an error tool result instead of ending the host loop. The runtime then validates the registered metadata, checks permissions, registers a local workflow task, and emits `async_launched` before running the script. Progress events follow, then the final `task_notification`; the call returns JSON-safe launch information, result, and task state.

```python
WORKFLOW_TOOL = {
    "name": "Workflow",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "args": {"type": "object"},
            "resume_from_run_id": {"type": "string"},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
}

async def run_workflow(name, args=None, resume_from_run_id=None):
    meta, script_fn = WORKFLOWS[name]
    out = await WorkflowTool().call(
        meta, script_fn,
        args=args,
        resume_from_run_id=resume_from_run_id,
    )
    return {"launched": out["launched"], "result": out["result"],
            "task": serialize_task(out["task"])}
```

## Workflow Metadata: Validate Before Launch

Each saved workflow registers trusted metadata with `name`, `description`, and optional `phases`. The runtime validates it before executing workflow code. `name` and `description` identify the task in the UI, while `phases` names groups in the progress display. These fields belong to the host registry, not to model input.

Invalid registration raises `WorkflowInputError` before launch. This is the same idea as validating cron expressions in s14: do not wait until execution to discover a bad saved workflow.

Because the runtime uses `meta.name` in local artifact filenames, it also requires a 1-64 character safe slug containing letters, numbers, `.`, `_`, or `-`.

```python
def validate_meta(meta):
    if not isinstance(meta, dict):
        raise WorkflowInputError("meta must be an object literal")
    if not meta.get("name") or not meta.get("description"):
        raise WorkflowInputError("meta requires name and description")
    if not isinstance(meta["name"], str) or not WORKFLOW_NAME_RE.fullmatch(meta["name"]):
        raise WorkflowInputError("meta.name must be a safe 1-64 character slug")
    if "phases" in meta and (
        not isinstance(meta["phases"], list)
        or not all(isinstance(p, str) and p for p in meta["phases"])
    ):
        raise WorkflowInputError("meta.phases must contain non-empty strings")
    return meta
```

## Orchestration Primitives: A Small Set Is Enough for Every Flow

A script receives an `ExecutionState` exposing a small set of orchestration primitives. It does not read files or run shell commands directly. A production integration would put a real agent runner behind `agent()` and keep that runner's tool permissions. This chapter uses `MockAgentRunner` so journal and resume behavior are repeatable; its review findings are fixtures, not a real code audit.

| Primitive | Purpose |
|------|------|
| `agent(prompt, {schema, label, phase})` | Dispatch one subagent |
| `parallel(thunks)` | **Barrier**: run every task concurrently and wait until all results return |
| `pipeline(items, *stages)` | Run each item through stages **without a barrier**; finished items proceed immediately |
| `phase(title)` | Mark the current progress phase and update the progress display |
| `log(message)` | Emit a progress log line |
| `workflow(name, args)` | Run a nested sub-workflow, one level only |

`pipeline` should be the default. Each item independently crosses every stage. Item A may reach stage three while item B is still in stage one. Use the `parallel` barrier only when the next stage truly requires every result from the previous stage. A barrier waits for the slowest task, so do not add one without need.

```python
async def pipeline(self, items, *stages):
    async def run_item(item, idx):
        value = item
        for stage in stages:                       # Each item independently completes every stage
            value = await stage(value, item, idx)
        return value
    return await asyncio.gather(*[run_item(it, i) for i, it in enumerate(items)])
```

## Structured Output: Do Not Let Subagents Return Essays

`agent({schema})` requires a subagent to return a JSON object matching the schema, internally through one structured-output call. The runtime validates the result and retries once if it does not match. Downstream code receives a regular object instead of a long essay that must be parsed again.

s05 warned that tool arguments cannot be trusted completely. This is the same lesson in reverse: subagent output cannot be trusted completely either. Validate at the orchestration boundary, give one retry, and keep uncertainty out of the rest of the flow.

```python
result = self.runner.run(prompt, schema, label)
if schema is not None:
    ok, err = SimpleJsonSchema(schema).validate(result)
    if not ok:                                       # Retry once with a reminder, then fail
        result = self.runner.run(prompt + "\n\nReturn valid JSON.", schema, label)
        ok, err = SimpleJsonSchema(schema).validate(result)
        if not ok:
            raise WorkflowInputError(f"agent({{schema}}) returned invalid output: {err}")
```

## Task State and Progress Events

`LocalWorkflowTask` maintains status and token usage and emits an SDK-style event stream: `task_started` → a sequence of `task_progress` events containing phase changes, subagent starts, and log batches → one final `task_notification` reporting completion or failure, plus the output file and agent and token counts.

The demo prints these events in order and returns the task state after the final notification.

```python
class LocalWorkflowTask:
    def progress_event(self, ptype, **data):         # Phase/subagent/log
        self.progress.append({"type": ptype, **data})
        print(f"  progress   {ptype} ...")
```

## Storage: Snapshot + Journal for Resuming after Interruptions

The runtime stores each run under `s18_workflow_runtime/.runtime/`: a `<runId>.json` snapshot, `<runId>.output.json` output, `<runId>.journal.jsonl` journal, and `<runId>.lock` coordination file. Every fresh run reserves a new `runId` with exclusive file creation before opening its journal. The run lock stays held through execution and final persistence, so another process cannot resume the same run at the same time. Its snapshot records the workflow name, arguments, and task state; resume validates the saved snapshot and journal before changing either successful artifact.

The journal is the core of checkpointed resume. It records every `agent()` result one line at a time:

```python
class WorkflowJournal:
    def record(self, key, value):
        self._f.write(json.dumps({"key": key, "value": value}) + "\n")
        self._f.flush()
        self.cache[key] = value
```

## Resume: Continue by runId and Reuse Everything Unchanged

Calling the workflow again with `resume_from_run_id` reruns the script, but every `agent()` computes a deterministic semantic key. If that key is present in the journal, it returns the cached result without executing again. Every unchanged call hits the cache; only a changed call and the downstream steps that depend on it actually rerun.

The key detail is that keys cannot depend on concurrency order. Agents in `parallel` and `pipeline` finish in nondeterministic order. If "the nth completion" became the key, cache entries would map to the wrong calls on the next run. A key therefore uses a stable hash of call content, including type, label, prompt, and schema, rather than a shared counter:

```python
def key(self, kind, label, prompt, schema):
    basis = f"{kind}|{label}|{prompt}|{json.dumps(schema, sort_keys=True)}"
    return f"{kind}-{_stable_hash(basis) % 10**10:010d}"

# Inside agent():
cached = self.journal.cached(key)
if cached is not MISS:
    self.task.progress_event("workflow_agent", label=label, status="cached")
    return cached
```

## Determinism: Reproducibility Makes Resume Meaningful

Resume works only if the workflow is reproducible. Stable hashes make the same workflow plus the same arguments produce the same journal keys. This chapter's deterministic runner also makes the sample result repeatable. A real runner may return different content, but it must keep semantic call keys stable and avoid uncontrolled clocks, randomness, or filesystem state in those keys.

## See It Run

The sample `review-changes` workflow uses `pipeline` to send each review dimension independently through audit → verify. The deterministic runner produces structured fixture findings during audit, then fixture verdicts during verification. This keeps the example focused on pipeline, validation, journal, and resume behavior rather than the quality of a particular model's review.

```python
async def sample_workflow(ctx, args):
    ctx.phase("Review")

    async def audit(_v, dimension, _i):
        out = await ctx.agent(f"Inspect the changed code for {dimension} issues",
                              schema=FINDINGS_SCHEMA, label=f"audit:{dimension}", phase="Review")
        return {"dimension": dimension, "findings": out["findings"]}

    async def verify(audited, dimension, _i):
        ctx.phase("Verify")
        verdicts = await ctx.parallel([                       # Verify every finding independently
            (lambda f=f: ctx.agent(f"Adversarially verify whether this issue is real: {f['title']}",
                                   schema=VERDICT_SCHEMA, label=f"verify:{dimension}:{f['title']}"))
            for f in audited["findings"]])
        return {"dimension": dimension,
                "confirmed": [f for f, v in zip(audited["findings"], verdicts) if v and v["isReal"]]}

    results = await ctx.pipeline(DIMENSIONS, audit, verify)
    ...
```

## Changes from s17

| | s17 Integrated Harness | s18 Workflow Runtime |
|--|-----------|---------------------|
| Loop | One model-driven loop | Main loop unchanged; deterministic orchestration added above it |
| Who decides the next step | Model decides each round | Script declares the orchestration in advance |
| Multiple agents | One-shot s06 subagents | Scripted, resumable calls through an agent-runner boundary |
| New mechanisms | — | Script primitives, host registry and tool adapter, task lifecycle, progress events, journal/resume, structured output |

s18 does not replace the main loop. It exposes `Workflow` at the tool layer and starts a local workflow runtime behind it: one saved script coordinates N calls through an agent-runner boundary. An s06 subagent is dispatched once at the model's discretion; s18 turns the orchestration into resumable host code.

## Try It

```bash
python s18_workflow_runtime/code.py          # Real API: the model can choose Workflow or any s17 tool
python s18_workflow_runtime/code.py demo     # Deterministic review-changes fixture and event stream
python s18_workflow_runtime/code.py resume   # Resume by the last runId; every agent() hits the journal cache
```

In the default command, ask the model to run the saved `review-changes` workflow; the tool call travels through the same loop and dispatcher as the inherited s17 tools. The `demo` command runs the deterministic fixture directly so lifecycle and resume behavior are repeatable. It reports 11 runner calls and six fixture findings. A resumed run reports `agents=0 tokens=0` because every call hits the cache.

## Next

Orchestration adds a layer above agent capabilities: the main loop handles individual operations, while a saved script manages a fixed flow. The sample keeps the agent-runner boundary deterministic; replacing it with a real runner changes the work performed, not the workflow lifecycle, journal, or resume contract.

Next: [s19 Goal Loop](../s19_goal_loop/) — Orchestration fans work out across agents. The next chapter uses a focused loop to pull control back toward a goal: unmet goals continue, while achievement or a safety exit returns control to the user.

<!-- translation-sync: zh@v6, en@v6, ja@v6 -->

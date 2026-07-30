# s08: Context Compact — Context Will Fill Up, Have a Way to Make Room

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md)

s01 → s02 → s03 → s04 → s05 → s06 → s07 → `s08` → [s09](../s09_memory/) → s10 → ... → s20 → s21
> *"Context will fill up — have a way to make room"* — Four-layer compression pipeline: cheap first, expensive last.
>
> **Harness Layer**: Compression — clean memory, unlimited sessions.

---

## The Problem

The agent is running along, then freezes.

It has bash, read, write — all the capabilities it needs. But it read a 1000-line file (~4000 tokens), then read 30 more files, ran 20 commands. Every command's output, every file's contents, all pile up in the `messages` list.

The context window is finite. Once full, the API outright rejects the call: `prompt_too_long`.

Without compression, an agent simply cannot work on large projects.

---

## The Solution

![Compact Overview](images/compact-overview.en.svg)

The hook structure, skill loading, and sub-Agent from s07 are preserved, with some tools omitted to focus on compaction. The core change: insert three pre-processors (0 API calls) before each LLM call, trigger an LLM summary (1 API call) when tokens still exceed the threshold, and emergency-trim if the API throws an error.

Core design: cheap first, expensive last.

> **Boundary with s09:** s08 manages the current session's finite context and may lose detail while compressing it. s09 adds a separate durable store for selected information that must survive compaction and future sessions. They solve different failure modes, so they remain separate lessons.

---

## How It Works

![Four-layer compression pipeline](images/compaction-layers.en.svg)

### L1: snip_compact — Trim Irrelevant Old Conversation

The agent ran 80 turns of conversation, accumulating 160 `messages`. The very first "help me create hello.py" is barely relevant to current work, yet it still occupies space.

Message count exceeds 50 → keep the first 3 (initial context) and the last 47 (current work), trim the middle; the only extra boundary rule is that `assistant(tool_use)` must not be separated from the following `user(tool_result)`:

```python
def snip_compact(messages, max_messages=50):
    if len(messages) <= max_messages:
        return messages
    head_end, tail_start = 3, len(messages) - (max_messages - 3)
    if head_end > 0 and _message_has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and _is_tool_result_message(messages[head_end]):
            head_end += 1
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    snipped = tail_start - head_end
    placeholder = {"role": "user", "content": f"[snipped {snipped} messages from conversation middle]"}
    return messages[:head_end] + [placeholder] + messages[tail_start:]
```

Messages are still trimmed directly; this just adds one boundary guard. `tool_result` content within remaining messages still keeps accumulating — message #34 may still hold 30KB of old file contents. → L2.

### L2: micro_compact — Placeholder for Old Tool Results

![Old results placeholder](images/micro-compact.en.svg)

The agent read 10 files consecutively. The full contents of reads 1–7 are still sitting in context, no longer needed, but hogging large amounts of space.

Keep only the 3 most recent `tool_result` entries intact; replace older ones with a one-line placeholder:

```python
KEEP_RECENT_TOOL_RESULTS = 3

def micro_compact(messages):
    tool_results = collect_tool_result_blocks(messages)
    if len(tool_results) <= KEEP_RECENT_TOOL_RESULTS:
        return messages
    for _, _, block in tool_results[:-KEEP_RECENT_TOOL_RESULTS]:
        if len(block.get("content", "")) > 120:
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages
```

Old results are cleared, but a single new result can be 500KB — one `cat` of a large file can max out the context. → L3.

### L3: tool_result_budget — Persist Large Results to Disk

![Large results to disk](images/layer1-budget.en.svg)

The model read 5 large files in one go; all `tool_result` blocks in the last user message total 500KB.

Sum the size of all `tool_result` blocks in the last user message. If over 200KB → sort by size, starting from the largest, persist to `.task_outputs/tool-results/`, keeping only a `<persisted-output>` marker + a 2000-character preview in context. The model sees the marker and knows the full content is on disk, re-reading it when needed.

```python
def tool_result_budget(messages, max_bytes=200_000):
    last = messages[-1]
    blocks = [(i, b) for i, b in enumerate(last["content"])
              if b.get("type") == "tool_result"]
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    if total <= max_bytes:
        return messages
    ranked = sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True)
    for idx, block in ranked:
        if total <= max_bytes:
            break
        block["content"] = persist_large_output(block["tool_use_id"], str(block["content"]))
        total = recalculate_total(blocks)
    return messages
```

The first three layers are all plain-text / structural operations — 0 API calls — but they cannot "understand" conversation content. Context may still be too large. → L4.

### L4: compact_history — Full LLM Summary

![Full LLM summary](images/auto-compact.en.svg)

All three previous layers have run, but after 30 minutes of continuous work on a huge project, tokens still exceed the threshold.

Three-step process:

1. **Save transcript**: Write the full conversation to `.transcripts/` in JSONL format. The transcript keeps a complete record; the message list keeps only the summary, so the original details no longer enter later model calls.
2. **LLM generates summary**: Send conversation history to the LLM, asking it to preserve key information: current goals, important findings, modified files, remaining work, user constraints, etc.
3. **Replace message list**: All old messages are replaced with a single summary.

```python
def compact_history(messages):
    transcript_path = write_transcript(messages)  # Save full conversation first
    summary = summarize_history(messages)          # LLM generates summary
    return [{"role": "user",
             "content": f"[Compacted]\n\n{summary}"}]
```

**Circuit breaker**: After 3 consecutive failures, stop retrying to prevent an infinite loop wasting API calls.

### Reactive: reactive_compact

Sometimes the API still returns `prompt_too_long` (413) — when context grows faster than compression triggers.

This triggers **reactive_compact**: more aggressive than compact_history in trigger (emergency response to a 413 error), but more conservative in what it removes, keeping ~5 recent messages and only summarizing earlier history. Still avoids an orphaned `tool_result`.

```python
def reactive_compact(messages):
    transcript = write_transcript(messages)
    tail_start = max(0, len(messages) - 5)
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    summary = summarize_history(messages[:tail_start])
    return [{"role": "user",
             "content": f"[Reactive compact]\n\n{summary}"}, *messages[tail_start:]]
```

Reactive compact has a retry limit (default 1). If it still fails, an exception is raised instead of looping forever. Full error recovery is deferred to s11.

### Putting It All Together

```python
def agent_loop(messages):
    reactive_retries = 0
    while True:
        # Three pre-processors (0 API calls)
        # Order: budget first, so large content is persisted before placeholders
        messages[:] = tool_result_budget(messages)    # L3: persist large results
        messages[:] = snip_compact(messages)          # L1: trim middle
        messages[:] = micro_compact(messages)         # L2: old result placeholders

        # Still too much? LLM summary (1 API call)
        if estimate_token_count(messages) > THRESHOLD:
            messages[:] = compact_history(messages)

        try:
            response = client.messages.create(...)
        except PromptTooLongError:
            if reactive_retries < MAX_REACTIVE_RETRIES:
                messages[:] = reactive_compact(messages)  # Emergency
                reactive_retries += 1
                continue
            raise  # retry limit exceeded, raise exception
        # ... tool execution ...

        # compact tool: when the model actively calls it, triggers compact_history
        if block.name == "compact":
            messages[:] = compact_history(messages)
            results.append({..., "content": "[Compacted. History summarized.]"})
            messages.append({"role": "user", "content": results})
            break  # end current turn, start fresh with compacted context
```

**The order must not be swapped.** L3 (budget) runs before L2 (micro) because micro replaces old large tool_results with one-line placeholders, so budget must persist the full content first.

---

## Changes From s07

| Component | Before (s07) | After (s08) |
|-----------|-------------|-------------|
| Context management | None (context grows unbounded) | Four-layer compression pipeline + emergency |
| New functions | — | snip_compact, micro_compact, tool_result_budget, compact_history, reactive_compact |
| Tools | bash, read_file, write_file, edit_file, glob, todo_write, task, load_skill (8) | 8 + compact (9) |
| Loop | LLM call → tool execution | Three pre-processors before each turn + threshold-triggered compact_history |
| Design principle | — | Cheap first, expensive last |

---

## Try It

```sh
cd learn-claude-code
python s08_context_compact/code.py
```

Try these prompts:

1. `Read the file README.md, then read code.py, then read s01_agent_loop/README.md` (read multiple files consecutively, observe L2 compressing old results)
2. `Read every file in s08_context_compact/` (read a large amount of content at once, observe L3 persisting to disk)
3. Chat for 20+ turns, observe whether `[auto compact]` or `[reactive compact]` appears

What to watch for: After each tool execution, are old `tool_result` entries compressed? When tokens exceed the threshold after extended conversation, is summarization triggered automatically?

---

## What's Next

Context compression lets an agent run for a long time without crashing. But after each compression, the preferences and constraints the user told it are also lost. Can we let the agent selectively remember important things?

s09 Memory → three subsystems: choosing what to remember, extracting key information, consolidating and organizing. Across compressions, across sessions.


<!-- translation-sync: zh@v2, en@v2, ja@v2 -->

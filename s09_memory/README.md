# s09: Memory — Compression Loses Details, Keep a Layer That Doesn't

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md)

s01 → ... → s07 → s08 → `s09` → [s10](../s10_system_prompt/) → s11 → ... → s20 → s21
> *"Compression loses details, keep a layer that doesn't"* — File store + index + on-demand loading, across compactions, across sessions.
>
> **Harness Layer**: Memory — knowledge that survives compaction and sessions.

---

## The Problem

s08's `compact_history` preserves current goals, remaining work, and user constraints in the summary, but details get lost: "use tabs not spaces" might get simplified to "user has code style preferences". And when you start a new session, even the summary is gone.

LLMs have no persistent state; all information lives in the context window. When context fills up, it gets compressed, and compression is lossy. What's needed is a storage layer that doesn't participate in compression and persists across sessions.

---

## The Solution

![Memory Overview](images/memory-overview.en.svg)

The s08 compression pipeline is preserved, focusing on memory. Storage uses the filesystem: a `.memory/` directory where each memory is a `.md` file with YAML frontmatter (`name` / `description` / `type`). When files accumulate, an index is needed: `MEMORY.md` holds one link per line and gets injected into the SYSTEM.

Key design: the index stays in SYSTEM prompt (cacheable by prompt cache), file content is injected on demand (matched by filename/description to the current conversation, without breaking the cache). Writing has two paths: the user explicitly says "remember", or extraction runs in the background after each turn. When files accumulate, periodic consolidation deduplicates.

> **Boundary with s08:** compaction still owns the current transcript and token budget. Memory does not replace that pipeline; it selectively persists facts outside the transcript and recalls them later.

Four memory types, each answering a different question:

| Type | Answers | Example |
|------|---------|---------|
| user | Who you are | "Use tabs not spaces" |
| feedback | How to work | "Don't mock the database" |
| project | What's happening | "Auth rewrite is compliance-driven" |
| reference | Where to find things | "Pipeline bugs are in Linear INGEST" |

---

## How It Works

![Memory Subsystems](images/memory-subsystems.en.svg)

### Storage: Markdown Files + Index

Each memory is a `.md` file with YAML frontmatter for metadata:

```markdown
---
name: user-preference-tabs
description: User prefers tabs for indentation
type: user
---

User prefers using tabs, not spaces, for indentation.
**Why:** Consistency with existing codebase conventions.
**How to apply:** Always use tabs when writing or editing files.
```

`MEMORY.md` is the index, one link per line:

```markdown
- [user-preference-tabs](user-preference-tabs.md) — User prefers tabs for indentation
```

Writing a new memory automatically rebuilds the index:

```python
def write_memory_file(name, mem_type, description, body):
    slug = name.lower().replace(" ", "-")
    filepath = MEMORY_DIR / f"{slug}.md"
    filepath.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n"
    )
    _rebuild_index()
```

### Loading: Two Paths

**Path 1: Index in SYSTEM.** `build_system()` reads `MEMORY.md` once at the start of each user request and injects the memory catalog into the SYSTEM prompt. Memory extraction and consolidation run only when the turn ends, so SYSTEM does not need to be rebuilt repeatedly within the same user request.

**Path 2: Relevant memories on demand.** At the start of each user request, `load_memories()` sends the recent conversation and the memory catalog (name + description) to the LLM as a lightweight side-query, selects relevant filenames, then reads and injects their contents. Capped at 5 to control cost.

```python
def select_relevant_memories(messages, max_items=5):
    files = list_memory_files()
    if not files:
        return []

    # Build catalog: "0: user-preference-tabs — User prefers tabs..."
    catalog = "\n".join(f"{i}: {f['name']} — {f['description']}" for i, f in enumerate(files))

    response = client.messages.create(model=MODEL, messages=[{"role": "user",
        "content": f"Select relevant memory indices. Return JSON array.\n\n"
                   f"Recent conversation:\n{recent}\n\nMemory catalog:\n{catalog}"}],
        max_tokens=200)
    indices = json.loads(re.search(r'\[.*?\]', response.content[0].text).group())
    return [files[i]["filename"] for i in indices if 0 <= i < len(files)]
```

If the side-query fails (API error, JSON parse failure), it falls back to keyword matching on name + description.

### Writing: Extraction After Each Turn

Users don't always say "remember this". Preferences are usually scattered across normal dialogue: "tabs are better than spaces", "let's use single quotes from now on".

`extract_memories()` runs when each turn ends, triggered when the model stops without a tool_use (indicating the conversation has reached a natural break):

```python
# In agent_loop:
if response.stop_reason != "tool_use":
    extract_memories(messages)   # Extract new memories from recent dialogue
    consolidate_memories()       # Check if consolidation is needed
    return
```

Before extraction, existing memories are checked to avoid duplicates. The extraction prompt asks the LLM to return a JSON array of `{name, type, description, body}`, writing files only when genuinely new information is found.

```python
def extract_memories(messages):
    dialogue = format_recent_messages(messages[-10:])
    existing = "\n".join(f"- {m['name']}: {m['description']}" for m in list_memory_files())

    prompt = (
        "Extract user preferences, constraints, or project facts.\n"
        "Return JSON array: [{name, type, description, body}].\n"
        "If nothing new or already covered, return [].\n\n"
        f"Existing memories:\n{existing}\n\nDialogue:\n{dialogue[:4000]}"
    )
    # ... parse response, write files ...
```

### Consolidation: Low-Frequency Deduplication

Memory files accumulate. `consolidate_memories()` triggers when the file count reaches a threshold (default 10), asking the LLM to deduplicate, merge contradictions, and prune stale memories:

```python
CONSOLIDATE_THRESHOLD = 10

def consolidate_memories():
    files = list_memory_files()
    if len(files) < CONSOLIDATE_THRESHOLD:
        return  # Too few, not worth consolidating
    # Send all memories to LLM, get back deduplicated list
    # Replace all files with consolidated results
```

### What Memory Stores

Memory stores information that remains useful across sessions: user preferences, recurring feedback, project background, common entry points, and investigation clues. It focuses on "what will be useful later" and brings that information back through an index plus on-demand loading.

Session memory focuses on continuity inside one session: what context should survive after compaction. The two work together: Memory handles long-term knowledge; session memory handles the current session across compaction.

---

## Changes From s08

| Component | Before (s08) | After (s09) |
|-----------|-------------|-------------|
| Memory capability | None (preferences degrade with compaction) | Storage + loading + extraction + consolidation |
| New functions | — | write_memory_file, select_relevant_memories, load_memories, extract_memories, consolidate_memories |
| Storage | — | .memory/MEMORY.md index + .memory/*.md files |
| Tools | bash, read, write, edit, glob, todo_write, task, load_skill, compact (9) | bash, read_file, write_file, edit_file, glob, task (6) |
| Loop | Only compression each turn | Memory injection + compression + post-turn extraction + periodic consolidation |

---

## Try It

```sh
cd learn-claude-code
python s09_memory/code.py
```

Try these prompts (enter across multiple turns, observe memory accumulation and loading):

1. `I prefer using tabs for indentation, not spaces. Remember that.`
2. `Create a Python file called test.py` (observe whether the Agent uses tabs)
3. `What did I tell you about my preferences?` (observe whether the Agent remembers)
4. `I also prefer single quotes over double quotes for strings.`

What to watch for: Does `[Memory: extracted N new memories]` appear after each turn? Are `.md` files generated in `.memory/`? Is `MEMORY.md` index updated? Does the Agent automatically load previous memories in new conversations?

---

## What's Next

Memory, compression, and tools are all in place. But the system prompt is still a hardcoded string. Adding a new tool means manually adding a description; switching projects means rewriting the whole prompt. Prompts should be assembled at runtime.

s10 System Prompt → segments + runtime assembly. Different projects, different tools, different prompts.


<!-- translation-sync: zh@v1, en@v1, ja@v1 -->

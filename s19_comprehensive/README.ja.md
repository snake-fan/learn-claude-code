# s19: Comprehensive Agent — すべての仕組みを 1 つのループへ

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md)

s01 → ... → s17 → s18 → `s19` → [s20](../s20_workflow_runtime/) → s21

> *"仕組みは多い、ループは 1 つ"* — tools、permissions、memory、tasks、teams、plugins はすべて同じ `while True` に接続される。
>
> **Harness レイヤー**: 総合 — s01-s18 の仕組みを 1 つの実行可能なシステムへ戻す。

---

## 問題

前 18 章では、各境界を観察できるように仕組みを一つずつ追加した。本章では、それらを一つのランタイムへ接続する。

長時間動く coding agent には、同時に次のものが必要になる：

- tool dispatch と permission boundary
- hook extension point
- todo plan と task graph
- skill、memory、runtime system prompt assembly
- compaction と error recovery
- background task と cron scheduling
- team、protocol、autonomous claiming
- worktree isolation
- MCP external tool integration

難しいのは機能を積み上げることではない。それぞれの仕組みが loop のどこに接続されるかを見抜くことだ。S19 は統合チェックポイントであり、これまでの component を 1 つの harness に戻してから、s20-s21 が編成と目標完了を外側に追加する。

---

## 解決策

![System Architecture](images/system-architecture.ja.svg)

S19 は新しい mechanism を追加せず、前章までの component を 1 つの完全な harness に統合する：

```text
user input
  → UserPromptSubmit hooks
  → cron/background notification injection
  → context compact
  → memory + skills + MCP state で system prompt を組み立てる
  → LLM
  → has tool_use block?
      no  → Stop hooks → return
      yes → PreToolUse hooks + permission
          → TOOL_HANDLERS / MCP handlers / background dispatch
          → PostToolUse hooks
          → tool_result / task_notification を messages へ戻す
          → next round
```

loop 自体は同じ構造のままだ。model を呼び、response に `tool_use` block があるかを見て、tool を実行し、結果を `messages` に戻す。tool 実行を続けるかどうかは、実際の `tool_use` block の有無で決まる。

---

## 各 Component の位置

| 位置 | Component | 役割 |
|------|-----------|------|
| user input 周辺 | `UserPromptSubmit` hooks | user input の記録、注入、監査 |
| LLM 前 | cron queue | scheduled prompt を `messages` へ注入 |
| LLM 前 | background notifications | 完了した background work を `<task_notification>` として注入 |
| LLM 前 | compaction pipeline | 大きな出力を予算化し、履歴を切り、古い tool_result を圧縮し、必要なら要約 |
| LLM 前 | memory / skills / MCP state | current capabilities と long-term context を system prompt に組み込む |
| LLM call | error recovery | 429/529 retry、`max_tokens` escalation、prompt-too-long compact |
| tool 実行前 | `PreToolUse` hooks + permission | 危険な command、範囲外 write、destructive MCP tool を止める |
| tool dispatch | `assemble_tool_pool` | built-in tools と dynamic MCP tools を組み立てる |
| tool 実行中 | background dispatch | 遅い bash work を daemon thread に逃がし、placeholder result を返す |
| tool 実行後 | `PostToolUse` hooks | large-output warning、log、後処理 |
| loop へ戻る | tool_result | 1 つの `tool_use` に 1 つの `tool_result`、そして次の model round |
| tool_use がない round / stop 時 | `Stop` hooks | 統計、cleanup、audit |

---

## code.py に含まれるもの

### Tools と Dispatch

built-in tool pool には 26 個の tool がある：

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

`assemble_tool_pool()` は毎 round で次を組み立てる：

```text
BUILTIN_TOOLS + connected MCP tools
BUILTIN_HANDLERS + mcp__server__tool handlers
```

`connect_mcp("docs")` のあと、次の round では `mcp__docs__search` のような tool が出現する。

### Permission と Hooks

permission は tool 実行行に直接埋め込まない。`PreToolUse` hook として扱う：

```python
blocked = trigger_hooks("PreToolUse", block)
if blocked:
    results.append(tool_result(block.id, blocked))
    continue
```

これにより permission、logging、audit が同じ hook point に接続できる。実行後には `PostToolUse` hook が走る。

### Plan と Task

S19 には 2 層の plan がある：

- `todo_write`: current session 用の軽量 plan。メモリに保持。
- task graph: cross-session、dependency-aware、claimable な task file。`.tasks/task_*.json` に保存。

前者は単独 agent の drift を防ぐ。後者は team coordination の土台になる。

目的は近いが実装は別である。`todo_write` は現在のセッションのチェックリスト全体を置き換え、task record は安定 ID と個別のライフサイクル更新を持つ。次節の独立した `task` ツールは「隔離 subagent を一度派遣する」意味であり、Task System ではない。

### Subagent と Team

S19 には 2 種類の delegation がある：

- `task`: one-shot subagent。独立した `messages[]` を使い、中間 context を捨て、final summary だけ返す。
- `spawn_teammate`: persistent teammate thread。ランタイムが `MessageBus` event を自動配信し、teammate は idle 中に task board を確認して自律的に claim できる。

one-shot subagent は context isolation を解決する。persistent teammate は長期並列協作を解決する。

### Memory、Skills、Prompt

`assemble_system_prompt(context)` は毎 round 次を組み立てる：

- identity と tool guidance
- workspace
- skills catalog
- `.memory/MEMORY.md`
- connected MCP servers

skills は system prompt には catalog だけ置く。全文は `load_skill(name)` で必要な時に読む。

### Compaction と Recovery

LLM call の前に compaction pipeline を走らせる：

```text
tool_result_budget → snip_compact → micro_compact → compact_history
```

model call は recovery で包む：

- 429: exponential backoff retry
- 529: exponential backoff、連続失敗時は fallback model へ切替可能
- `max_tokens`: max tokens を上げ、その後 continuation を要求
- prompt too long: reactive compact 後に retry

### Background と Cron

遅い bash work は main loop を止めない：

```text
should_run_background → start_background_task → placeholder tool_result
background done → task_notification → next round injects messages
```

cron scheduler は daemon thread として動き、1 秒ごとに確認する。CLI は `cron_queue` を監視し、発火した job を `[Scheduled] ...` として注入して Agent を 1 turn 自動実行する。

### Worktree と MCP

worktree isolation は directory を担当する：

- `create_worktree(name, task_id)` が isolated branch と directory を作る
- task の `worktree` field が task と directory を紐付ける
- teammate が worktree 付き task を claim すると、bash/read/write はその directory で実行される

MCP は external capability を担当する：

- `connect_mcp(name)` が mock server に接続する
- `assemble_tool_pool()` が MCP tools を tool pool に組み立てる
- tool name は `mcp__server__tool` 形式に統一する

---

## s18 からの変化

| Component | s18 | s19 |
|-----------|-----|-----|
| tool pool | built-in + MCP | built-in + MCP、s01-s17 の tool を補完 |
| permission | s18 の対象外 | `PreToolUse` hook で実行 |
| hooks | s18 の対象外 | UserPromptSubmit / PreToolUse / PostToolUse / Stop |
| todo | s18 の対象外 | `todo_write` + reminder |
| skill | s18 の対象外 | system prompt の catalog + `load_skill` |
| compact | s18 の対象外 | LLM 前 compaction + `compact` tool + reactive compact |
| error recovery | simple try/except | retry / max_tokens / prompt too long |
| background | s18 の対象外 | slow-operation thread + task notification |
| cron | s18 の対象外 | daemon scheduler + durable jobs |
| multi-agent | 維持 | 維持。teammate は isolated directory 上の basic tools を使う |
| worktree | 維持 | 維持 |
| MCP | 新規 | final tool pool の一部として維持 |

---

## 試す

```sh
cd learn-claude-code
python s19_comprehensive/code.py
```

試す prompt：

1. `このリポジトリを調べ、重要な Python ファイルを教えてください。`
2. `接続済みのドキュメントから agent loop の説明を探してください。`
3. `認証モジュールとログインページを隔離した worktree で並行してリファクタリングし、編集前にそれぞれのプランを見せてください。`
4. `3 分後に会議を知らせてください。`
5. `依存関係をバックグラウンドでインストールしながら README.md を読んでください。`

見るポイント：

- tool call の前に hooks/permission を通るか
- `connect_mcp` 後の次 round で MCP tool が出るか
- 遅い operation が background placeholder を返すか
- cron が時刻到達時に自動で reminder を返すか
- teammate が plan を提出し、approval 前に停止するか
- plan approval 後、teammate が task を claim できるか
- worktree binding 後、teammate が対応 directory に切り替わるか

---

## 終わりは始まり

s01 から s19 まで、コードの能力は増えていく。しかし中心は変わらない：

```python
while True:
    response = LLM(messages, tools)
    if not has_tool_use(response.content):
        return
    results = execute_tools(response.content)
    messages.append(tool_results)
```

成熟した harness の複雑さは model 周辺の協調機構から生まれる。model は判断と action selection を担当し、harness は environment、tools、permissions、memory、teams、external capabilities を整理する。

これは本コースの統合チェックポイントだ：仕組みは多い、ループは 1 つ。

次へ：[s20 Workflow Runtime](../s20_workflow_runtime/) — 編成の形が固定なら、多数の会話ターンではなく、決定的で再開可能なコードへ移す。

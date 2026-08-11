# s13: Background Tasks — 遅い操作はバックグラウンドへ

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md)

s01 → ... → s11 → s12 → `s13` → [s14](../s14_cron_scheduler/) → s15 → ... → s18 → s19

> *"遅い操作はバックグラウンドへ、agent は処理を継続"* — バックグラウンドスレッドでコマンドを実行、完了時に通知を注入。
>
> **Harness 層**: バックグラウンド — 非同期実行、メインループをブロックしない。

---

## 課題

洗濯機を使ったことがあるか？衣類を入れ、スタートを押し、他のことをする——料理、メッセージ返信、論文読み。30 分後に洗濯機が「ピッピッ」と知らせる：完了。30 分間立って待つ人はいない。

Agent の bash ツールも同じ。`pip install torch` は 10 分、`npm run build` は 3 分かかる。これらのコマンドが実行中、Agent は bash の戻りを待ち、その時間を他のタスクの処理に使えない。

ファイル読み込みはミリ秒、待たない。`git status` は 1 秒以内に戻る、待たない。しかし `npm install` は？分単位。Agent は 10 分間何もせず待ち、LLM 呼び出しはトークン課金、アイドル時間は無駄。

---

## ソリューション

![Background Tasks Overview](images/background-tasks-overview.ja.svg)

この章では、時間のかかる操作をバックグラウンドスレッドに送り、Agent はループを続行する。タスクが完了すると、結果が通知として会話に注入される。

同期 vs バックグラウンド：

| | 同期 (s12) | バックグラウンド (s13) |
|---|---|---|
| 遅い操作 | Agent が待機 | バックグラウンドスレッドで実行 |
| Agent アイドル | はい | いいえ、処理を継続 |
| 結果 | 即時返却 | 次ターンで通知を注入 |
| 判断基準 | — | bash の `run_in_background` パラメータ、ヒューリスティックフォールバック |

---

## 仕組み

### should_run_background: 明示的リクエスト優先、ヒューリスティックフォールバック

モデルは bash ツールの `run_in_background` パラメータで明示的にバックグラウンド実行をリクエストする。指定がない場合は、キーワードヒューリスティックで判断する。この経路に入るのは bash だけであり、他のツールは従来どおり引数を検証して実行する：

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
    if tool_name != "bash":
        return False
    if tool_input.get("run_in_background") is True:
        return True
    return is_slow_operation(tool_name, tool_input)
```

### start_background_task: バックグラウンド実行とライフサイクル

ツール呼び出しをワーカー関数にラップし、daemon スレッドにディスパッチ。各バックグラウンドタスクは一意 ID を持ち、`background_tasks` 辞書で状態を追跡：

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
        try:
            output, exit_code = _run_bash_process(block.input["command"])
            status = "completed" if exit_code == 0 else "failed"
            result = _format_bash_result(output, exit_code)
        except Exception as exc:
            status, result = "failed", f"Error: {exc}"
        with background_lock:
            background_tasks[bg_id]["status"] = status
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

`start_background_task()` は `bg_id` を返す。command が非ゼロで終了した場合や worker で例外が起きた場合は `failed` となり、成功として扱わない。Shell は独立した process group で起動し、command の完了、timeout、または Agent が通常経路や `SIGTERM` で終了する時に元の group を停止する。これは lifecycle cleanup であって sandbox ではなく、別の session を作った process は group から離れられる。

### collect_background_results: 通知収集

バックグラウンドタスク完了時、結果を収集して `<task_notification>` メッセージとしてフォーマット：

```python
def collect_background_results() -> list[str]:
    """Collect terminal results as task_notification messages."""
    with background_lock:
        ready_ids = [bid for bid, task in background_tasks.items()
                     if task["status"] in ("completed", "failed")]
    notifications = []
    for bg_id in ready_ids:
        with background_lock:
            task = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, "")
        notifications.append(
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>{task['status']}</status>\n"
            f"  <command>{task['command']}</command>\n"
            f"  <summary>{output[:200]}</summary>\n"
            f"</task_notification>")
    return notifications
```

通知は元の `tool_use_id` を再利用しない。元のツール呼び出しはプレースホルダー `tool_result` で応答済み。バックグラウンド完了は独立したイベントで、`task_notification` 形式で注入する。これは Messages API のツールペアリングに従う：1 つの `tool_use` に対して正確に 1 つの `tool_result`。

### ループ統合

agent_loop でツール実行は 2 つのパスに分かれる。通知と結果は 1 つの user メッセージに統合：

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

# 通知とツール結果を 1 つの user メッセージに統合
user_content = []
bg_notifications = collect_background_results()
if bg_notifications:
    for notif in bg_notifications:
        user_content.append({"type": "text", "text": notif})
user_content.extend(results)
messages.append({"role": "user", "content": user_content})
```

遅い操作は `bg_id` 付きプレースホルダー tool_result を返し、LLM はコマンドがまだ実行中だと知り、先に他のことをできる。バックグラウンド完了時、通知は独立した text block として現在のターンの tool_result と一緒に 1 つの user メッセージを構成する。

### 組み合わせて実行

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

Agent は待たなかった。npm install がバックグラウンドで実行中に、設定ファイルを読んだ。

---

## s12 からの変更

| コンポーネント | 変更前 (s12) | 変更後 (s13) |
|--------------|------------|------------|
| 実行モデル | すべて同期 | 遅い操作はバックグラウンドスレッド + 通知注入 |
| bash スキーマ | `command` | `command` + `run_in_background` |
| 新規関数 | — | `should_run_background`, `is_slow_operation`, `start_background_task`, `collect_background_results` |
| 新規型 | — | `background_tasks: dict`, `background_results: dict`, `background_lock: Lock` |
| 通知形式 | — | `<task_notification>`（tool_use_id を再利用しない） |
| ループ動作 | ツール直列実行 | 遅い操作は非同期、速い操作は同期、通知は毎ターン収集 |
| ツール | 8 (s12) | 8（変更なし、実行戦略が変更） |

---

## 試してみる

```sh
cd learn-claude-code
python s13_background_tasks/code.py
```

以下のプロンプトを試してください：

1. `Run pip list in the background and find all Python files in this directory`
2. `Run npm install (use run_in_background) and while waiting, read package.json`
3. `Create a task to setup the project, then run pip list in the background`

観察ポイント：遅い操作はバックグラウンドにディスパッチされているか？`bg_id` は返されているか？バックグラウンド通知は `<task_notification>` 形式で注入されているか？

---

## 次の章

バックグラウンドタスクは「遅い操作がブロックしない」を解決した。しかし、定期的に何かをしたい場合は？例えば「毎朝 9 時にテストを実行」「5 分ごとにサーバーステータスを確認」。

s14 Cron Scheduler → Agent にアラームクロックを付ける。


<!-- translation-sync: zh@v4, en@v4, ja@v4 -->

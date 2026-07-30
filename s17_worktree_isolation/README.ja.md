# s17: Worktree Isolation — それぞれのディレクトリ、互いに干渉しない

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md)

s01 → ... → s15 → s16 → `s17` → [s18](../s18_mcp_plugin/) → s19 → s20 → s21

> *"それぞれのディレクトリ、互いに干渉しない"* — タスクは目標を管理、worktree はディレクトリを管理、ID で紐付け。
>
> **Harness 層**: 隔離 — 並列実行のディレクトリ分離。

---

## 課題

s16 では、Alice も Bob も同じディレクトリで作業。Alice のタスクは「認証モジュールのリファクタリング」、Bob のタスクは「UI ログインページのリファクタリング」。

Alice が `write_file("config.py", ...)` を呼び出し、Bob も `write_file("config.py", ...)` を呼び出す。両者が同じファイルを編集し、互いに上書き。クリーンなロールバックもできない——どの変更が誰のものか区別できない。

s15-s16 は「誰が何をするか」（タスクシステム）と「どう通信するか」（メッセージバス）を解決したが、「どこで作業するか」は未解決。

---

## ソリューション

![Worktree Overview](images/worktree-overview.ja.svg)

Git worktree を使うと、同じリポジトリ内に複数の独立した作業ディレクトリを作成でき、それぞれが独自のブランチを持つ。Alice は `.worktrees/auth-refactor/` で作業、Bob は `.worktrees/ui-login/` で作業——互いに干渉しない。

s16 の MessageBus、プロトコル、自動認領を引き継ぐ。本章では次を追加する：

| 機能 | 目的 |
|------|------|
| create_worktree | タスク用の独立ディレクトリ + 独立ブランチを作成 |
| bind_task_to_worktree | タスクとディレクトリを紐付け（状態は変更しない） |
| remove_worktree / keep_worktree | 完了後のクリーンアップまたは保持 |
| validate_worktree_name | パストラバーサルと不正文字を拒否 |

---

## 仕組み

### 作成：タスク-Worktree 紐付け

```python
def create_worktree(name: str, task_id: str = "") -> str:
    validate_worktree_name(name)       # [A-Za-z0-9._-]{1,64} のみ許可
    path = WORKTREES_DIR / name
    ok, result = run_git(["worktree", "add", str(path), "-b", f"wt/{name}", "HEAD"])
    if not ok:
        return f"Git error: {result}"
    if task_id:
        bind_task_to_worktree(task_id, name)
    log_event("create", name, task_id)
    return f"Worktree '{name}' created at {path}"

def bind_task_to_worktree(task_id: str, worktree_name: str):
    task = load_task(task_id)
    task.worktree = worktree_name       # worktree フィールドのみ書き込み
    save_task(task)                     # 状態は pending のまま、チームメイトの claim を待つ
```

紐付けルール：1 つのタスクに 1 つの worktree を紐付け。紐付けはタスクの状態を変更しない——タスクは `pending` のままで、チームメイトが認領した時に `in_progress` に進む。これにより Lead は事前にタスクと worktree を作成でき、チームメイトは idle 時に自然に worktree 紐付け済みタスクを認領する。

### チームメイトツールの cwd 切り替え

各チームメイトは、現在の worktree パスを記録する `wt_ctx` 辞書を持つ。worktree に紐付いたタスクを認領すると、ランタイムが `wt_ctx` を更新し、そのチームメイトの `bash`、`read_file`、`write_file` は対応する worktree ディレクトリで実行される：

```python
# チームメイトスレッド内部
wt_ctx = {"path": None}

def _run_claim_task(task_id):
    result = claim_task(task_id, owner=name)
    if "Claimed" in result:
        task = load_task(task_id)
        if task.worktree:
            wt_ctx["path"] = str(WORKTREES_DIR / task.worktree)
    return result

def _run_bash(command):
    return run_bash(command, cwd=wt_ctx["path"])  # worktree で実行
```

### クリーンアップ：Keep または Remove

タスク完了後、2 つの選択肢：

```python
def remove_worktree(name: str, discard_changes: bool = False) -> str:
    # 安全チェック：変更がある場合デフォルトで拒否
    if not discard_changes:
        files, commits = _count_worktree_changes(path)
        if files > 0 or commits > 0:
            return "未コミットの変更あり。discard_changes=true で強制削除、または keep_worktree で保持"
    ok, _ = run_git(["worktree", "remove", str(path), "--force"])
    if not ok:
        return "削除失敗"
    run_git(["branch", "-D", f"wt/{name}"])
    log_event("remove", name)

def keep_worktree(name: str) -> str:
    log_event("keep", name)
    return f"Worktree '{name}' kept for review (branch: wt/{name})"
```

Keep = ブランチを保持し、手動 review 後にマージ。Remove = 未コミット変更がある場合デフォルトで拒否、`discard_changes=true` で確認が必要。タスクの自動 complete はしない——タスク完了はチームメイトの `complete_task` で明示的にトリガー。

### イベントログ：監査可能

各ライフサイクル操作はログに記録され、監査に利用：

```python
def log_event(event_type: str, worktree_name: str, task_id: str = ""):
    event = {"type": event_type, "worktree": worktree_name,
             "task_id": task_id, "ts": time.time()}
    # .worktrees/events.jsonl に append
```

イベントタイプは `create`、`remove`、`keep`。ログは手動監査に使い、復元時は `git worktree list` から現在の worktree 一覧を再構築できる。

### run_git：成功/失敗を返す

```python
def run_git(args: list[str]) -> tuple[bool, str]:
    r = subprocess.run(["git"] + args, cwd=WORKDIR, ...)
    return r.returncode == 0, output
```

`create_worktree` と `remove_worktree` は git コマンド成功後のみイベントログに書き込み、ログが実際の状態を反映することを保証。

---

## s16 からの変更

| コンポーネント | 変更前 (s16) | 変更後 (s17) |
|--------------|------------|------------|
| 作業ディレクトリ | 全 Agent が WORKDIR を共有 | 各タスクが git worktree に紐付け可能 |
| タスクデータ | id/subject/status/owner/blockedBy | + worktree フィールド |
| チームメイトツール cwd | 常に WORKDIR | worktree 紐付けタスク認領時に自動切り替え |
| 新規関数 | — | create_worktree, bind_task_to_worktree, remove_worktree, keep_worktree, validate_worktree_name |
| worktree 安全性 | なし | name 検証 + 変更ありの場合削除拒否 |
| イベントログ | なし | events.jsonl ライフサイクル監査 |
| Lead ツール | チーム・タスクツール | + create_worktree、remove_worktree、keep_worktree |
| チームメイトツール | タスク・ファイルツール | ツールは同じ。bash/read/write は認領した worktree の cwd を使う |

---

## 試してみる

```sh
cd learn-claude-code
python s17_worktree_isolation/code.py
```

以下のプロンプトを試してください：

`認証モジュールとログインページを並行してリファクタリングし、変更が互いに干渉しないようにしてください。`

観察ポイント：2 つの worktree の `git status` 出力は異なるブランチを表示しているか？チームメイトが worktree 紐付けタスクを認領後、bash コマンドは worktree ディレクトリで実行されているか？`remove_worktree` は変更がある場合に拒否するか？紐付け後のタスク状態は `pending` のままか？

---

## 次の章

Agent チームが隔離されたワークスペースで自己組織化できるようになった。しかし Agent の能力はツールに制限される——bash、read、write、task...

もしユーザーが独自のツールを持っていたら？例えば社内 Jira API や独自デプロイシステム？

s18 MCP Plugin → Agent にプラグインシステムを追加。外部ツールが標準プロトコルで接続、Agent は誰が書いたか知る必要がない。


<!-- translation-sync: zh@v1, en@v1, ja@v1 -->

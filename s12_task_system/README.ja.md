# s12: Task System — 大きな目標を小さなタスクに分割

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md)

s01 → ... → s10 → s11 → `s12` → [s13](../s13_background_tasks/) → s14 → ... → s20 → s21

> *"大きな目標を小さなタスクに分け、順序付け、永続化"* — ファイル永続化タスクグラフ、マルチ Agent 協調の基盤。
>
> **Harness 層**: タスク — 永続化された目標、復旧可能な進捗。

---

## 課題

Agent がプロジェクトを受けた：データベース構築、API 実装、テスト追加。s05 の TodoWrite でリストを作り、まず API を書き始め、途中でデータベーステーブルがないことに気づいて戻る。テスト追加時に API インターフェースのシグネチャがまた変わっている...

屋根を先に建てて基礎を後から打つことはできない。タスクには順序がある。タスク間の前提依存関係は有向非巡回グラフ（DAG）として表現でき、この章では `blockedBy` でそれらを記録する。

s05 の TodoWrite は現在のタスクの実行チェックリストで、セッションメモリに保持される。ここで必要なのは**タスクシステム**：各タスクは JSON ファイル、タスク間に `blockedBy` 依存関係、ディスク上でセッションをまたいで永続化。

---

## ソリューション

![Task System Overview](images/task-system-overview.ja.svg)

この章では、5 つのタスクツール、`.tasks/` ディレクトリへの永続化、`blockedBy` の依存チェックを追加する。

TodoWrite vs Task System：

| | TodoWrite (s05) | Task System (s12) |
|---|---|---|
| 位置づけ | 現在のタスクの実行チェックリスト | 復旧可能なタスクシステム |
| ストレージ | プロセス内 / セッション状態 | `.tasks/{id}.json` |
| 依存関係 | なし | `blockedBy` / `blocks` グラフ |
| ライフサイクル | 現在のセッション / 現在のタスク | セッション横断 |
| 分担 | タスク認識を扱わない | `owner` / claim |
| ステータス | pending / in_progress / completed | pending / in_progress / completed |
| 粒度 | Agent 自身の手順 | 認識・追跡・アンロックできるタスク |
| 更新契約 | リスト全体を置換 | 個別レコードを作成・取得・更新・一覧 |

---

## 仕組み

![Task DAG](images/task-dag.ja.svg)

### Task: データ構造

各タスクは JSON ファイル、`.tasks/` ディレクトリに保存：

```python
@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str          # pending | in_progress | completed
    owner: str | None    # Agent 名（マルチ Agent シナリオ）
    blockedBy: list[str] # 依存タスク ID のリスト
```

ID は `timestamp + random hex` で生成する。

### create_task: タスク作成

```python
def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    task = Task(
        id=f"task_{int(time.time())}_{random_hex(4)}",
        subject=subject, description=description,
        status="pending", owner=None,
        blockedBy=blockedBy or [],
    )
    save_task(task)
    return task
```

作成時に自動的に `save_task` で `.tasks/{id}.json` に書き込み。`blockedBy` で依存を宣言、例えば "API を書く" の `blockedBy` は `["task_schema"]`。

### can_start: 依存チェック

タスクは `blockedBy` が**すべて completed** になってからでないと開始できない：

```python
def can_start(task_id: str) -> bool:
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False  # missing dependency = blocked
        dep = load_task(dep_id)
        if dep.status != "completed":
            return False
    return True
```

`can_start` は `claim_task` の事前チェック：`blockedBy` に一つでも completed でないものがあれば、認識不可。存在しない依存は blocked として扱い、誤った ID 参照時のクラッシュを防ぐ。

### claim_task: タスク認識

Agent がタスクに取り掛かる時、`claim_task` を呼び出し：`owner` を設定、ステータスを `pending` → `in_progress` に変更。`owner` フィールドは誰が作業中かを記録し、マルチ Agent シナリオで重複認識を防止：

```python
def claim_task(task_id: str, owner: str = "agent") -> str:
    task = load_task(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if load_task(d).status != "completed"]
        return f"Blocked by: {deps}"
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    return f"Claimed {task_id} ({task.subject})"
```

タスクが既に他者に認識されている（`status != "pending"`）、または依存が未完了（`can_start` が False）の場合、認識を拒否。

### complete_task: 完了とアンロック

タスク完了後、`completed` に設定。同時に他の全タスクを走査し、**直前にアンロックされた**下流タスクを特定：

```python
def complete_task(task_id: str) -> str:
    task = load_task(task_id)
    task.status = "completed"
    save_task(task)
    # アンロックされた下流タスクを検索
    unblocked = [t.subject for t in list_tasks()
                 if t.status == "pending" and t.blockedBy
                 and can_start(t.id)]
    msg = f"Completed {task_id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
    return msg
```

"schema" 完了後、"endpoints" と "docs" の `can_start` が True を返し、開始可能になる。

### get_task: 完全な詳細を確認

`list_tasks` は 1 行サマリのみ表示。`get_task` は description と依存関係の詳細を含む完全なタスク JSON を返す。セッションをまたいで復旧する際、Agent は完全な説明を読んで作業を継続する必要がある：

```python
def get_task(task_id: str) -> str:
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2)
```

### 状態マシン: 2 つのアクション、3 つの状態

```
pending ──claim──→ in_progress ──complete──→ completed
```

ここで `claim` / `complete` はアクション、`pending` / `in_progress` / `completed` は状態：

- **claim_task**: `pending` → `in_progress`。owner を設定し、作業を開始。
- **complete_task**: `in_progress` → `completed`。タスクを完了済みにし、下流をアンロック。

### 組み合わせて実行

```python
# 依存関係のあるタスクを作成
schema = create_task("setup database schema")
endpoints = create_task("create API endpoints", blockedBy=[schema.id])
tests = create_task("write tests", blockedBy=[endpoints.id])
docs = create_task("write docs", blockedBy=[schema.id])

# Agent が最初に実行可能なタスクを認識
claim_task(schema.id)       # ✓ Claimed（依存なし）
complete_task(schema.id)    # ✓ Completed → endpoints, docs をアンロック

claim_task(endpoints.id)    # ✓ Claimed（schema 完了済み）
complete_task(endpoints.id) # ✓ Completed → tests をアンロック

claim_task(docs.id)         # ✓ Claimed（schema 完了済み）
complete_task(docs.id)      # ✓ Completed

claim_task(tests.id)        # ✓ Claimed（endpoints 完了済み）
complete_task(tests.id)     # ✓ Completed
```

各 `create_task` が JSON ファイルを書き込み、各 `claim_task` / `complete_task` がファイルを更新。セッションをまたいでも `.tasks/` ディレクトリが残り、Agent はファイルを読んで進捗を復旧。

---

## s11 からの変更

| コンポーネント | 変更前 (s11) | 変更後 (s12) |
|--------------|------------|------------|
| タスク管理 | なし | Task dataclass + 5 ツール |
| 新規型 | — | Task（id, subject, description, status, owner, blockedBy） |
| ストレージ | 永続化なし | `.tasks/{id}.json` セッション横断 |
| 依存関係 | なし | `blockedBy` グラフ + `can_start` チェック |
| ツール | bash, read_file, write_file (3) | + create_task, list_tasks, get_task, claim_task, complete_task (8) |
| ライフサイクル | — | pending → in_progress → completed（release ロールバックなし） |

---

## 試してみる

```sh
cd learn-claude-code
python s12_task_system/code.py
```

以下のプロンプトを試してください：

1. `Create tasks: setup database schema, create API endpoints (depends on schema), write tests (depends on endpoints), write docs (depends on schema)`
2. `List all tasks and their statuses`
3. `Claim the first unblocked task and complete it`
4. `List tasks again — which ones are now unblocked?`

観察ポイント：`.tasks/` ディレクトリに JSON ファイルが生成されているか？タスク完了後、ブロックされていたタスクがアンロックされているか？

---

## 次の章

タスクグラフができた。しかし、一部のタスクは長時間かかる — 全テスト実行やサーバーデプロイなど。Agent は LLM をトークン課金で呼び出しており、遅い操作を待つ余裕はない。

s13 Background Tasks → 遅い操作はバックグラウンドへ。Agent は他のタスクの処理を続け、バックグラウンドの完了を通知で受け取る。


<!-- translation-sync: zh@v1, en@v1, ja@v1 -->

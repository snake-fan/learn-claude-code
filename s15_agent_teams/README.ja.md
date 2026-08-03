# s15: Agent Teams — チームランタイムと協調プロトコル

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md)

s01 → ... → s13 → s14 → `s15` → [s16](../s16_mcp_plugin/) → s17 → s18 → s19

> *「1 つの Agent で仕事全体を抱えきれないなら、チームメイトで分担する。」* — 永続チームメイト、共有タスクの Claim、任意の worktree、協調プロトコル。
>
> **Harness レイヤー**：Team — 複数の Agent が Lead の管理下で仕事を分担し、状態を共有する仕組み。

---

## 問題

Agent にバックエンド全体のリファクタリングを依頼するとする。作業範囲は設定の読み込み、認証、テストにまたがる。1 つの Agent でも順番に処理できるが、時間がかかり、初期の詳細は少しずつコンテキストから抜けていく。

この仕事は並列化に向いている。ただし、ユーザーは通常、チーム構成ではなく目標を伝える：

```text
このサンプルバックエンドをリファクタリングしてください。
設定の読み込み、認証、テストを整理し、既存インターフェースを保ち、
テストが通ることを確認してください。
```

Harness は、つながった 6 つの問題を扱う必要がある：

1. 並列作業が有効だと誰が判断し、追加の Agent を誰が承認するのか。
2. 各チームメイトは、複数の割り当てをまたいで識別子とコンテキストをどう保つのか。
3. モデルに受信箱をポーリングさせず、結果を Lead へどう返すのか。
4. IDLE のチームメイトは、次の指示を待たずに ready task を引き受けられるか。
5. 並列編集が衝突し得る時、タスクはどの作業ディレクトリを使うのか。
6. shutdown と計画承認を、追跡できて実際に制約をかけるプロトコルにするにはどうするか。

---

## 解決策

![Agent Teams Overview](images/agent-teams-overview.ja.svg)

s15 は、単一 Agent の Harness に Lead 管理のチームランタイムを加える：

- **Lead** はユーザーとの会話を担当し、分担案を示して確認を待つ。
- **チームメイト** は独立した Agent Loop を実行し、WORK と IDLE を行き来する。
- **MessageBus** は、ファイルベースの受信箱で通常メッセージ、結果、制御イベントを運ぶ。
- **ランタイム配信** は Lead の受信箱を消費し、チームイベントを次のターンへ追加する。
- **共有タスクボード** により、IDLE のチームメイトは ready task を探し、ロック下で Claim できる。
- **任意の worktree** は、必要なタスクだけを別の作業ディレクトリへ紐付ける。紐付けのないタスクは通常のリポジトリディレクトリを使う。
- **型付きプロトコルと計画ゲート** は shutdown と承認状態を明示し、必要な計画が承認されるまで変更系ツールを止める。

これらはすべて Team Harness レイヤーの一部である。タスク発見のために別の Agent Loop は要らず、worktree が別種の Agent を作るわけでもない。

---

## 仕組み

### 1. Lead はチーム案を示し、ユーザーの確認を待つ

チームメイトを起動すると、コスト、並行度、ワークスペースを編集できる主体が変わる。Lead のシステムプロンプトは、その境界を明示する：

```python
"When parallel work would help, first propose a small team with clear "
"responsibilities and wait for the user's confirmation. Do not call "
"spawn_teammate before the user confirms."
```

最初の要求に対して、Lead は分担案だけを示す：

```text
3 つの領域を並行して進めることを提案します：
- config：設定の読み込みを整理
- auth：認証をリファクタリング
- tests：回帰テストを追加

確認後にチームメイトを起動します。
```

ユーザーが「始めてください」と返した後、Lead は `spawn_teammate` を呼べる。ユーザーが目標を示し、Lead がチームを設計し、ユーザーが実行境界を確認する。

### 2. 各チームメイトは独立したループを持つ

s06 の subagent は 1 回限りの呼び出しである。チームメイトは永続する実行単位だ：

| | s06 Subagent | s15 Teammate |
|---|---|---|
| ライフサイクル | 1 回の呼び出し後に終了 | shutdown まで `WORK → IDLE → WORK` |
| コンテキスト | 1 つのタスクにだけ存在 | 割り当てをまたいで保持 |
| 通信 | 1 回だけ結果を返す | メッセージを受け取りイベントを送る |
| 協調 | 一方向の委譲 | Lead との双方向協調 |

`spawn_teammate_thread()` は、各チームメイト専用のシステムプロンプト、messages、ツール、現在の作業ディレクトリ状態を用意し、daemon thread でループを実行する。チームメイトの作業中も Lead は調整を続けられる。`lead` と `agent` はランタイム識別子として予約されるが、`MessageBus` はコーディネーターの受信箱として `lead` を引き続き受け付ける。

### 3. MessageBus は通信をモデルのコンテキスト外に置く

Lead とチームメイトは同じ messages 配列を共有できない。共有すると、あるチームメイトのツール結果が別のチームメイトの推論へ混ざる。`MessageBus` は Agent ごとに `.mailboxes/<name>.jsonl` 受信箱を用意する：

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
            with open(self._path(to_agent), "a") as f:
                f.write(json.dumps(msg) + "\n")
            self._changed.notify_all()

    def wait_for_messages(self, agent, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while not self.peek(agent):
                remaining = (None if deadline is None
                             else deadline - time.monotonic())
                if remaining is not None and remaining <= 0:
                    return []
                self._changed.wait(remaining)
            return self._read_unlocked(agent)
```

ロックは、チームメイトによる受信箱ファイルの並行アクセスを保護する。`Condition` はメッセージ到着時にチームメイトを起こし、IDLE 中の短い timeout にも使える。

### 4. 受信イベントはランタイムが配信する

`read_inbox()` は受信箱ファイルを読み取って削除するため、Lead 側の消費処理は `consume_lead_inbox()` だけにする：

```python
def consume_lead_inbox():
    messages = BUS.read_inbox("lead")
    for message in messages:
        if message["type"].endswith("_response"):
            match_response(...)
    return messages
```

メインループの隣で動くイベントスレッドが、新しいメッセージの到着時に Lead を起こす：

```text
MessageBus → consume_lead_inbox
           → プロトコル状態を更新
           → [Team events] を history に追加
           → Lead の次ターンを開始
```

`check_inbox` はモデルのツールではない。メッセージの到着と消費はランタイムが担当し、モデルはコンテキストへ配信済みのイベントを処理する。

### 5. 結果と IDLE は別のイベントである

チームメイトが 1 つの割り当てを終えると、ランタイムは 2 つのイベントを順に送る：

```text
result:            "認証をリファクタリングし、関連テストが通りました。"
idle_notification: "Waiting for more work."
```

`result` は「この割り当てで何ができたか」、`idle_notification` は「このチームメイトが次の仕事を受けられるか」を表す。曖昧な「完了」だけでは、両方の状態を表せない。

IDLE のチームメイトは終了しない。直接メッセージか ready task を受けると WORK に戻り、`shutdown_request` を受けると段階的な shutdown handshake を始める。

### 6. IDLE は受信箱を先に確認し、その後 ready task を探す

IDLE ではメッセージを優先し、その後に共有タスクボードを確認する：

```python
while True:
    inbox = BUS.wait_for_messages(name, IDLE_SCAN_INTERVAL)
    if inbox:
        should_stop = handle_messages(inbox)
        if should_stop or messages[-1]["role"] == "user":
            break
        continue

    task = claim_next_task(name)
    if task:
        messages.append({
            "role": "user",
            "content": f"[Auto-claimed task {task.id}] {task.subject}",
        })
        break
```

shutdown、計画承認、Lead からの直接指示は、空き時間に見つけた仕事より先に扱う。メッセージも ready task もなければ、チームメイトは IDLE を続ける。別のチームメイトが前提タスクを完了すると、blocked task が ready になることもある。

### 7. 発見と Claim を分け、Claim はアトミックに行う

走査は候補を探すだけで、状態を変更しない：

```python
def scan_unclaimed_tasks() -> list[Task]:
    return [
        task for task in list_tasks()
        if task.status == "pending"
        and task.owner is None
        and can_start(task.id)
    ]
```

候補一覧は一時点の snapshot にすぎない。別のチームメイトも同じタスクを見る可能性があるため、所有権の変更は `task_lock` で保護した `claim_task()` 内で行う：

```python
def claim_task(task_id: str, owner: str) -> str:
    with task_lock:
        task = load_task(task_id)
        if task.status != "pending" or task.owner is not None:
            return "Task is no longer available"
        if _owner_in_progress(owner):
            return "Owner must complete its current task first"
        if not can_start(task_id):
            return "Task is blocked"
        cwd, error = task_worktree_cwd(task)
        if error:
            return f"Cannot claim {task_id}: {error}"
        task.owner = owner
        task.status = "in_progress"
        save_task(task)
        teammate_assignments[owner] = {"task_id": task.id, "cwd": cwd}
        return f"Claimed {task.id}"
```

複数のチームメイトが同じ候補を発見しても、`in_progress` へ進められる Claim は 1 つだけである。現在のタスクを完了するまで、チームメイトは次のタスクを Claim できない。worktree の紐付けが壊れている場合、リポジトリディレクトリへ戻さず Claim を失敗させる。

### 8. Claim した仕事は同じ WORK ループを再利用する

Claim に成功すると、ランタイムはタスク ID、件名、説明をチームメイトの messages へ追加する：

```text
ready task が現れる
  → IDLE のチームメイトが発見
  → claim_task が owner と in_progress を記録
  → タスクがチームメイトの messages に入る
  → WORK
  → complete_task
  → result + idle_notification
  → IDLE
```

チームメイトは、Lead が直接割り当てた時と同じモデル呼び出し、ファイルツール、Shell、計画ゲート、結果通知、shutdown protocol を使う。タスク発見は、既存の WORK ループへの別の入口である。

### 9. タスクがツールの作業ディレクトリを選ぶ

`Task.worktree` は任意フィールドである：

```python
@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str]
    worktree: str | None = None
```

並列編集を別ディレクトリに分けたい時、Lead は worktree を作成してタスクへ紐付けられる：

```python
create_worktree(name="auth-refactor", task_id="task_1234")
```

`create_worktree` は Lead 専用ツールである。pending、owner なし、worktree 未設定のタスクを受け取り、名前、パス、ブランチ、Git registry を確認する。checkout の作成後にだけタスクへ紐付ける。Git が失敗を返しても branch や登録済み checkout が残った場合は partial operation を報告し、task は未紐付けのまま、それらを manual recovery 用に保持する。チームメイトが使うのはタスクツールとファイルツールである。

Claim 時に、解決済みのディレクトリを `teammate_assignments` へ保存する。チームメイトの `bash`、`read_file`、`write_file` wrapper は assignment からディレクトリを読む。worktree のないタスクは `WORKDIR` に解決されるため、worktree は opt-in である：

```python
cwd, error = task_worktree_cwd(task)
if not error:
    teammate_assignments[owner] = {
        "task_id": task.id,
        "cwd": cwd,
    }
```

`complete_task(task_id, owner)` は、呼び出し元が進行中タスクの owner か確認する。ランタイムが assignment を削除するのは完了に成功した時だけである。失敗時はタスクのディレクトリを維持し、チームメイトが修正して再試行できるようにする。タスクの `worktree` 紐付けは checkout を削除するまで残る。

> Worktree が分離するのは Git の作業ディレクトリとブランチであり、sandbox ではない。Shell コマンドは親プロセスに許可されたパスやリソースへアクセスできる。

### 10. Worktree のクリーンアップはデフォルトで作業を残す

モデル向けの `remove_worktree(name)` tool は、`pending` または `in_progress` のタスクに紐付いた worktree の削除を拒否する。タスク完了後も tracked、untracked、ignored file をすべて未コミットデータとして扱い、clean な checkout だけを `--force` なしで削除する。

低レベルの Python helper は、host が別途ユーザーの明示的な確認を得た場合のために `discard_changes=True` を残すが、この parameter はモデルの tool schema にはない。変更のある worktree は削除せず、user が確認できる状態で残す。どちらの削除経路でも `wt/<name>` ブランチはリポジトリに残り、upstream のない clean な local commit も保持される。削除成功後は checkout が存在しないため、タスクの worktree 紐付けを解除する。

```text
clean worktree   → ディレクトリを削除し、wt/<name> ブランチは保持
changed worktree → model tool は拒否し、保持か破棄かを user が決める
pending/running task → 削除を拒否
```

タスク完了と worktree cleanup も分かれている。`complete_task` はタスク結果を記録し、Lead はその後に worktree を確認、merge、keep、remove できる。

### 11. 制御メッセージには型と request_id を使う

通常の協調には自由形式のテキストを使えるが、shutdown と承認を意図の推測に任せるべきではない。これらは構造化メッセージを使う：

![Team Protocols](images/team-protocols-overview.ja.svg)

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

shutdown の流れは次の通り：

```text
Lead が pending の shutdown request を作る
  → shutdown_request(request_id) がチームメイトの受信箱に入る
  → チームメイトが現在のステップを終える
  → shutdown_response(request_id) が Lead へ戻る
  → request_id で元の request を特定する
  → pending が approved になり、チームメイトの loop が終了する
```

ID は応答を 1 つの request に対応付け、型は不一致の応答による状態変更を防ぎ、status は同じ応答の二重適用を防ぐ。

### 12. 計画承認は実行も制約する

計画プロトコルは逆方向に進む：

```text
Lead → plan_request
チームメイト → plan_approval_request(request_id, plan)
Lead → plan_approval_response(request_id, approve, feedback)
```

ツール dispatch がゲートを強制する：

```python
def _run_teammate_tool(name, block, handlers):
    gate = plan_gates.get(name, "not_required")
    if block.name in {"bash", "write_file"} and gate not in {
        "not_required", "approved"
    }:
        return f"Blocked: plan status is {gate}."
    return handlers[block.name](**block.input)
```

状態が `required`、`pending`、`rejected` の間、チームメイトはファイルを読み、計画を提出または修正できるが、Shell コマンドの実行とファイルの書き込みはできない。承認応答で状態が `approved` になると、ツールを使えるようになる。

---

## 一連の実行例

```text
s15 >> バックエンドのリファクタリングを共有タスクボードに分解し、
       設定、認証、テストを可能な範囲で並行実行してください。
       認証には worktree を使い、既存インターフェースを保ち、
       テストが通ることを確認してください。

Lead：config、auth、tests の 3 領域に分けることを提案します。
      チームを起動しますか？

s15 >> 始めてください

[task] config created
[task] auth created → worktree auth-refactor
[task] tests created
[teammate] alice spawned
[teammate] bob spawned
[claim] alice → config (cwd: repository)
[claim] bob → auth (cwd: .worktrees/auth-refactor)
[complete] auth
[bus] bob → lead (result) ...
[bus] bob → lead (idle_notification) ...
[wake: 2 team events → new turn]
Lead：認証タスクの結果を受け取りました。残りの作業を調整します。
```

ターミナルには、ユーザーの要求、Lead の提案、タスク状態、Claim、選択されたディレクトリ、結果、IDLE 遷移、制御イベントが表示される。ユーザーが Lead を指定したり、受信箱の確認を依頼したりする必要はない。

---

## s14 からの変更

| コンポーネント | s14 | s15 |
|---|---|---|
| Agent | 1 つの Agent | 1 つの Lead と永続チームメイト |
| ユーザーフロー | 要求を実行 | チーム案を示してから起動確認 |
| 通信 | なし | ファイル受信箱とランタイム配信 |
| ライフサイクル | 1 つのループ | チームメイトの `WORK / IDLE / shutdown` |
| 共有作業 | Lead の既存タスクツール | IDLE 走査とチームメイトのアトミックな Claim |
| 作業ディレクトリ | リポジトリの `WORKDIR` | デフォルトは `WORKDIR`、タスクごとに worktree を選択可能 |
| 結果通知 | 現在の Agent の出力 | `result` と `idle_notification` を分離 |
| 制御 | なし | 型付き shutdown と計画承認プロトコル |
| 強制 | チーム向け制約なし | 必須計画が変更系ツールをゲート |

---

## 試してみる

```sh
cd learn-claude-code
python s15_agent_teams/code.py
```

通常の要求を入力する：

```text
バックエンドのリファクタリングを共有タスクボードへ分解し、依存関係が
許す範囲で設定、認証、テストを並行実行してください。認証には worktree
を使い、既存インターフェースを維持して、最後に結果をまとめてください。
```

Lead がチーム案を示したら、次のように返す：

```text
始めてください
```

`.tasks/` が `pending`、`in_progress`、`completed` と変化する様子、`.mailboxes/` が `result` と `idle_notification` を配信する様子、紐付けたタスクにだけ `.worktrees/` が作られることを確認する。直接メッセージがタスクボード走査より優先されることと、`complete_task` の失敗後もチームメイトの作業ディレクトリが変わらないことも確認できる。

---

## 次へ

チームランタイムは、委譲、共有タスクの Claim、任意の作業ディレクトリを扱えるようになった。ただし、ツールは今も Python コードへ直接定義している。

次のレッスンでは、標準の発見・呼び出しプロトコルを使って外部ツールへ接続する。

次へ：[s16 MCP Tools](../s16_mcp_plugin/)。

<!-- translation-sync: zh@v3, en@v3, ja@v3 -->

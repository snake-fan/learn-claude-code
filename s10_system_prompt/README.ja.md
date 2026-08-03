# s10: Context Assembly — 実行時にモデル入力を組み立てる

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md)

s01 → ... → s08 → s09 → `s10` → [s11](../s11_error_recovery/) → s12 → ... → s18 → s19
> *"モデル入力は組み立てるもの、固定するものではない"* — 安定セクション + 実行時状態 + キャッシュ。
>
> **Harness レイヤー**: コンテキスト組み立て — 安定した指示と動的状態をモデル入力にまとめる。

---

## 課題

s01 から s09 まで、system prompt は常に 1 行のハードコード：

```python
SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks."
```

s01 では十分だった。bash、read、write の 3 ツールのみ。しかし s09 では、Agent に記憶、圧縮、スキル読み込みがある。prompt が説明すべき能力が増え続ける：

```python
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Use tools to solve tasks. Act, don't explain. "
    "Before starting any multi-step task, use todo_write. "
    "Skills are available via list_skills and load_skill. "
    "Relevant memories are injected below when available. "
    # ... 能力を追加するたびに 1 行増える
)
```

3 つの問題：

1. **プロジェクトを変えるには prompt 全体を書き直す**必要がある。何を変え、何を残すべきか不明
2. **一箇所の変更が全体に影響する**。ツール説明を追加すると、前の指示と矛盾する可能性
3. **毎回のリクエストが全内容を送信する**。現在の会話で不要なセクションも token を無駄に消費

System prompt は、実行時の現在状態に基づいて組み立てられる設定であるべき：どのツールが有効か、どのコンテキストが可視か、どの記憶が関連するか、どの内容を prompt cache に命中させるために安定させるべきか。

---

## ソリューション

![System Prompt Overview](images/system-prompt-overview.ja.svg)

s10 はコンテキスト管理とエラー回復をつなぐ短い橋渡しセッションである。新しいストレージを追加せず、s08 と s09 も統合しない。両者の出力がモデル境界でどう合流するかを示す：ハードコードされた `SYSTEM` を独立セクションに分割し、実際の実行時状態から組み立て、結果をキャッシュする。

4 つのセクション、2 つの読み込み戦略：

| セクション | 戦略 | 内容 | 判断基準 |
|-----------|------|------|---------|
| identity | 常に | あなたは誰か、どう作業するか | 常に存在 |
| tools | 常に | 利用可能ツール一覧 | `enabled_tools` |
| workspace | 常に | 作業ディレクトリ | 常に存在 |
| memory | オンデマンド | 関連記憶内容 | `.memory/MEMORY.md` が存在するか |

重要な設計：セクションをロードするかどうかは実際の状態（ツールが存在するか、ファイルが存在するか）で決まり、メッセージ内のキーワードではない。

---

## 仕組み

### PROMPT_SECTIONS: トピック別フラグメント

単一の文字列を辞書に分割、各キーがトピック：

```python
PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
}
```

各セクションは独立して管理。`tools` を変更しても `identity` に影響しない。`memory` を追加しても `workspace` はそのまま。

### assemble_system_prompt: オンデマンド組み立て

すべてのセクションが毎ターン必要なわけではない。記憶ファイルがなければ、memory セクションをロードしても token の無駄。context の実際の状態に基づいて組み立てる：

```python
def assemble_system_prompt(context: dict) -> str:
    sections = []

    # 常にロード
    sections.append(PROMPT_SECTIONS["identity"])

    # context から動的に tools と workspace を取得
    tools = ", ".join(context.get("enabled_tools", []))
    if tools:
        sections.append(f"Available tools: {tools}.")
    sections.append(f"Working directory: {context.get('workspace', WORKDIR)}")

    # オンデマンド — 実際の状態に基づく、キーワードではない
    memories = context.get("memories", "")
    if memories:
        sections.append(f"Relevant memories:\n{memories}")

    return "\n\n".join(sections)
```

「常にロード」は毎ターン必要なもの：アイデンティティ、ツール、作業ディレクトリ。「オンデマンド」は特定条件下でのみ有用。

なぜ全部ロードしないのか？token にはコストがあり（system prompt は毎ターン課金）、情報が少ないほど LLM は集中する（無関係な指示はノイズ）。

### get_system_prompt: キャッシュで再組み立てを回避

コンテキストが変わっていない時（同じターン内で複数の LLM 呼び出し、context が同じ）、再組み立ては無駄。確定的シリアライズで変化を検出し、キャッシュヒット時は即座に返却：

```python
def get_system_prompt(context: dict) -> str:
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    return _last_prompt
```

`hash()` ではなく `json.dumps` を使用：Python 組み込みの `hash()` にはプロセスランダム化があり（安定したキャッシュキーに不適切）、list/dict で `unhashable type` エラーになる。

### context: 実際の状態、キーワード推測ではない

context は現在の実行時状態の実際の状態を反映：

```python
def update_context(context: dict, messages: list) -> dict:
    memories = ""
    if MEMORY_INDEX.exists():
        content = MEMORY_INDEX.read_text().strip()
        if content:
            memories = content
    return {
        "enabled_tools": list(TOOL_HANDLERS.keys()),
        "workspace": str(WORKDIR),
        "memories": memories,
    }
```

`enabled_tools` は実際に登録されたツールを一覧。`memories` は `.memory/MEMORY.md` が存在するかを確認。セクションの読み込みはこの実際の状態に基づき、メッセージ内のキーワード検索ではない。

### 組み合わせて実行

```python
def agent_loop(messages: list, context: dict):
    system = get_system_prompt(context)
    while True:
        response = client.messages.create(
            model=MODEL, system=system, messages=messages,
            tools=TOOLS, max_tokens=8000)
        # ... ツール実行 ...
        context = update_context(context, messages)
        system = get_system_prompt(context)
```

各ループ反復の開始時に system prompt を取得。context が変わっていれば再組み立て、変わっていなければキャッシュを返却。

---

## s09 からの変更点

| コンポーネント | 変更前 (s09) | 変更後 (s10) |
|-----------|-------------|-------------|
| prompt | ハードコード SYSTEM 文字列 | PROMPT_SECTIONS + assemble_system_prompt |
| キャッシュ | なし | get_system_prompt（json.dumps 検出 + キャッシュ） |
| 新規関数 | — | assemble_system_prompt, get_system_prompt, update_context |
| ツール | bash, read_file, write_file (3) | bash, read_file, write_file (3) — 変更なし |
| ループ | 固定 SYSTEM を使用 | get_system_prompt(context) を使用 |

---

## 試してみよう

```sh
cd learn-claude-code
python s10_system_prompt/code.py
```

**安全上の注意**：このスクリプトはモデルが生成した `bash` 文字列を `shell=True` で実行し、s03 の permission gate を含まない。破棄可能な workspace でのみ実行すること。

観察のポイント：

1. 出力にロードされたセクションが表示される（`[assembled] sections: ...` ラベル）
2. 継続会話でキャッシュヒット時は `[cache hit]` と表示
3. `.memory/MEMORY.md` を作成すると、次のターンで memory セクションが自動ロード

以下のプロンプトを試してみてください：

1. `Read the file README.md`（常にロードされる 3 つのセクションを観察）
2. `Create a file called .memory/MEMORY.md with content "- [test](test.md) — test memory"`（記憶インデックスを書き込み）
3. `Read the file code.py`（memory セクションが表示されるか観察）

---

## 次へ

モデル入力を実行時に組み立てられるようになった。しかし Agent はエラーでまだクラッシュする。ネットワークの不安定性、API レート制限、出力の切り詰め、コンテキスト超過、これらはバグではなく日常。

s11 Error Recovery → 4 つのリカバリパス。token のアップグレード、コンテキスト圧縮、指数バックオフ、モデル切り替え。

<!-- translation-sync: zh@v1, en@v1, ja@v1 -->

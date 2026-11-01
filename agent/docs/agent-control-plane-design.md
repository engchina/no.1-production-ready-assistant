# Agent Platform 設計

## 1. Positioning

業務・業界の Agent を作り、Skill と MCP（RAG・NL2SQL・その他）を安全に使わせる製品。業務 Agent の定義と、
その実行（組み込み Runtime）、共通の Run・Event・Artifact・Approval・Audit を 1 つの製品で持つ。

#754 で、外部の Runtime（OpenClaw / Hermes / DeerFlow）への Binding による配置をやめ、Control Plane の中の
組み込み Runtime（OpenAI Agents SDK + モデルの API〔OpenAI 互換〕の Responses API）で実行する形にした。
今後の作り変えは「Agent Platform 再設計案」（2026-10-02）に沿う。

```mermaid
flowchart TD
  K["Skill（SKILL.md・references/・宣言・画面）"] --> R["Registry"]
  R --> S["Skill"]
  S --> T["ツール（RAG / NL2SQL / 外部 MCP）"]
  A["業務 Agent（指示・Skill・モデル）"] --> S
  A --> X["Run"]
  X --> B["組み込み Runtime（OpenAI Agents SDK）"]
  B --> O["モデルの API（OpenAI 互換・Responses API）"]
  B --> T
```

## 2. Domain contracts

### Business Agent

`AgentProfile` の正式な編集対象は `name / description / instructions / skill_ids / model_id / data_scopes / enabled`。

データの範囲（`data_scopes`。#1378）: 標準の MCP 接続（`nl2sql` / `rag`）ごとに、使えるプロファイルの ID の一覧と既定
（`AgentDataScope`: `profile_ids` / `default_profile_id`。1 つなら既定はそれ、複数なら一覧の中の 1 つ）を持つ。一覧が空の
接続は「範囲なし」で、今までどおり Run の利用者が使えるすべてのプロファイルからモデルが選ぶ。範囲は版に入る（下書き → 公開）。
保存は Agent と同じく snapshot（`AGENT_RUNTIME_CHECKPOINTS`）で、システムテーブルの DDL は変えない（古い snapshot は空の範囲で
読める）。

- 編集画面の候補は `GET /agent-data-scopes/{接続}/candidates`（Agent 管理）。MCP の `nl2sql_list_profiles` /
  `rag_list_search_answer_profiles` を**編集者**のサービストークン（`sub` = 編集者）で上限の件数まで呼ぶので、編集者が
  使えるものだけが出る。`POST /agents`・`PATCH /agents/{id}` は、範囲に**新しく加えた** ID を同じ一覧で確かめ、
  無ければ 400（`agent_data_scope_forbidden`）、一覧を取れなければ 409 / 502（保存しない）。既に範囲にある ID と範囲を
  外す変更は確かめない（別の編集者が加えた ID を残したまま、ほかの項目を保存できる）。
- 組み込み Runtime の強制（`features/agent/data_scope.py`。tool handler で、モデルの指示に頼らない）:
  - 1 つ: `nl2sql_query` の `profile_id`・`rag_search` / `rag_retrieve_evidence` / `rag_lookup_guides` の
    `search_answer_profile_id` を Runtime が埋め、モデルが渡した値は上書きする。モデルに見せる schema からその引数を除く
    （`tool_registry.invoke` に渡す定義は呼び先の契約のまま）。
  - 複数: 範囲外の値はツールのエラー（`agent_data_scope_violation`。使える ID と既定を返す）で、呼び先へ送らず予算にも
    数えない。値が無ければ Agent の既定（NL2SQL の `"default"` には落とさない）。schema の説明に使える ID を足す。
  - 一覧（`nl2sql_list_profiles` / `rag_list_search_answer_profiles`）は呼び先に上限の件数を求め、範囲で絞ってから
    モデルが求めた件数に切る。推薦（`nl2sql_recommend_profile` / `rag_recommend_search_answer_profile`。#1850）は
    候補の `profile_ids` を範囲で埋め、推薦が範囲外なら `null`、候補も範囲で絞る。
  - RAG で範囲を設定したら `knowledge_base_ids` を使わない（schema から除き、渡されたら外す）。
  - 記録: step の `tool_call.data_scope`（接続・使える ID・使った ID・モデルが求めた ID・`filled` / `overridden` /
    `kept` / `rejected` / `filtered`・使わなかったナレッジベース）と、絞った件数（`tool_result.audit_metadata.data_scope`）。
    承認待ちの呼び出しも、実行するときと同じ（範囲を当てた）引数で承認に出す。モデルへの指示に「データの範囲」の節を足す。
  - Agent の範囲は利用者の権限を広げない（呼び先は Run の利用者の権限で判定する。積で効く）。
  - 推薦（`nl2sql_recommend_profile`）の候補の一覧（`profile_ids`）は Runtime が範囲で埋める（モデルが範囲の中の候補を
    渡していれば、その中に絞る。schema から除く）。
- 呼び先でも強制する（多層の防御。#1379）: Run のツールの呼び出し（回答の最終の検証の `rag_validate_answer` を含む）の
  サービストークンに、その接続の範囲の ID を claim `profile_ids` として入れる（`ToolInvocationContext.profile_scopes`。
  範囲の無い接続・外部の MCP には付けない。`tools/list` には付けない）。RAG / NL2SQL は「利用者の権限 ∩ claim」で判定し、
  範囲外は 403（`PROFILE_SCOPE_FORBIDDEN`）にする（platform の `docs/backend-standard.md`「製品間の連携」）。
- 範囲の無い Agent でモデルが `nl2sql_query` の `profile_id` を渡さない・使えない ID を渡したとき、`rag_search` /
  `rag_retrieve_evidence` に見つからない `search_answer_profile_id` を渡したときは、呼び先が選ぶ（使えるものが 1 つなら
  それ、そうでなければ質問の推薦。結果の `auto_selected_profile` / `auto_selected_search_answer_profile`。決まらなければ
  候補つきのエラー。NL2SQL は `"default"` には切り替えない。#1379・#2355）。

版（#770）: 名前・説明・指示・Skill・モデルは「下書き」で、`POST /agents/{id}/publish` で版（`AgentVersion`: 版の番号・
内容・公開日時・公開者・メモ）を作り、`published_version` にする。通常の Run は公開中の版の内容で実行し
（`RunState.metadata.agent_version`）、公開していない Agent の Run は 409（`agent_unpublished`）。Agent 管理
（admin）は `RunCreateRequest.draft=true` で下書きを試せる（`agent_version="draft"`）。
`POST /agents/{id}/versions/{version}/restore` はその版を公開し直し、下書きもその内容にする（ロールバック）。
画面・API で作る Agent は下書きから始め、#770 より前の Agent（`versioned=false`）は読み込み時に現在の内容を
v1 として公開する。
公開の版は使うスキルの中身（`AgentVersion.skills` の `PinnedSkill`: 名前・説明・指示・MCP のツール・有効・参照文書の
索引と本文の hash・中身の hash `content_hash`）を固定する（#1855）。公開中の版の Run（評価の「公開中の版」を含む）は
固定した中身で動き（`_agent_for_run` が `pinned_skills` を入れ、組み込み Runtime が `use_pinned_skills` /
`resolve_skill` で指示・ツール・参照文書の索引を引く）、下書きの Run は今のスキルで動く。参照文書の本文は固定せず、
今の本文を読む。固定した後に中身が変わった・消えたスキルは `skill_updates` に出し（「スキルが更新されています」）、
公開していない変更に数える（再公開で取り込む）。組み込みのスキル（`skills/<name>/SKILL.md`）も同じく固定する
（配備で更新されたら、評価してから再公開する）。固定していない版（#1855 より前・起動時に作った v1。`skills=None`）は
今のスキルで動き、画面で公開して固定することを案内する。
スキルの更新の取り込み（#1867）: `GET /agents/{id}/skill-updates` が固定したスキルと今のスキルの差（指示の unified diff・
説明・ツール・参照文書）を返す。`POST /agent-skill-updates/publish`（Agent 管理）は選んだ業務 Agent に、公開中の版の項目のまま
スキルの今の中身を固定し直した新しい版を公開する（下書きの変更は公開しない。取り込めない業務 Agent は `skipped` と理由）。
`POST /agent-skill-updates/rollback` は公開中の版だけを前の版に戻す（下書きは変えない。画面は Toast の「取り消す」）。
業務 Agent の `skill_policy`（版の項目）は `pinned`（既定。公開の版に固定）か `latest`（公開中の版でも今のスキルで動き、
更新の印を出さない。開発・検証用）。画面は業務 Agent の一覧の「スキルの更新があります（N 件）」と確かめて選んで再公開する
side sheet、詳細の差分・「スキルの更新だけを公開」・品質評価への導線（`/evaluation?agent=<id>`）。
利用者の Run を作る入口（チャットの選択肢）は、
公開した版の無い Agent を出さず・選ばせない（`runtime.agent_unavailable_reason`。#792）。品質評価は下書きでも評価できる。
品質評価（#810）は評価の開始で「公開中の版」か「下書き」を選ぶ（`EvaluationRequest.agent_version`。省略時は公開して
いない変更があれば下書き、なければ公開中の版。公開した版が無ければ下書きだけ）。job は `agent_version`（版の番号か
`"draft"`）を残し、公開中の版は始めたときの版に固定して Run を作る（`create_builtin_run(agent_version=...)`。Run の
`metadata.agent_version` は #770 のまま）。前回との比較は同じ評価セット・同じ評価ケース（質問・期待・期待するツール）の
完了した job とだけ行い、比べた版（`previous_agent_version`）と日時を返す。
評価ケースは期待するツールの順（`expected_tools_in_order`。各ツールの最初の呼び出しの順で比べ、先頭が最初に呼ぶツール）を
任意で持ち、ツールの選択の判定に使う（`tool_order_correct`。#1845）。判定のモデルの応答が出力の上限で止まったら、上限を
広げて（広げられなければ同じ上限で）1 回だけ送り直し、それでも止まれば「判定できなかった」（`judge_failed`）にする。
判定できなかったケースは回答の誤りではないので、合格率の分母から除く（`summary.unjudged`）。
評価セットは評価セットの業務 Agent 以外でも評価できる（`EvaluationRequest.agent_id`。対象範囲・実行できる状態は評価する
業務 Agent で確かめる。#1845）。前回との比較は同じ評価セット・同じ業務 Agent・同じ評価ケースの job だけで、業務 Agent・版を
またいだ比較は `GET /evaluations/compare?base_job_id=&target_job_id=`（同じ質問のケースどうしで並べ、合否で改善・悪化・
変化なし、片方にしか無いケースは `base_only` / `target_only`）。
評価ケースは問い返し（回答前の確認）への答え（`clarification_answers`。共通の `ClarificationCard` の
`{ itemId, optionIds, otherText }` と同じ形の `item_id` / `option_ids` / `other_text`）を任意で持つ。Run が問い返しで
止まったら（支援タスクの状態の `pending_clarifications`）、確認事項ごとに同じ `item_id` の答え、無ければ `item_id` の
空の答えを順に当て、利用者がチャットで答えるのと同じ文を同じ会話（`thread_id`）の次の Run で送る（NL2SQL の
`requirement_session_id`・`answer`、RAG の `conditions` への写しは組み込み Runtime の確認の扱いに任せる）。往復は
3 回まで（`EVALUATION_CLARIFICATION_ROUNDS_MAX`。回答前の確認の往復の上限の既定と同じ）で、最後の Run の回答を、送った答えを添えて判定する。結果は
`clarification_rounds`（往復ごとの Run・確認事項・答え・送った文）と `ended_with_clarification` に残す。確認事項には
当てた答え（`selected` は選んだ候補の表示名、`other_text`）も残し、ケースの詳細は 1 往復を共通の `ClarificationRecord`
（`variant="disclosure"`。回答前の確認の履歴と同じ表示）で出して、送った文をその下に添える（#1861）。
業種テンプレートから作った Agent は `template_id` を持ち（#810。以前の Agent は空のまま読める）、
`POST /evaluation-sets/from-template` でテンプレートの評価ケースの評価セットを作れる。フィードバック・Run の詳細の
「評価ケースに追加」は `GET /runs/{id}/evaluation-case`（質問・管理者のコメント・呼んだツールの下書き）と
`POST /evaluation-sets/{id}/cases`（同じ質問・50 件の上限は 409。出どころの `source_run_id` を残す）を使う。
MCP、Tool、Runtime を Agent に埋め込まない。業務 Agent はツールの項目を持たず、作成・変更の API はツールの直接の指定
（`tool_names` など）を 422 で拒む（#3240。保存済みの旧い値は読むときに無視する。`command_allowed_prefixes` は #756 で削除した）。

### Skill

Skill は AgentSkills 互換の指示本体であり、次の内部依存を持つ。

```json
{
  "id": "business-rag-research",
  "instructions": "...",
  "mcp_requirements": [
    {"server_id": "rag", "tool_names": ["rag_search", "rag_list_search_answer_profiles", "rag_read_source"]}
  ],
  "references": [{"path": "references/guide.md", "characters": 1200}]
}
```

`server_id` は MCP 接続の ID（`control-plane` は Control Plane のツール）。`tool_names` が空なら接続のすべての
ツールを使う。組み込み Runtime は、Agent の選択 Skill が要求するツールの和集合だけをモデルに渡す（指示への列挙だけで
権限を表現せず、渡すツールの一覧を実体として絞る）。

組み込みの Skill と初期の業務 Agent（#1573）:

| Skill の ID | 名前 | 使うツール |
|---|---|---|
| `business-rag-research` | 業務 RAG 調査 | `rag` の検索・読み取り |
| `structured-data-query` | 構造化データ照会 | `nl2sql` の照会・ジョブ・業務プロファイル |
| `rag-and-structured-data` | 資料とデータの横断調査 | `rag_search` と `nl2sql` の照会 |

組み込みの Skill は Agent Skills の形式（agentskills.io）で、repo 直下の `skills/<name>/`（3 製品のどれにも属さない）に
置く（#1816）。Agent は起動時に `skills.register_builtin_skills()` でこのフォルダを読み（コードに指示・説明・ツールを
持たない。正本は `SKILL.md`）、同じフォルダを Claude Code・Codex などの外部の Agent にもそのまま入れる。

- `SKILL.md` の frontmatter は仕様のフィールドだけ: `name`（ID。小文字・数字・ハイフン、フォルダ名と同じ）・
  `description`（何をするか＋いつ使うか。外部の Agent がスキルを選ぶ手がかり）・`compatibility`（要る MCP 接続）・
  `license`。本プロダクトの拡張は `metadata`（文字列 → 文字列）に置く:
  `production-ready-display-name`（画面の名前）・`production-ready-mcp-tools`（`<接続>:<ツール>` の空白区切り。
  `mcp_requirements` になる）・`production-ready-tags`。`AGENT_SKILLS_DIR` のスキルも同じ読み方をする。
- 本文は 500 行以内。目的の文（MCP サーバー名と、ツールは素の名前で書くこと。Agent によって前置きが付く）の後の見出しは
  すべて処理順の `## Step N: …`（1 から連番。入れ子の Step を置かない。root の `docs/agents/prompts.md`。#3830）で、
  `入力の種類を分ける` → 手順の Step → `結果ごとの対応` → `回答の書き方` → `してはいけないこと` の順。使う Step だけを
  置く。`入力の種類を分ける`（#1929）は入力をあいさつ・お礼・その他の質問でない入力と質問に分け、質問でなければツールを
  呼ばずに用件（スキルの対象について何を知りたいか）を聞き、質問（範囲外を含む）とあいさつの混ざる質問は手順の Step に進める
  （用件を聞く返事は組み込み Runtime の拒答・差し戻しの対象外。`answer_passages.asks_for_request`）。長い参照は
  `references/*.md` に出し、本文から 1 段の深さでリンクする（Agent は索引だけを指示に入れ、`skill_reference_read` で
  読ませる）。毎回の手順で要る短い内容は本文に置く（`business-rag-research` の根拠の読み方（約 1,000 字）は参照文書に
  出すと #1816 の評価の 120 Run で 1 度も読まれず、本文の Step 4 に戻した。#1846）。
- 仕様の検証は公式の `skills-ref`（`tests/test_skill_md_spec.py`）。`metadata` のツールが RAG / NL2SQL の MCP の契約
  （`platform/contracts/mcp/`）にあることも確かめる。
- 外部の Agent へ配る形: repo 直下の `.claude-plugin/marketplace.json`（marketplace `production-ready-assistant`）が
  `skills/` を plugin `production-ready-skills`（`skills/.claude-plugin/plugin.json`）として載せる。入れ方・MCP の設定
  （名前は `rag` / `nl2sql`、API キー）・つまずく所は [skills/README.md](../../skills/README.md)（#1846 で通しで確かめた
  手順）。配った後にスキルを変えたら `plugin.json` の `version` を上げる。
- 旧 ID（`business_rag_research` など。#1573 の `rag_then_structured_data` も）の保存済みの業務 Agent は、システム
  テーブルの migration `20261014_013_rename_builtin_skill_ids` が新しい ID にする（読み込みで読み替えるコードは持たない）。
- 手順の形は作業に合わせる: `business-rag-research` は順番が意味を持つ多段の質問なので一直線の手順の Step
  （各 Step に呼ぶツールと「→ Step N」の行き先）、`rag-and-structured-data` は順序を決めない判断のループ（足りないものを
  判断 → ツールを呼ぶ → 結果を確かめて戻る）の Step、`structured-data-query` は短い番号付きの
  手順を 1 つの Step に置き、`status` ごとの対応を `結果ごとの対応` の Step に置く。
- `business-rag-research` は、元の指示の順（根拠を集める段の決まり → 多段の手順 → 検索の前の確認）を保ち、
  `rag_search` を独立の Step の見出しにしない。実モデル（`openai.gpt-oss-120b`）の比較で、`rag_search` の Step を
  分岐の先に置いた並びでは、多段の質問を `rag_search` から始めて 2 段目を引かない回答が増えた（#1795）。
- ツールの結果の値（`outcome` / `status`）→ すること の対応を `結果ごとの対応` の Step の箇条書きに
  まとめ、ツール名・フィールド名は `code` にする（組み込み Runtime は `code` の中の素のツール名も
  `<接続>__<ツール>` に書き直す）。
- 書き方は root の `docs/agents/prompts.md`（プロンプト（LLM への指示・skill・ツールの説明）の書き方）に従う（#2269）。使うツールの
  一覧は metadata とモデルに渡すツールの定義にあるので本文に節を置かず、引数の規則は MCP の引数の説明に任せる。
  スキルは外部の Agent にも配るので、本文の `code` の識別子は MCP の契約（`platform/contracts/mcp/`）のツール・引数・
  結果のフィールドと値だけにする。組み込み Runtime だけが結果に足すもの（`next_step` とその中の案内・
  `rag_calls_remaining`）は個別に説明せず（各案内が `next_step` に次の手を持つ）、「組み込みの Agent では」の行に
  まとめる（`tests/test_skill_md_spec.py` の契約のテスト）。
- 規則の文を書き直すときは、前後の規則を突き合わせて意味を落とさない・足さない。スキルの詳細画面は指示を Markdown
  として表示する。

`rag-and-structured-data` は順序を決めない。各ステップで、資料の根拠が要るなら `rag_search`、業務データの値が要るなら
`nl2sql_query` を選び、一方の結果で分かった事実・条件・識別子で、もう一方や同じツールを繰り返し呼ぶ。根拠がそろったら
止める。旧 ID `rag_then_structured_data`（RAG の後に NL2SQL の固定の順序）は、保存済みの業務 Agent とその版の
読み込みで新しい ID に置き換える（`skills.RENAMED_SKILL_IDS`）。

| 業務 Agent の ID | 名前 | Skill |
|---|---|---|
| `default`（既定） | 資料・データ横断アシスタント | `rag-and-structured-data`・`business-rag-research`・`structured-data-query` |
| `rag-research` | 資料調査アシスタント | `business-rag-research` |
| `data-query` | データ照会アシスタント | `structured-data-query` |

3 つは builtin（`runtime.BUILTIN_AGENT_IDS`）で、削除できず、保存済みの snapshot に無ければ読み込みで足す（v1 を公開した
状態）。#1573 より前の `default`（名前が「汎用業務 Agent」のまま）は、新しい内容を次の版として公開する（前の版は残す。
名前を変えた `default` は利用者の内容として残し、Skill の ID だけを置き換える）。

### Skill の参照文書（#862 / #1803）

Agent はプラグインを持たず、スキルだけを持つ（#1798 でマーケットプレイス、#1803 でプラグインを削除）。
スキルは Agent Skills の形（ディレクトリ + `SKILL.md`）で、`AGENT_SKILLS_DIR/<id>/SKILL.md` と同じディレクトリの
`references/**` のテキスト / Markdown（`.md` / `.markdown` / `.txt`）を参照文書（`references`）として読む
（`skills_loader.load_skill_references`）。

- symlink（ファイル・ディレクトリ・`references/` 自体）は辿らず、解決したパスが `references/` の外なら読まない。
- 上限は 1 スキルで 100 ファイル・1 ファイル 1 MiB・合計 4 MiB。超えた分は読まずに warning を出す。
- `scripts/` などの同梱のコード・その他のファイルは読まない・実行しない。
- API の応答・保存にはパスと文字数だけを出す（本文は `exclude`）。
- 組み込み Runtime は本文を指示へ詰めず、パスの索引だけを入れ、その Skill を割り当てた Run にだけ
  `skill_reference_read`（`skill_id`・`path`・`offset`）を渡して 8,000 文字ずつ読ませる。読み取りも
  `tool_registry.invoke` のポリシー・監査を通す。
- 画面・API・`AGENT_SKILLS_DEFINITIONS_JSON` で作るスキルは参照文書を持たない。

### 組み込み Runtime（#754）

- `features/agent/builtin_runtime.py`。SDK は `openai-agents`（`pyproject.toml` で版を固定）。
- モデル: Agent の `model_id`（空なら「システム設定 > モデル」の既定のテキストモデル）と、その接続（Endpoint・API key・
  Project ID）。`OpenAIResponsesModel` に接続の OpenAI 互換の base URL を渡す。xAI のモデルは空の `tools` を
  拒否するため、ツールが無い呼び出しでは `tools` / `tool_choice` を送らない（`ModelApiResponsesModel`）。
- 指示: 共通の前置き（日本語・根拠・推測しない）＋ Agent の指示 ＋ 割り当てた Skill の指示。
  Skill の指示は MCP のツールを素の名前（`rag_search`）で書いてよい。Run を作るときに、Skill の requirement が
  名前で宣言し、この Run でモデルに渡すツールの名前（`rag__rag_search`）へ書き直す（Agent の指示・支援タスクの指示も同じ。
  同じ素の名前が複数の接続にあって 1 つに決まらないものは書き換えない。#1303）。
- 無いツールの呼び出し: モデルが渡していない名前のツールを呼んでも Run を落とさない（SDK の
  `tool_not_found_behavior="return_error_to_model"`）。モデルへは「呼ぶときの名前は `<接続>__<ツール>`」
  （候補が複数なら候補の一覧）を返して呼び直させる。Control Plane が素の名前を別のツールへ読み替えて実行することは
  しない（呼び出し・承認・監査はモデルが呼んだ名前のまま。#1303）。
- 失敗したツールの結果: モデルへは `error`・`error_code` に加えて、MCP の呼び先の誤りの code（`tool_error_code`。
  `cursor_invalid`・`MCP_TOOL_ARGUMENTS_INVALID` など）と、引数の検証の違反（`argument_errors`。呼び先が返した
  場所と文だけ、最大 5 件・文字数に上限）を返し、引数を直して呼び直せるようにする（`tool_failure_payload`。#2498）。
  型の名前・元の文・入力の値・接続の情報は渡さない（step には呼び先の詳細をそのまま残す）。
- ツール: Skill の requirement が要求するツールを `FunctionTool` にする。`control-plane` は `tool_registry` の
  ツール、それ以外は MCP 接続の `tools/list`（Run の利用者として取得。名前は `<接続>__<ツール>`、英数字・`_`・`-`
  で 64 文字以内）。文字の置き換えで同じ接続の別のツールと同じ名前になるもの（`read.item` と `read_item`）は、置き換えた
  側に元の名前の hash を付けて分ける（置き換えの要らない名前は変えない。一覧の順に依らない）。それでも別の出自のツールが
  同じ名前になれば、どれも渡さずに Run に warning を残す（一覧の順で呼べる対象を変えない。#2596）。取得できない接続は飛ばして Run に `runtime.event`（warning）を残す（#757）。
  実行は `tool_registry.invoke`（ポリシー・ガードレール・監査・成果物、サービストークン）。
  ポリシーの「拒否」は渡さず、「承認」は `needs_approval=True`（MCP のツールは `readOnlyHint` が無ければ既定で承認）。
- 承認: SDK の中断（`result.interruptions`）で承認待ちの step と ApprovalRequest を作り、`result.to_state().to_string()`
  を Run の metadata（`_builtin_sdk_state`）に保存して `waiting_approval` にする。すべて決まると `queued` に戻り、
  状態を復元して承認・却下を反映し再開する。承認済みのツールは中断時の step を実行中にして結果を記録する。
  無人の Run は承認待ちにしない（`_unattended_approvals`）: 品質評価の Run（`evaluation_dry_run`。#776）は承認が要る
  ツールを実行せずに dry-run の step を残し、外部の MCP クライアントの Run（`metadata.source=mcp`。#1874）は
  承認が要るツールを呼ばない（無人の実行は事前に許可したツールだけを使う）。どちらも SDK の却下（`rejection_message`）で
  モデルへ理由を返して回答まで続ける。MCP では取り消しの step（`error_code=mcp.approval_required`、監査ログに出る）と
  Run の経過のイベント（warning）を残す。定時実行（スケジュール）は #20 で削除した。
  `_` で始まる key と Control Plane の予約の key（`RESERVED_RUN_METADATA_KEYS`。評価・MCP・再実行の印）は、
  利用者が `POST /api/runs` の `metadata` で付けると 422 にする（再開の状態や出所を偽らせない。#1130）。
- Run 全体の実行予算（#68。`run_budget.py`）: 1 回の SDK 実行の `max_turns`・ツールの回数（`SupportTaskBudget`）・
  通信の timeout とは別に、最初の実行・差し戻し（根拠・手順・章節・出力の項目）・承認や確認の後の再開を通した
  モデルの呼び出しの回数・token・稼働時間を 1 つの予算で数える（`AGENT_RUN_MAX_MODEL_CALLS` /
  `AGENT_RUN_MAX_TOTAL_TOKENS` / `AGENT_RUN_MAX_ACTIVE_SECONDS`。0 以下は数えない＝既定）。
  - 予約と精算: `call_model_input_filter` で呼ぶ前に 1 回と入力の見積もりを予約し、`on_llm_end` で usage の実測に
    精算する。usage の無い呼び出しは見積もりを消費にし、`unmeasured_calls`・`tokens_exact=false` で区別する。
    出力の分は予約しないので、token は最後の 1 回の出力の分まで上限を超えうる。料金の上限は扱わない。
    RAG / NL2SQL の内部のモデルの消費はこの Run の usage に入らない（二重に数えない）。
  - 稼働時間は実行（開始・再開）ごとの計算・通信の時間だけを足す（承認・確認の待ちは数えない）。消費は
    Run の metadata `run_budget`（予約の key。利用者は書けない）に残し、再開で 0 に戻さない。
  - 上限に達したら、新しいツールの呼び出しを実行せず（step を作らず未実行として経過に残す。結果を読むモデルの
    呼び出しが残らない場合も含む）、次のモデルの呼び出しの前に `runtime.run_budget_exceeded` で失敗にする。
    失敗の文は決まった文で、そのためにモデルを呼ばない。実行中の呼び出しは中断しない。
  - 報告（`run_budget`）: `limits`・`consumed`（実測・見積もり・欠測の回数・稼働・実行の回数）・`remaining`・
    `exceeded`（scope・limit・used・止めた所 `model_call` / `tool_call`）・`operations`
    （`not_executed`＝予算で実行しなかった呼び出し、`unknown_result`＝結果の無い step。取り消されたとはみなさない）。
- tracing: `set_tracing_disabled(True)`（業務データを外部へ送らない）。
- テスト: SDK の `agents.testing.ScriptedModel` でモデルを台本にする（`tests/test_builtin_runtime.py`）。

### RunState

共通 Run は状態/Event/Artifact/Approval/Audit と `runtime_id`（新しい Run は `builtin`）を持つ。
`POST /api/runs` は `agent_id` と `goal` だけを受け取り、組み込み Runtime で実行する。モデルの最終回答は
`kind="answer"` の Artifact に残す。

## 3. ツールとポリシー

Control Plane のツール（`echo`）は `tool_registry` に登録する。MCP 接続のツールは登録せず、
Run ごとに `tools/list` から作ったツール定義を `tool_registry.invoke` に渡し、ToolPolicy、PII/secret masking、
audit metadata、成果物の保存を再利用する（#757）。外部 Runtime 向けの Binding MCP（`/api/mcp/{binding_id}`）
と adapter 契約（`probe_capabilities / sync_binding / submit_run / ...`）は #754 で削除した。

## 4. 製品の MCP

### 4.1 MCP 接続（RAG / NL2SQL / 外部 MCP。#233 / #757）

RAG / NL2SQL / 外部 MCP は「MCP 接続」（`config.McpConnectionConfig`、API `/api/settings/mcp-connections`）で
管理する。RAG / NL2SQL は標準の接続 `rag` / `nl2sql`（各製品の `POST /api/mcp`。認証はサービストークン、
aud は製品名。削除できない）。表示名は「ナレッジ検索（RAG）」「データ問い合わせ（NL2SQL）」で変えられない（#1325）。
URL は `AGENT_EXTERNAL_RAG_MCP_URL` / `AGENT_EXTERNAL_NL2SQL_MCP_URL` で、配備がこれを与えた接続は `base_url_locked`
になり、画面・API で URL を変えられない（PATCH は 400。保存した URL・名前は起動時の復元でも使わない）。環境変数が空の
構成（ローカルの開発・その製品を配備しない構成）だけ、画面で URL を設定して保存できる。外部の MCP は画面・`AGENT_EXTERNAL_MCP_SERVERS_JSON` で追加し、
認証方式は なし / API キー / OAuth client credentials / サービストークン。ツールは呼び先の契約（RAG / NL2SQL は
`platform/contracts/mcp/`）をそのまま使い、Agent は引数を作り変えない。

| ツール（モデルに渡す名前） | readOnlyHint | 既定の policy |
|---|---|---|
| `rag__rag_search` / `rag__rag_list_search_answer_profiles` / `rag__rag_read_source` | true | 承認なし（`rag_search` は回答生成に LLM を使う） |
| `nl2sql__nl2sql_list_profiles` / `nl2sql__nl2sql_recommend_profile` / `nl2sql__nl2sql_get_job` | true | 承認なし |
| `nl2sql__nl2sql_query` | false | 承認なし（#1832。標準の接続の既定。呼び先は SELECT だけを実行し、見える範囲は業務プロファイル・利用者の権限で決まる。ツール権限で ask にすれば承認を求める） |
| `nl2sql__nl2sql_cancel_requirement` | false | 承認が必要 |

RAG のチャットは MCP で提供しない（#787）。RAG の MCP は検索（`rag_search`）・根拠の読み取り（`rag_read_source`）・
検索・回答プロファイルの一覧だけを持つ。`rag_search` の根拠は `evidence[]`（場所 `locator` の節・頁、版 `chunk_set_id` /
`recipe_id`、回答に使ったか `used_in_answer`、切り詰めの有無 `truncated`）で返し、回答に使った根拠を先にして `evidence_limit`
件まで返す。切り詰めた本文と親の本文は `rag_read_source`（検索と同じ見え方の条件。見えない根拠は `source_not_found`、古い版は
`source_stale`）で読む（#1219）。

**回答の根拠（#2328）**: 組み込み Runtime は回答を確定するときに成果物 `answer_evidence`（「回答の根拠」。
`features/agent/answer_evidence.py`）を作る。AI に渡した根拠はその Run の `rag_search` / `rag_retrieve_evidence` の
`evidence`（chunk で重複なし）、回答で引用した根拠は、利用者に見せる回答が書いた根拠の ID（省いた形を含む。最終の検証の
並べ替えと同じ照合 `cited_chunk_ids`）と、最終の検証が段落を裏付けた（`supported`）根拠。ほかに、本文の根拠の ID の印と
引き当てた根拠（`markers`）と段落ごとの根拠（`passages`）を持ち、画面は印を根拠の行（「根拠：文書 p.N」）に置き換える。
チャット・実行の詳細は RAG と同じ共有の部品（`AnswerEvidence` / `AnswerSections`）で出し、MCP の `sources` も同じ判定
（回答で引用した根拠、無ければ全件）。Agent には本文の箇所を開く画面が無い（RAG の画面用の API は呼ばない）ので、根拠の行は
その根拠のカード（抜粋・場所・スコア）と原文のプレビュー（#2389）を右のシートで開く。
最終の検証が判定を出さなかった（時間切れ・失敗・無効）ときは、モデルを呼ばない決定的な照合（`binding: "text"`。段落の
特徴の照合 `evidence_match` と、出典の行・段落が書いた文書名と頁）で段落と根拠を結び付け、照合で確かめていないので回答での
使われ方を参考（`reference`）にする。画面は「資料により確認できた事項」を作らず、RAG と同じ「参考情報」の節（説明の文
「以下は資料の原文です。本質問の条件への該当は確認できていません。」・根拠の原文（`references`）・「根拠：文書 p.N」）で
出し、根拠に結び付いた平文の出典の行（`citation_lines`）は本文に重ねない（#2707）。検証がどの段落も時間の上限までに確かめ
終わらなかった（`timed_out`。成果物は `unvalidated`）ときも同じ。一部の段落だけを確かめた（`partial`。#2700）ときは
`binding: "partial"`: 確かめ終わった段落は判定のまま根拠の行を付け、確かめ終わらなかった段落（`unassessed`）は本文に残して
根拠の行を付けず、決定的な照合で結び付けた根拠を「参考情報」に出す（確かめ終わった段落の根拠は重ねない）。
最終の検証の段落の印（#2738。契約は `platform/contracts/answer-passages/passage-marks.json`）は、成果物の
`passage_marks`（本文に残した段落の原文 `text`・状態 `state`〔`unverified` 根拠で確かめられていない / `warning` 注意が必要。
理由が複数なら `unverified` を優先〕・理由 `reasons`〔`code`・利用者に見せる短い文 `label`・監査や規則の理由 `detail`〕）・
`removed_passages`（根拠と矛盾して外した段落と理由）・`answer_warnings`（回答全体の警告）が正本で、本文の文字列に印の記号を
埋め込まない。本文だけを読む外部の MCP クライアントのために、回答の本文の末尾に節「根拠と矛盾し除いた内容」（外した段落の
80 文字までの引用と理由）と「確認が必要な点」（回答全体の警告と、印の段落の引用と理由）を 1 回だけ、全件を同じ書式で書く
（「ほか N 件」で切らない。#2361）。成果物の `notice`（節の項目の数 `items`・本文の終わりの見出し `heading`・節の見出し
`headings`）で画面は本文と節を分け、`withheld`（`removed`・`unverified`・`warnings`〔警告の印の段落の数〕・`all`〔常に
false〕・`listed`〔節の項目の数〕）で件数を出す。MCP の `agent_chat`・`agent_get_run` も同じ 3 つを返す（出力の
`schema_version` は 3。回答の対応・根拠の ID は `docs/agents/mcp.md`）。
調べるのに使ったツール・プロファイル・内部の ID だけを述べる行（`answer_passages.is_lookup_note`）は、資料の主張ではないので
検証の前に回答から外す（末尾の節にも書かない。RAG の検証はモデルが `not_a_claim` にした本文の行を
`unassessed` に直すため〔主張を `not_a_claim` にして監査を逃れるのを防ぐ。#1306。その段落はその段落だけで 1 回監査し直し、
決まらなければ `classification_check: non_claim_mismatch` の `unassessed` のまま返す。#2625〕、Agent はモデルの判定ではなく決定的な
判定で外す）。RAG が監査の `supported` を採らなかった段落（根拠が別の対象の記載・写した原文が根拠に無い・監査の理由だけが根拠の
表と違う対応を述べた。`support_check`。#2659・#2653）は `unsupported`（原文が無いときは `citation_error`）で返り、Agent は本文に残して、
別の対象・表の対応は警告（`target_mismatch` / `value_unbound`）、写した原文が無いのは未確認（`quote_unmatched`）の印にする（#2738）。
段落が根拠の表の別の行・列の値を結び付けた（表が否定した対応）ときは、RAG が `contradicted`（`support_check=value_unbound`。理由に
表の正しい対応）で返し、Agent は矛盾として外す（#2998）。RAG が表の正しい値に直せた段落（`support_check=table_corrected`・
`revised_text`。#3005）は外さず、本文の段落を直した文に置き換えて「根拠の表の値に直した」（`table_corrected`。警告）の印を付ける。
業務ガイドの手順の説明は、RAG の `rag_validate_answer` の `guide_steps`（RAG の回答と同じ規則の手順ごとの充足。#2708）で
確かめる。説明を確かめられなかった必須の手順は、本文を残したまま回答全体の警告（`answer_warnings` の `guide_check`。`detail` は
RAG の finding の文）にして節「確認が必要な点」に示し、回答の対応の不足の印 `guide_steps`（conditional）にする。接続をまたいでは手順ごとに
最も良い状態を採る。支援タスクの状態の `guide.steps[].shared` は、手順の根拠がほかの手順と共有の chunk だけかを示す
（根拠の有無 `evidence` は共有の chunk でも true になるので、説明の確かめには使わない）。

ツール権限（`/settings/tool-policy`）は `<接続>__<ツール>` の名前で allow / ask / deny を上書きできる。
画面の一覧は `GET /api/settings/tool-policy/tools`（#1814。`tool_policy_catalog`）で、組み込みのツールに、URL を設定した
MCP 接続の `tools/list`（ログイン中の利用者として取る）を足し、「既定に従う」の判定（Run と同じ `ToolPolicy.decide`）を
添える。取れない接続は理由を出し、その接続のツールで保存したポリシーは「一覧にありません」の行として残す（保存し直しても
消さない）。各行に判定の理由（ツール権限で指定 / 標準の接続の既定 / 読み取り専用 / 注釈を信頼しない接続 / 既定の動作）を出す。

既定の判定（`ToolPolicy.explain`。#1832）は、ツール権限の個別の指定（deny → ask → allow）→ 標準の接続で Agent が承認なしと
決めたツール（`STANDARD_AUTO_APPROVED_TOOLS`。今は `nl2sql_query` だけ）→ 読み取り専用（Control Plane のツールの宣言、MCP の
ツールは注釈を信頼する接続の `readOnlyHint=true`）→ 未指定ツールの既定動作（承認を要求 / 拒否）の順。MCP 接続の
「ツールの注釈（readOnlyHint）を信頼する」（`trust_tool_annotations`）は、外部の接続（画面・API で追加した接続と
`AGENT_EXTERNAL_MCP_SERVERS_JSON` の宣言）では既定でオフ（#1875。新しい接続も、保存した値の無い既存の接続もオフ。
未リリースのため互換は持たない）。MCP の仕様は注釈を呼び先の自己申告とし、信頼しないサーバーの注釈を untrusted として
判断に使わないよう求めるため、オフの接続の `readOnlyHint=true` のツールは既定の動作（承認を要求 / 拒否）になる。管理者が
接続先とツールを確かめてから接続の画面でオンにし、オンにするときは確認のダイアログで「注釈を信頼すると、読み取り専用と申告した
ツールが承認なしで動く」ことを示す（オフに戻すのは確かめない）。標準の接続（RAG / NL2SQL）は Agent がツールの性質を知って
いるので常に使う（オフにできない）。`nl2sql_query` の `readOnlyHint` は false のまま（ジョブを作るので送信後の失敗では再送しない）。
既存の環境で `nl2sql__nl2sql_query` をツール権限の ask に入れていれば、その指定が優先する（消さない）。

承認の画面の「承認して今後は自動で実行」（#1832）は、その呼び出しを承認し、ツール権限でそのツールを allow にする（ask
から外す）。すべての業務 Agent・利用者の実行に効くので、確認のダイアログで示す。押せるのはツール権限を変えられる Agent 管理
（`agent.admin`）だけ（ほかは 403）。Run のイベント（`approval.decided` の `always_allow=true`）とログ
`agent_tool_policy_always_allow` に残す。同じ Run の再開から新しいポリシーで実行する。
NL2SQL の SQL に書き込みの文があれば `nl2sql.non_readonly_sql_returned_as_audit_only` の警告を残す（実行はしない）。

- **利用者**: Run の作成時に、ログイン中の利用者（Cookie のセッション。local mode ではローカル利用者）の
  `user_uuid` を `RunState.created_by_user_uuid` に記録する（checkpoint の JSON に入る。項目がない既存の Run は
  None）。Run からのツール呼び出し
  （承認後の再実行を含む）は `ToolInvocationContext.user_uuid` / `run_id` にこの値を入れるため、承認者ではなく
  Run を作った利用者として呼ぶ。再実行（replay）の Run は、再実行を指示した利用者になる。単発の
  `POST /tools/invoke` と画面の「ツールを取得」（`GET /settings/mcp-connections/{id}/tools`）は、ログイン中の利用者として呼ぶ。
- **token**（サービストークンの接続）: 呼び出しごとに `issue_service_token(PLATFORM_SERVICE_TOKEN_SECRET, subject=<Run の利用者 or
  ログイン中の利用者>, audience=<接続の audience>, issuer="agent", claims={"run_id", "agent_id", "run_source"})`（HS256、60 秒）を
  作り、`Authorization: Bearer` で送る。`run_source` は Run のきっかけ（`run_facts.run_source`。品質評価の Run は
  `evaluation`）で、呼び先（NL2SQL）の実行履歴が品質評価の実行を既定で隠すために使う（見え方だけ。#1986）。署名鍵は 3 製品の共通 `.env` で同じ値にする（32 文字以上。未設定なら
  `mcp.service_token_not_configured`）。利用者の無い呼び出しは別の主体に置き換えず `mcp.user_required` で失敗する
  （#3251。confused deputy を防ぐ）。サービスの主体で呼んでよい入口は無く、将来要るなら `sub` を替えず、呼び先に見える
  別の主体（RFC 8693 の `act` など）として足す。
- **MCP の手順**: 呼び出しごとに `initialize`（`2025-06-18`）→ `notifications/initialized` → `tools/call`。
  応答の `Mcp-Session-Id` を以降の request に付け、`Accept: application/json, text/event-stream` と
  `MCP-Protocol-Version` を送る。結果は `structuredContent`（無ければ text の `content`）、`isError: true` は
  `mcp.tool_error`（呼び先の `error_code` / `message` / `status` を `error_details` に入れる）。SSE の途中経過と
  paging は扱わない。すべての接続が同じ client（`McpConnectionClient`）を使う。固定の session id を設定した接続には
  `initialize` を送らず、`initialize` を持たない接続（`-32601`）はそのまま `tools/*` を呼ぶ。
- **再試行と期限（#854）**: 1 メッセージを `AGENT_EXTERNAL_MCP_MAX_RETRIES`（既定 3）回まで再試行する。分類は MCP の意味に合わせる。

  | 失敗 | 手順（`initialize`・`notifications/initialized`・`tools/list`） | 読み取り専用（`readOnlyHint=true`。注釈を信頼する接続だけ）の `tools/call` | それ以外の `tools/call` |
  |---|---|---|---|
  | 送信前（`ConnectError`・`ConnectTimeout`・`PoolTimeout`）・429・503 | 再試行 | 再試行 | 再試行 |
  | 502・504・通信の途中の切断 | 再試行 | 再試行 | しない |
  | 500・読み取りの timeout | 再試行 | しない | しない |

  書き込みのツール（`nl2sql_query` など）は、呼び先に届いた可能性がある失敗では再送しない（SQL の実行・LLM の呼び出しを
  重複させない）。読み取り専用のツールも、読み取りの timeout は再送しない（`rag_search` のように呼び先で LLM が動いている）。
  `readOnlyHint` を送信後の失敗の再送の根拠にするのは、注釈を信頼する接続（標準の接続か「注釈を信頼する」にした接続）だけで、
  承認の既定の判定と同じ境界にする。信頼しない接続のツールは、ツール権限で許可しても・利用者が承認しても「それ以外」として
  扱う（実行してよいことと、同じ操作を 2 回送ってよいことは別。`tools.retry_after_delivery_allowed`。#2595）。
  待ちは 0.5 秒から 2 倍ずつ（上限 8 秒、後半を jitter）、429 / 503 の `Retry-After`（秒・HTTP 日付）があればその値（上限 30 秒）。
  1 回のツールの呼び出し（`initialize` から `tools/call` まで）全体を接続の timeout で区切り、各 POST の timeout は残りの時間
  （接続の確立は 10 秒以下）、待つと期限まで 1 秒を切るときは再試行しない。待ちは `tools._retry_sleep` で差し替える（テストは
  待たない）。最後の失敗は利用者の言葉のメッセージ（例:「MCP 接続「RAG」に接続できません（RAG のサービスが起動しているか、
  接続の URL が正しいかを確認してください）。」）と code（送信前の失敗 `mcp.unreachable`、ほか `mcp.timeout` / `mcp.http_error` /
  `mcp.request_error`。`error_details` に `attempts`・`retryable`）のツールの失敗としてモデルへ返し、Run は続く（モデルが
  利用者に伝える）。ツールの一覧が取れない接続は従来どおり飛ばして warning を残す。
- **NL2SQL のジョブの完了を待つ（#848）**: 組み込みの接続 `nl2sql` の `nl2sql_query` / `nl2sql_get_job` の結果が
  `pending` / `running`（`job_id` あり）なら、ツールの handler の中で `nl2sql_get_job` を `wait_seconds`（最大 20 秒。
  接続の timeout から 15 秒引いた値以下）付きで繰り返し呼び、完了した結果をモデルに返す。待つ合計の上限は
  `AGENT_NL2SQL_JOB_WAIT_SECONDS`（既定 300 秒、0 で待たない）で、1 回の待ちごとに Run がキャンセル・終了していないかを
  確かめる。上限を超えた・Run が終わった・続きの取得に失敗したときは最後の結果（`running` と `job_id`）を返す。
  SDK の function tool の timeout は待つ分を足した値にする。外部の MCP 接続のツールは対象外。
- **NL2SQL の SQL の生成前の確認（#1441）**: `nl2sql_query` は SQL の生成前の確認（NL2SQL の SQL 生成の画面の回答前の確認と同じ session）で
  要件を確かめ、足りない・明確でない情報があれば SQL を作らずに `status=needs_clarification` と `requirement.question`
  （質問・候補・理由。表・列は物理名）を 1 つだけ返す。Agent はモデルへの結果に `next_step`（`ask_clarification`。RAG の確認と
  同じ形）を足し、確かめ中の問い（`kind=nl2sql`・`requirement_session_id`・候補）を支援タスクの状態に残す。利用者が答えた次の Run は、
  指示の「データベース（NL2SQL）の確かめ中の問い」に従い `requirement_session_id` と `answer` を付けて呼び直す。確認の要るものが
  無くなり SELECT・FROM が明確になると、NL2SQL が要件を反映してジョブで SQL を作る（確認が要らなければ聞かずに進む）。
  Agent のチャットに「SQL の生成前の確認」のボタンは置かない。回答の対応は `needs_clarification`（`basis=nl2sql_clarification`）。
- **確認の取り消し（#1441・#1445）**: 確かめ中の確認（NL2SQL の SQL の生成前の確認・RAG の `needs_clarification` / 業務ガイドの clarify）は、
  チャットの「データベースの検索は不要」「資料の検索は不要」（`POST /api/runs` の `clarification_cancel`。同じ部品）か、確かめ中の問いへの
  取り消しの文（「データベースは関係ない」「資料は関係ない」「確認はやめて」など。`clarification_cancel.message_cancel_kinds` の
  決定的な判定）で取り消せる。取り消した Run は、その種類のツール（`nl2sql_*` / `rag_*`）をモデルに渡さず、ほかの手段で続けるか
  意図を聞き直す指示を足す。NL2SQL は Control Plane が `nl2sql_cancel_requirement` で session を閉じる（step に残し、予算に数えない）。
  RAG を取り消した回答は資料と照らし合わせない（`answer_validation` の `skipped` / `rag_declined`）。記録は Run の metadata
  `clarification_cancel`・イベント・支援タスクの状態の `clarification_cancellations` に同じ形
  （`{kind, trigger, run_id, at, questions, requirement_sessions}`）で残す。
- **回答前の確認（#1839）**: 3 製品共通の UX 契約（`platform/docs/ux-contracts/confirm-before-answer.md`）と部品
  （`ClarificationCard` / `ClarificationRecord`）で、Agent の確認を 2 つの出所から出す（実装は `features/agent/confirmation.py`）。
  - **ツールの結果の確認**（NL2SQL の SQL の生成前の確認・RAG の `needs_clarification`〔業務ガイドの条件と、検索・回答プロファイルの
    ルールの確認（`condition_id` が `rule:<id>`。RAG のチャットと同じ判定。#2117）〕/ 業務ガイドの clarify）: 確認事項・選択肢は呼び先が
    作ったまま受け渡す（Agent で二重に定義しない）。この Run で新しく確かめ中になった問いを、完了した Run の `confirmations`
    （`source=nl2sql` / `rag`）に出す。答え・スキップ（`POST /api/runs/{run_id}/confirmations/{confirmation_id}`。Run を作った利用者
    だけ）で同じ会話に次の Run を作り、metadata `clarification_response`（確認事項・選んだ選択肢の名前と ID・自由入力）と
    `clarification_round` を残す。次の Run の指示に答えを構造化して渡す（NL2SQL は `nl2sql_query` の `requirement_session_id` と
    `answer` の引数、RAG は `rag_search` の `conditions`。RAG の答えは支援タスクの分かっている条件にも `user_answer` で入れる）。
    「確認せずに回答」は前提を示して答える指示を渡し、NL2SQL の確認なら `nl2sql_query` を `requirement_session_id` と
    `skip_clarification: true` で呼び直させる（NL2SQL が確認せずに SQL を作り、`requirement.status=skipped`）。会話に残す記録は
    `outcome=skipped`。会話が先へ進んだら、答えていない確認は `expired` にする。
    利用者に確認しない Run（「確認せずに回答」を選んだ Run・方針が `assume`・確認の上限に達した Run）は、Control Plane が
    `rag_search` の引数に `skip_clarification: true` を決定的に付け（入力の schema にあるときだけ。モデルに任せない）、RAG に
    ルールの確認を出させずに回答させる。品質評価の Run は利用者のチャットと同じく確認を出させ、評価ケースの答えで答える
    （`ConfirmationBudget.skips_tool_clarification`。#2117）。複数選べる確認（`multiple`）はカードでも複数選べる。
  - **Agent 自身の確認**（Control Plane のツール `ask_user_confirmation`。引数 `question`・`reason`・`options[{label, description}]`
    （2〜8 個）・`multiple`。「その他（自由入力）」は画面が必ず出す）: SDK の `needs_approval`（呼び出しごとの判定）で中断し、Run を
    `waiting_confirmation`（確認待ち）にして SDK の状態を保存する（承認待ちと同じ仕組み。承認と同時に中断したら承認待ち）。答え・
    スキップで `queued` に戻し、同じ Run を再開する（確認のツールは実行され、結果が利用者の答え `status=answered`（`selected`・
    `other_text`）か `status=skipped`（前提を示して答える指示））。処理の段階は `confirmation_wait`（kind `confirmation`）。
    **確認は Run の状態を変える中断**（#3352。LangGraph の `interrupt` と同じ考え方）: 利用者に聞ける確認は、どの経路（差し戻しの
    後を含む）でも Run を確認待ちにし、問いを画面と MCP（`agent_chat` の `confirmation`）に出す。中断の判断はツールを実行する前に
    応答ごとに 1 回決め（SDK の `on_llm_end`。`_ToolRecorder.hold_for_confirmation`）、確認で中断する応答のほかのツールの呼び出しは
    実行せず、承認にも回さない（結果は `error_code=tool.confirmation_pending`。答えの後にモデルが必要なら呼び直す）。中断しない
    呼び出し（下のツールの確認のカード・ツールの失敗の直後・`nl2sql_query` の前の表・列の確認・スキップ・上限）は、確認を出して
    いないことと次にすることを結果で返す。
    **先に資料・業務データのツールで調べる**（#1917）: 根拠・データのツールを渡した Run で、モデルがまだ MCP 接続のツール（根拠・
    データのツールとプロファイルの一覧・推薦）を 1 つも呼んでいない確認は、モデルが作った選択肢を外した自由入力の確認にする（選択肢を
    ツールの結果から作れないため。`ConfirmationBudget.tools_consulted`）。先に調べることは指示とツールの説明に置く（中断せずに
    「先に調べる」を返すと、モデルは確認を出したと取り違え、答えを待つと述べるだけの回答で完了した。#3352）。拒答のガード
    （`AGENT_TOOL_GROUNDING_REQUIRED`）と同じ設定で切れる。
    **ツールの確認に任せる**（#1934・#2726）: この Run のツールの結果が確認事項を返して止まっている（NL2SQL・`rag_search` の
    `needs_clarification`・根拠の取得のルールの確認と、最も新しい業務ガイドの判断（`rag_lookup_guides`・`rag_search` の
    `guide`）が条件を確かめる `clarify`。答えた条件の問いは数えない）ときは中断せず、`status=tool_confirmation_pending` を返す。
    確認事項は条件の id 付きのカードになり、答えは次の Run の分かっている条件として業務ガイドの照合・最終の検証の `guide` に渡る
    （Agent 自身の確認で聞くと、答えが業務ガイドの条件に渡らず、同じ条件の確認が確かめ中のまま残る）。
  - **やめる**（API の `action: "cancel"`。画面は #2311 から出さず、確認を終えるのは「確認せずに回答」だけ。質問を取り消すのは入力欄の「停止」）: 確認を出した Run と、同じ質問の確認の往復の Run に metadata `clarification_withdrawn` を付けて会話・履歴・支援タスクの
    状態から外し、最初の質問を返す。Agent 自身の確認で待っている Run は取り消し、NL2SQL の session は
    `nl2sql_cancel_requirement` で閉じる。
  - **業務 Agent の方針**（`clarification_policy`: `confirm` = 確認してから回答する（既定）/ `assume` = 前提を示して回答する、
    `clarification_max_rounds`: 確認の上限（往復。0 は既定の 3 回、2〜6。NL2SQL の業務プロファイルと同じ））: 確認できる Run にだけ `ask_user_confirmation` を渡す。前提を示す
    Run（方針が `assume`・上限に達した・「確認せずに回答」の次の Run・無人の Run（品質評価））は、ツールを渡さず、RAG の
    確認の次の手を `answer_with_assumptions`（聞き返さずに前提を示す）に変え、RAG の確認のカードを出さない。往復は、答えで作った
    Run の始めの `clarification_round` と、その Run の Agent 自身の確認の数で数える。
  - **「確認せずに回答」を選んだ Run**（ツールの結果の確認のスキップで作った次の Run と、Agent 自身の確認のスキップで再開した
    Run。#1903。`ConfirmationBudget.skipped`）: その Run の残りでも確認しない。方針を `assume` 相当にし（新しい
    `ask_user_confirmation` の呼び出しは中断せず `status=skipped` を返す。再開のためにツールは渡したまま）、NL2SQL が確認事項を
    返したら結果の次の手を `retry_without_clarification`（`requirement_session_id` と `skip_clarification: true` の引数）にして
    聞かずに呼び直させ、RAG の確認は `answer_with_assumptions`。完了した Run の `confirmations` には NL2SQL・RAG の確認を出さず、
    支援タスクの状態にも確かめ中の問いを残さない（モデルが呼び直さなかったときも、カードで聞き直さない）。答えた（スキップ
    ではない）ときは今どおり。
- **設定**: `AGENT_EXTERNAL_RAG_MCP_URL` / `AGENT_EXTERNAL_NL2SQL_MCP_URL`（例 `http://rag-host/api/mcp`）、
  `AGENT_EXTERNAL_RAG_TIMEOUT_SECONDS`（既定 630 秒。`rag_search` は RAG の回答生成（`RAG_ANSWER_TIMEOUT_SECONDS`、既定 600 秒）
  を通しで待つため、それに HTTP の往復の余裕 30 秒を足す。#1425）/ `AGENT_EXTERNAL_NL2SQL_TIMEOUT_SECONDS`（既定 600 秒。NL2SQL の
  `nl2sql_query` / `nl2sql_get_job` は SQL の生成・実行をジョブにし、1 回の呼び出しでは結果を最大 45 秒（`wait_seconds`）だけ待って
  job_id を返す。`nl2sql_query` は SQL の生成前の確認〔LLM。下書きと回答の後の再検査で最大 2 回。#1441〕を呼び、その分だけ
  ジョブを待つ秒数を減らす。45 秒と SQL の生成前の確認の 480 秒〔NL2SQL の LLM 1 回 240 秒 × 試行 2 回。#2240〕に、HTTP の往復・
  ジョブの登録・DB の負荷の余裕 75 秒を足した 600 秒にする〔#1432・#2240〕。続きの `nl2sql_get_job` は 1 回 20 秒まで待つ。
  ジョブの完了は `AGENT_NL2SQL_JOB_WAIT_SECONDS` まで待つ。SQL の生成〔LLM の 1 回〕と実行は NL2SQL の DB の呼び出しの
  上限〔120 秒〕で打ち切られる。#1430）は接続 `rag` / `nl2sql` の初期値（タイムアウト秒の上限は 900 秒。SDK の function tool の
  timeout は、接続の timeout〔`nl2sql` はジョブを待つ分を足した値〕の 2 倍）。`backend/.env.example` は両方の値を明示する（#1430）。画面で保存済みの接続はその値のまま（#1425 より前に保存した `rag` の 60 秒は、
  画面の「MCP 接続」で 630 秒に直す）。画面の「MCP 接続」で URL・タイムアウトを変更でき（保存先に残り、.env の値より優先する。#764）、署名鍵は
  設定済みかどうかだけを表示する。旧「外部 MCP」の単一の設定（`AGENT_EXTERNAL_MCP_BASE_URL` 等）・
  既定のサーバー・NL2SQL の既定取得件数（`AGENT_EXTERNAL_NL2SQL_DEFAULT_LIMIT`）は #757 で削除した。
- **RAG の図の根拠を画面で開く（#1311）**: 画面の「図を開く」は #2462 で削除した（図は原文のプレビューの強調で確かめる）。
  backend の `GET /api/runs/{run_id}/figure-url`（実行の詳細と同じ権限・対象範囲）が、
  Run の `rag_evidence` の成果物にその図があることを確かめてから、組み込みの接続 `rag` の `rag_read_source` を
  `include_image_url=true` で呼ぶ。サービストークンの `sub` は **画面を見ている利用者**（Run を見られる管理者が Run の
  利用者の権限を借りないように）、claim に `purpose=figure_url` を入れる（RAG はこの印の無い呼び出し、つまり Run の中で
  モデルが呼んだときは URL を作らない）。画面の操作で Run のツール呼び出しではないので `tool_registry` を通さず、
  URL（トークンを含む）は保存・ログに残さない（応答も `Cache-Control: private, no-store`）。RAG は利用者・chunk・版に
  縛った 5 分の HMAC 署名の URL（`GET /api/figures/{token}`）を返し、読むたびに権限・版を確かめ直す。ブラウザから
  開く起点は `AGENT_EXTERNAL_RAG_PUBLIC_URL`（空なら RAG が返す URL。MCP の URL の host）で、画面は開くたびに
  URL を取り直し、画像を読めなければ（期限切れ・資料の更新・権限）取り直しの操作を出す。
- **RAG の根拠の原文のプレビュー（#2389）**: 根拠の右のサイドパネル（共通の `EvidenceSourceSheet`）の下部に、RAG と
  同じ共通の `DocumentPreview`（ページの画像・ツールバー・bbox の強調）を出す。`GET /api/runs/{run_id}/source-preview`
  （実行の詳細と同じ権限・対象範囲）が、Run の `rag_evidence` の成果物にその根拠があることを確かめてから、`rag` の
  `rag_read_source` を `include_preview=true`・`purpose=source_preview`（`sub` は画面を見ている利用者）で呼ぶ。RAG は
  プレビューの種類・ページの一覧・ページの画像と原文の URL（利用者・chunk・版・ファイルに縛った 5 分の HMAC 署名。
  `GET /api/source-previews/{token}/pages/{page}`・`…/content`）・強調に要る根拠の metadata を返し、読むたびに権限・版を
  確かめ直す。画面は期限の前に取り直し、出せない根拠（権限が無い・ファイルが無い・資料の更新）は理由と再試行を出す。
  図を開く（#1311）とは署名・検証の仕組み（RAG の `app/rag/signed_url.py`）を共有し、用途の印で鍵を分ける。

### 4.2 外部の MCP クライアントに業務 Agent を公開する（#1874）

外部の Agent（Claude Code・Codex など）が業務 Agent を「専門家」として呼べるよう、`POST /api/mcp`
（`features/agent/mcp_server.py`。共通の `pr_backend_core.mcp`、Streamable HTTP の JSON 応答）でチャットのツールを出す。
契約は `platform/contracts/mcp/agent-tools.json`（`tests/test_mcp_contract.py`）。

- 認証は共通の API キーだけ（`authorize_api_request` の `api_key_paths={"/mcp"}`。使える製品に `agent`）。キーを発行した
  利用者の capability と対象範囲（ロールの業務 Agent）で判定し、キーの範囲（`PLATFORM_API_KEYS.RESOURCE_SCOPES` の
  `agent`）があれば「利用者の範囲 ∩ キーの範囲」に絞る（外は 403 の `AGENT_OUT_OF_KEY_SCOPE`）。local の認証モードでも
  キーを付けた要求は検証し、付けない要求はローカル利用者になる。Run・会話の見え方は画面と同じで、キーの持ち主が
  system_admin なら他の利用者の Run を読めるが、他の利用者の会話の続きと確認への答えは 403 の
  `OTHERS_CONTENT_FORBIDDEN`（持ち主だけ。#3244）。
- ツール: `agent_list`（公開中の版の名前・説明・スキル）、`agent_chat`（`POST /runs` と同じ規則で Run を作る。下書きは
  使えない。`thread_id` で同じ会話を続ける）、`agent_get_run`（読める Run の状態・回答・RAG の根拠・回答前の確認・使った
  ツール）、`agent_answer_confirmation`（`decide_confirmation` と同じ。`answer` / `skip`。「確認をやめる」は出さない）、
  `agent_cancel_run`（`POST /runs/{id}/cancel` と同じ判定と取消。冪等で、終わった Run は `result` で返す。#3240）、
  `agent_get_run_observation`（評価・監査の観測。`agent_get_run` と同じ人が読め、Run の保存した記録から根拠の全件と場所・
  ツールの呼び出しの経過・利用量を返す。根拠は呼ぶたびに今の権限で確かめ直す。#3240。詳細は docs/agents/mcp.md）。
  回答は `wait_seconds` まで待ち（既定 50 秒・30 秒、最大 300 秒。in-process でもこのプロセスの Run を待つだけで、
  dispatcher の構成でも保存先を読み直して待つ）、終わらなければ `in_progress=true` を返す。
- MCP の Run の印は `metadata.source=mcp` とキーの ID・名前（`api_key_id` / `api_key_name`。予約の key）。回答前の確認の
  答えで作る次の Run にも引き継ぐ。印のある Run は、無人の実行（#1813）と同じく承認の要るツールを呼ばず、モデルへは
  「外部の MCP クライアントからの実行では使えない」と返す（step の `error_code` は `mcp.approval_required`）。
  Agent 自身の回答前の確認（`ask_user_confirmation`）は断らず、クライアントが答える。
- 上限: キーごとの 1 分あたりの呼び出し（共通の認証。`PLATFORM_API_KEY_CALLS_PER_MINUTE`、429 と `Retry-After`）と、
  直近 24 時間に作る Run の数（`PLATFORM_API_KEY_RUNS_PER_DAY`。`agent_chat` / `agent_answer_confirmation` が数え、
  超えたら `API_KEY_RUN_LIMIT`・`status` 429・`details.retry_after_seconds`）。
- スキルの書き出し（`agent_skill_export`）: 使える業務 Agent の公開中の版が使うスキルを、Agent Skills の形のファイル（`skill_export.export_skill`。画面の `GET /api/skills/{skill_id}/export` と同じ）で返す。
- 向きは「外部のクライアント → Agent → RAG / NL2SQL」。RAG / NL2SQL は Agent の MCP を呼ばない。A2A（Agent2Agent）
  プロトコルは持たない。

## 5. Dispatcher and persistence

開発時は API のプロセスで実行する（`AGENT_RUNTIME_DISPATCH_MODE=in_process`。asyncio の task）。本番で別プロセスに
する場合は `runtime-dispatcher`（`python -m app.features.agent.runtime_dispatcher`）が Oracle checkpoint row を
`SELECT ... FOR UPDATE` し、queued の組み込み Runtime の Run に期限付き lease を付けて claim し、実行・再開する。
実行を始めたら lease を外す（承認の決定で queued に戻った Run を、すぐ claim できるようにする）。別 queue 製品は追加しない。

- **claim と実行の世代（fencing。#2606。`app/features/agent/run_claim.py`）**: lease は所有者（`worker_id`）・一意な
  `token`・単調に増える `generation` を持つ（表・列は足さず、Run の予約の metadata `_runtime_dispatch_lease`）。
  実行の開始（`begin_builtin_run` / `begin_builtin_resume` に claim を渡す）は、queued の Run を、今の lease と
  token・所有者・世代が一致し期限（`expires_at`）が今より後のときだけ 1 回受け付ける。Oracle では head を lock して
  別のプロセスの claim を読んでから判定し、その場で書く（条件付きの遷移）。期限の切れた・claim し直された旧所有者の
  開始、同じ claim の 2 回目の開始、running・承認待ち・確認待ち・終了した Run の開始は受け付けない。in_process の
  開始（claim なし）も queued だけを 1 回開始し、期限内の lease を持つ worker がいれば開始しない。
- 開始で lease を外し、token と世代を実行の記録（`_runtime_execution`）に移す。実行の区切りの書き戻し（完了・失敗・
  承認待ちの SDK の状態の保存）は、running で実行の記録の token が自分の実行と同じときだけ受け付け、利用量は
  token が同じときだけ受け付ける（実行の文脈の token は `run_claim.bind_execution`。実行の外の API の操作は
  検査しない）。別の実行が始まった後・自分の実行が承認待ちで区切った後の遅れた書き戻しは無視する（ログ
  `agent_runtime_stale_execution_write_ignored`。開始の拒否は `agent_runtime_begin_rejected`）。
- running の Run を自動で claim し直す仕組み・heartbeat は持たない（止まった worker の Run は再起動の回復の方針の
  まま）。この fencing は保存先の中で同じ Run の実行が重ならないようにするだけで、外部のサービスへの副作用の
  重複を防ぐものではない（#2601）。

外部 dispatcher は `AGENT_RUNTIME_REPOSITORY_BACKEND=oracle_checkpoint|oracle_normalized` が前提。
memory backend は process 間共有されないため production dispatcher に使用しない。

### 5.1 保存先（#764）

保存先は `AGENT_RUNTIME_REPOSITORY_BACKEND`（既定 `auto`。#839）。`auto` は起動時に 1 回だけ、共通の `PLATFORM_ORACLE_*` の
設定がそろっていれば `oracle_checkpoint`、無ければ `memory` に決め（`app/features/agent/storage_backend.py`）、Run の repository・
定義の store・Run の事実が同じ決定に従う。Oracle に接続できない（接続のエラー）ときも memory に縮退せず、backend は起動して
接続できた時点で読み込む（#1212。§5.1.1）。今の保存先は `GET /api/runtime/storage`（`backend`・
`persistent`・`database_configured`・`reason`。接続先は返さない。#839）で分かり、「運用設定 > 実行環境」の「保存先」の
カードが `StatusBadge` と直し方を出す。保存していない（`persistent=false`）ときは、業務 Agent・スキル・
実行履歴・品質評価・MCP 接続・ツール権限・システムテーブルの
画面の先頭に warning の Banner（`NonPersistentStorageNotice`）を出し、実行環境へ案内する。理由は、DB の設定がそろって
いれば `memory_backend`（memory などを明示。保存先の設定を直す）か `restart_required`（`auto` で起動時は DB が未設定だった。
再起動する）、そろっていなければ `database_not_configured`（DB の設定から直す）。`auto` で保存済みの checkpoint 全体が読めず memory で
起動したときは `checkpoint_invalid`（#853。§5.1.1）。

| 対象 | memory | file | oracle_checkpoint / oracle_normalized |
|---|---|---|---|
| Run・業務 Agent | プロセス内 | `AGENT_RUNTIME_SNAPSHOT_PATH` の JSON | `AGENT_RUNTIME_ENTITIES`（1 件 = 1 行の JSON）と head の `AGENT_RUNTIME_CHECKPOINTS`（版・header。#1939）。normalized は監査用の `AGENT_RUNTIME_RUNS/EVENTS/STEPS/APPROVALS/ARTIFACTS` も、書いた Run の分だけ書く |
| 画面・API で変えた定義（Skill・MCP 接続・ツール権限） | 保存しない | snapshot の隣の `<名前>.control-plane.json` | `AGENT_CONTROL_PLANE_ITEMS`（`ITEM_KIND` × `ITEM_ID` の JSON） |
| 利用状況・フィードバックの集計に使う Run の事実（#794） | 保存しない（メモリの Run をその場で集計） | 保存しない（同左） | `AGENT_RUN_FACTS`（1 Run = 1 行。SQL で集計） |

- Oracle は共通の `PLATFORM_ORACLE_*` で接続する（旧 `AGENT_RUNTIME_ORACLE_*` は読まない）。テーブルはシステムテーブル
  （`app.system_schema` の migration 006）が作り、アプリは DDL を実行しない。テーブルが無いあいだは空の状態で起動し、
  定義の保存は「システムテーブルで作成・更新してください」で断る。全再作成はこれらのテーブルも消す（Run の履歴も消える）。
- 定義は API の変更の後に保存し（`control_plane_store.save_*`）、保存先の読み込み（`storage_bootstrap`。§5.1.1）で
  `restore_control_plane()` を呼び、`.env` の宣言の後に重ねる。RAG / NL2SQL の接続は画面で変えた URL・タイムアウトだけを
  上書きする（認証方式は変えない）。`.env` の宣言・組み込みの定義は保存しない。
- 保存済みのデータを書き換える migration（`MigrationArtifact.rewrites`。例: 013 の組み込みのスキルの ID の改名は
  `AGENT_RUNTIME_CHECKPOINTS`、008〜011 は `AGENT_CONTROL_PLANE_ITEMS`）と全再作成は、`storage_bootstrap.rewrite_saved_data`
  で包む（#1844）。書き換えの間は Run の repository の lock を持って変更・保存を待たせ（Run・業務 Agent）、定義の保存は
  `ControlPlaneStoreError`（API は 503「システムテーブルを更新しています…」）で断る。書き換えの後に、同じプロセスの
  Run の repository を checkpoint から、定義の registry（スキル・MCP 接続・ツール権限・品質評価）を保存先から
  読み直し、保存先から消えた定義はメモリからも消す（実行中の評価の job はメモリの値を残す）。読み直しの間の保存が
  古いメモリの値で migration の結果を上書きしないため。
  - 別のプロセスのメモリは読み直せないので、結果の `restart_required`（画面は「作成・更新」の直下の warning の Banner）で
    再起動を求める: `AGENT_RUNTIME_DISPATCH_MODE` が `in_process` でなければ dispatcher、読み直しに失敗したら backend。
    CLI（`agent_system_schema --initialize`）は別のプロセスなので、書き換えたら（出力の `rewritten_saved_data`）動いている
    backend の再起動を出力で求める（配備の `init_script.sh` は CLI の後に backend を再起動する）。
- MCP 接続の API キー・OAuth の client secret・セッション ID は、`PLATFORM_SERVICE_TOKEN_SECRET` から HKDF-SHA256 で
  導いた鍵の Fernet で暗号化して保存する（`app.secret_box`。`enc:v1:...`）。署名鍵を変えると復号できないため、その接続の
  秘密は画面で入れ直す。署名鍵が無いと秘密を含む接続は保存できない（503）。
  - #1101 より前に平文で保存した行も読める（`enc:v1:` で始まらない値は平文として扱う）。起動時の復元で平文の秘密を
    見つけたら、その行を暗号化して保存し直す（暗号化済みの値は作り直さない。署名鍵が無い・保存できないときは平文のまま
    読み続け、起動は止めない）。
- 1 worker・`in_process` の前提は変えない（Run の状態機はプロセスのメモリにあり、保存先はその写し）。
- **Run・業務 Agent は 1 件 = 1 行で、変えたものだけを書く**（#1939。`app/features/agent/runtime_checkpoint_store.py`）。
  以前は変更のたびに全 Run・業務 Agent の snapshot を JSON にして 1 行の CLOB に書き戻し、書き込みの時間が Run の総数に
  比例した（開発の DB で会話の削除に 19 秒。合成の 3,000 件の Run で 1 回の保存に約 2 秒）。
  - repository の変更は lock の中でメモリを変え、変えた Run・業務 Agent に印を付けて（`_mark_run_changed` など。
    Event を足すと自動で付く）保存の予定に積む。書き込み（I/O）は lock を放した後に行い、書き込みの間も一覧・回答の
    stream はメモリを読める。予定はプロセスの中で 1 つずつ、積んだ順に書き、書けなければ予定に残して次の書き込みで
    重ねて書き直す（API の要求には例外を返す。メモリは変わったまま）。
  - event loop のスレッド（組み込み Runtime の実行・`async def` の handler）からの変更は書き込みを待たずに戻り、
    バックグラウンドの書き込みのスレッドが書く（失敗は間をおいて書き直す。`agent_runtime_background_write_failed`）。
    `router.py` の `async def` の handler は repository を `run_in_threadpool` で呼び（直に呼ぶ書き方は
    `tests/test_router_threadpool.py` が検出する）、書き終えてから応答する。終了時は残った予定を書く（10 秒まで）。
  - 遅い書き込み（1 秒以上）はログ `agent_runtime_storage_written`（`duration_ms`・行の数）を warning で出す。
  - テストは印の付け忘れを保存のたびに検出する（`VERIFY_CHANGE_TRACKING`。lock を取ってからの変更と印を比べる）。
- **2 つのプロセスが書いても互いの更新を消さない**（#1916）。head（`AGENT_RUNTIME_CHECKPOINTS` の行）の `VERSION_NO` は
  書くたびに 1 つ増え、`WRITER_ID` に書いたプロセス（ホスト・pid・起動ごとの乱数）を残す。書くプロセスは head を
  `FOR UPDATE` で lock し（書き込みは版の順に並ぶ）、前回の同期の版より後に書かれた行（`CHANGE_VERSION`。別のプロセス
  ＝同じ `.env` の 2 つ目の backend・dispatcher・CLI の変更）を読んで、このプロセスが変えていないものは保存先の値に
  そろえ、変えたものはこのプロセスの値で書く。両方が同じ 1 件を変えたときだけ後から書いた方の値になる（ログ
  `agent_runtime_checkpoint_concurrent_write` の `overlapping`。in_process では warning、dispatcher を別プロセスに
  した構成では info）。別のプロセスが書かなければ、行は読まない（head の 1 行と変えた行だけ）。
  - 1 件の削除（業務 Agent の削除・読めない行の退避）は行を消さずに `ENTITY_JSON` を NULL にする（別のプロセスが削除を
    知る）。全体の書き直し（バックアップの復元）は行を入れ替えて header の `reset_version` を上げ、別のプロセスは次に
    書くときに全体を読み直す。
  - 読めない行は、読み込み・別のプロセスの変更の取り込みで退避し（元の文字列は header の `quarantined` に移す）、
    行は削除の印にする。head が読めなければ上書きしない（`auto` は memory で起動して案内する。§5.1.1）。
  - dispatcher の claim は head を lock して別のプロセスの変更を取り込み、queued の Run の lease を書く（以前は
    claim のたびに全体の snapshot を読み直した）。
- **migration 016**（`20261015_016_runtime_entities`。データを消さないので承認は求めない。`rewrites=runtime`）が、
  以前の全体の snapshot を行に移す。元の snapshot は `AGENT_RUNTIME_ENTITIES` の `ENTITY_KIND = 'legacy_snapshot'` の行に
  そのまま残し（アプリは読まない。全再作成で消える）、`runs` / `agents` の要素を id ごとに 1 行にする（id の無い要素・
  同じ id の 2 つ目以降は移さず、元の snapshot に残る）。head の `SNAPSHOT_JSON` は `runs` / `agents` を除いた header
  （`storage = "entities"`）にし、版を 1 つ進める。適用の前（移す前の形・`VERSION_NO` の列や行の表が無い DB）は
  Run の repository を読み込まず、業務の API は 503 で「運用設定 > システムテーブルで更新（migration 20261015_016）を
  適用してください」と返す（`AgentRuntimeMigrationRequiredError`）。適用すると、再起動せずに読み込む。
  - 起動を止める lease（1 つの保存先に 1 つの backend）は持たない。再起動（`--reload` を含む）のたびに前のプロセスの
    lease の期限を待つことになり、dispatcher は正当な 2 つ目の書き手のため。2 つ目の backend の起動は開発の手引き
    （`agent/AGENTS.md`「開発・検証」）で止める。

### 5.1.1 保存先の読み込みと再試行（#853 / #1212）

DB に接続できないあいだも backend は起動する（RAG / NL2SQL と同じ。#1212）。Run の repository は module の import 時に
作らず（`runtime.runtime_repository` は遅延の参照。`get_runtime_repository()`）、`app.features.agent.storage_bootstrap` が
Run の repository → 中断した Run の失敗（`in_process` だけ。#1894）→ 定義の復元 → 履歴の準備（評価の整理・Run の事実の
backfill）の順に 1 回だけ読み込む。

- **再起動で中断した Run**（#1894）: `in_process` の Run の実行は backend のプロセスの asyncio の task だけで、再起動
  （`--reload`・配備の更新・異常終了）で task が消えると拾い直す仕組みが無い。読み込みで、このプロセスの起動（module の
  import）の時刻より前に作った・変えた `running` / `queued` の Run を `failed` にする（Event `runtime.failed`、
  `error_code=runtime.interrupted`、`payload.source="startup"`、文「サーバーの再起動で実行が中断されました。もう一度送信して
  ください。」。途中の step も `failed`）。起動の後の Run は触らない（このプロセスが始めたものか、別のプロセスが動かしている途中）。
  承認待ち・確認待ちは保存した SDK の状態から再開できるので触らない。dispatcher（`in_process` 以外）のときは触らない
  （dispatcher が lease で claim する）。チャットは Run の失敗の文をそのまま出す。品質評価の job は `evaluation_store.restore` が中断として終える（#1432）。読み込み（`rewrite_saved_data` の読み直しを
  含む）の 2 回目以降は行わない。ログ `agent_interrupted_runs_failed`。

- 起動時（lifespan）はバックグラウンド（daemon thread）で読み込みを始め、済むまで 15 秒ごとに再試行する（ログ
  `agent_storage_not_ready`。済んだらログ `agent_storage_ready`）。
- 業務の API は、読み込みが済むまで最初の要求で読み込みを試み（`app.api.router.require_agent_storage`。認証の前）、
  読み込めなければ 503（`error_code=agent_storage_unavailable`、`Retry-After: 15`）。
  DB の状態・システム設定（データベース・システムテーブル・モデル・アップロード保存先）・認証・ユーザーとロールの API は待たない。
  画面は DB ゲートで案内し、DB に接続できるようになれば再起動せずに読み込む。
- DB に接続できない（`is_oracle_connection_error`）ときは memory に縮退しない（`AgentRuntimeStorageUnavailableError`）。
  済んだ段階は持ち越す。失敗の直後 5 秒は接続を試さずに同じ理由を返す（wallet の接続記述子の `retry_count` /
  `retry_delay` で 1 回の接続に 1 分以上かかることがあるため、要求ごとに接続を待たない）。別のスレッドが読み込んで
  いるあいだの要求は 10 秒まで待ち、超えたら 503。
- 定義の読み込みの接続以外の障害（権限など）は記録して先へ進む。Run の repository の接続以外の障害（明示した Oracle の
  checkpoint の破損・SQL・権限）は 503 で返す（checkpoint の破損は直し方の文、それ以外はログ `agent_storage_load_failed`
  への案内）。
- runtime-dispatcher（別 process）は最初の claim で repository を作り、作れなければ下の backoff で続ける。

保存先の 1 件の不整合で読み込みを止めない。読み込み（Oracle の checkpoint・file の snapshot・
dispatcher の claim）は `runtime.load_snapshot_tolerant` で、snapshot を JSON として読み、Run・業務 Agent を 1 件ずつ検証する。

- **直す**（`_repair_run`。待ちを終わらせる方向だけで、承認・再開の方向には直さない）: 終わった Run に残った pending の承認 →
  `cancelled`（`decided_by="system:storage-repair"`、承認待ちの step も `cancelled`）、承認待ちなのに pending の承認が無い Run →
  `failed`（`runtime.inconsistent_state`）、Event / step / 承認の `run_id` のずれ → 入っている Run に合わせる、無い承認を指す
  step → 参照を外す。直した内容は Run の `runtime.event`（`payload.source="storage_repair"`）とログ
  `agent_runtime_snapshot_run_repaired` に残す。
- **退避する**: schema に合わない・id が空 / 重複・直しても整合しない（承認が無い step を指すなど）Run・業務 Agent は読み込まず、
  元の JSON と理由を snapshot の `quarantined` に入れる（ログ `agent_runtime_snapshot_record_quarantined`）。次の保存でも
  `quarantined` は残る（黙って消さない。中身は checkpoint の先頭の行の `SNAPSHOT_JSON` で確かめる）。
- 直した・退避したら、読み込みの直後に保存する（失敗しても起動は止めない）。件数は `GET /api/runtime/storage` の
  `repaired_runs`（この起動で直した Run）・`skipped_runs` / `skipped_agents`（退避している数）と「保存先」のカードに出す。
- snapshot 全体が読めない（JSON の破損・object でない・未対応の版）ときだけ止める。`auto` は checkpoint を上書きしないよう
  memory にし（定義の store も memory にそろえる）、`reason=checkpoint_invalid` で案内する。明示した `oracle_*` / `file` は、
  直し方（行・ファイルを退避して削除するか、内容を直す）を書いた例外で業務の API を止める（データの保護）。
- 画面からの全体の書き出し・取り込み（「バックアップと復元」）は #1961 で削除した（データの保護は ADB のバックアップ）。
- runtime-dispatcher は 1 回の claim・実行の失敗で止まらず、ログを残して poll の間隔から 2 倍ずつ（上限 60 秒）待って続ける。

### 5.2 Run の事実と集計（#794）

- 利用状況（#772）・フィードバック（#774）は、Oracle の構成では `AGENT_RUN_FACTS`（migration 007）を SQL で集計する。
  期間は 7 / 30 / 90 / 180 / 365 日。日は `FROM_TZ(CREATED_AT, 'UTC') AT TIME ZONE (:timezone)`（画面のブラウザの
  タイムゾーン）で区切り、対象は Run の一覧と同じく利用できる業務 Agent だけ。フィードバックの一覧は
  `offset` / `limit` でサーバー側でページングする（新しい順。`RATED_AT DESC, RUN_ID DESC`）。
- 1 Run = 1 行（業務 Agent・版・利用者・状態・起点・モデルの利用量・本人と管理者の評価・質問・回答・日時）。
  日時は UTC の `TIMESTAMP`（タイムゾーンなし）。Runtime repository が作成・状態の変化・利用量・評価の時点で
  事実をキューに入れ、バックグラウンドのスレッドがまとめて MERGE する（`run_facts_store`。Run を待たせない・止めない。
  失敗はログ `agent_run_facts_not_saved` に残して捨てる）。起動時に Runtime repository の全 Run を backfill する。
- テーブルが無い・DB の障害のあいだは、メモリの Run の集計に戻す（応答の `source` が `memory`。画面に集計元を出す）。
  memory / file の構成は保存せず、メモリの Run を集計する（`source=memory`）。
- 確かめるべき実行（#1822。フィードバックの既定の見方。`GET /api/feedback/signals`）: 明示の評価は回答のごく一部にしか
  付かないため、評価の付いていない Run も暗黙のシグナルから集める（モデルは呼ばない。定義は `run_signals.py`、Python の
  `fact_signals` と SQL の `SIGNAL_CONDITIONS` は同じ規則で、テストが SQLite で突き合わせる）。
  | シグナル | 定義（Run の事実の列） |
  |---|---|
  | `not_helpful` 役に立たなかった | 本人か管理者の評価が `not_helpful`（`FEEDBACK_RATING` / `ADMIN_RATING`） |
  | `validation_failed` 検証で確かめられない内容 | 成果物「回答の検証」が `completed` で、根拠と矛盾する段落を外した・未確認の印の段落がある・回答全体の警告がある・読めない根拠があった（`VALIDATION_RESULT = 'failed'`。警告の印だけなら合格。#2738） |
  | `unvalidated` 検証できなかった | 検証そのものが失敗した `unvalidated`（`VALIDATION_RESULT = 'unvalidated'`。資料の根拠を使わなかった回答 `no_rag_evidence` は `ungrounded` として残し、業務データの道具も使わなかったときだけ下の `ungrounded_no_tools` にする） |
  | `ungrounded_no_tools` 根拠を使わずに答えた（#1834） | 資料の根拠を使わずに答え（`VALIDATION_RESULT` が `ungrounded`〔RAG の根拠のツールを持つ Agent〕か `no_rag_tools`〔持たない Agent〕）、業務データの道具（NL2SQL など RAG 以外の MCP 接続のツール）の成功した呼び出しも無い（`DATA_TOOL_CALLS = 0`）。業務データの道具で答えた Run・拒答した Run（回答の対応が `insufficient_evidence` / `needs_human`。ツールを呼ばずに答えた回答の拒答 #1835 を含み、「答えられなかった」に数える）・記録の無い古い事実（NULL）は数えない |
  | `unanswered` 答えられなかった | 回答の対応（#1305）が `insufficient_evidence` / `needs_human`（`ANSWER_OUTCOME`） |
  | `approval_rejected` 承認の却下 | 承認が `rejected`（`APPROVALS_REJECTED > 0`。終了で無効にした承認は数えない） |
  | `tool_failed` ツールの失敗 | step が `failed`（`TOOL_FAILURES > 0`） |
  | `run_failed` 実行の失敗・取り消し | Run の状態が `failed` / `cancelled` |

  対象は期間（Run を作った日時）と業務 Agent で絞った、終わった Run（品質評価の Run を除く）。集計は対象の Run の数・
  シグナルのある Run の数・種類ごとの数を前の同じ長さの期間と比べ、一覧は種類で絞って `CREATED_AT DESC, RUN_ID DESC` で
  ページングする。列は migration 012 / 014（`DATA_TOOL_CALLS`。#1834）で足す（データは消さない）。既存の Run の事実の新しい列は起動時の backfill が
  Runtime repository の Run から書く（Run の snapshot を整理した古い事実は空のままで、シグナルにならない）。migration 012 の
  前のテーブルには、シグナルの列を除いた MERGE（`LEGACY_MERGE_SQL`。014 の前は `MERGE_SQL_012`）で利用状況・フィードバックの事実を書き続ける。
- 品質評価（#776）の job は、終わった job も自動では消さずに残し（#3235。以前は 365 日・2,000 件、その前は 50 件で消していた）、
  `GET /api/evaluations` を `offset` / `limit` でページングする。前回との比較は残っている全 job から探す。
- 品質評価の 1 ケースの上限時間（Run の実行。判定は別。#1432）は `AGENT_EVALUATION_CASE_TIMEOUT_SECONDS`（秒。86400 秒まで）。
  既定の 0 は自動で、接続 `rag` の待ち時間（画面で保存した値。既定 630 秒）×（Run の RAG の予算 `AGENT_MAX_RAG_CALLS_PER_RUN`
  + 1）にする（既定 630 ×（6 + 1）= 4410 秒。300 秒〜86400 秒に収める）。RAG を使う業務 Agent の Run は RAG を予算の回数まで呼び、
  1 回は RAG の回答生成を通しで待つため（推論の量 medium の実測で 1 Run 160〜600 秒以上）、#1425 より前の固定の 300 秒では
  時間切れになった。+1 は回答の最終の検証（`rag_validate_answer`）とモデルの推論の分。RAG の予算が無制限（0 以下）なら
  `AGENT_MAX_TOOL_CALLS_PER_RUN` を回数に使う。上限までに決着しない Run は取り消し、ケースは時間切れ（`timed_out`。メッセージに
  実際の上限の秒数）にする。評価の job 全体の上限は持たない（ケースごとの上限で止まる）。
- 品質評価のケースは上限つきの並行（`AGENT_EVALUATION_CONCURRENCY`。既定 2、1〜8。#2004）で、評価ケースの順に始め、結果は
  ケースの位置に書く（並行でも順番は変わらない）。判定（LLM-as-judge）の呼び出しは job の中で 1 つの共通の実行器
  （`pr_backend_core.bounded_calls`）を共有し、openai SDK の再試行を切る。429 は全体で待って並行の数を下げ（最小 1。成功が
  続けば上限まで戻す）、5xx・接続・時間切れは種類ごとに再試行し、使い切った判定は間をおいて最後に 1 回だけ呼び直す。出力の
  上限で止まった判定は上限を広げて（広げられなければ推論の量を 1 段下げて。#2023）1 回だけ送り直す（#1845）。権限・認証・モデルの設定の誤り・割り当ての使い切りは、そのケースに
  理由を残し、残りのケースを始めずに job を失敗にする（同じ失敗をくり返さない）。業務 Agent の Run の中の呼び出しは対象外
  （Run ごとの SDK の再試行のまま）。
- 品質評価は 1 ケースを k 回反復できる（`POST /api/evaluations` の `trials`。既定 1、10 まで。#62）。ケースの数 × k は実行の
  予算 `AGENT_EVALUATION_MAX_TRIAL_RUNS`（既定 200）まで。結果は「ケース × 試行」（`results[].trial`）で、試行の順に並べる
  （取消で途中で止まっても先の試行はすべてのケースがそろう。Run の `metadata.evaluation_trial`）。概要の `pass_rate` は判定できた
  試行の平均で、ほかに Pass^k（k 回すべて成功したケースの割合）・Pass@k（どれか 1 回成功）・安定 / 不安定・欠測（未完了・評価者の
  失敗・取消・承認の停止）を出す。Pass@k / Pass^k の分母は k 回すべてを判定できたケースだけで、欠測のある k 回を成功に数えない。
  区間（95%）の単位はケースで、合格率と Pass@k / Pass^k は Wilson の score 区間、2 つの評価の対応のある差（`GET /api/evaluations/compare`
  の `paired`）は定義が同じケースだけで MOVER の区間を求める（集計は `pr_evaluation_scoring.trials`）。評価者の指紋
  （`judge_fingerprint`: 判定のモデルの設定と判定の指示の hash）が違う評価どうしは `same_judge=false` と出し、公開前の回帰の門の
  基準にしない。
- 評価ケースは能力分類（`capability`）・禁止するツール（`forbidden_tools`）・完了を示す語（`completion_claims`）を任意で持てる
  （#63。分類の語は評価データで決め、実装に業務の語を持たない）。安全違反は Run の step・承認・ツールの結果の事実だけでコードが判定し
  （`safety_check`）、LLM の合否で上書きしない: 禁止するツールの実行（`forbidden_tool_executed`）・承認が要る / 拒否されたツールの
  承認なしの実行（`unapproved_write`）・期待するツール（無ければ書き込みのツール）を実行していないのに完了を示す語で答えた
  （`unexecuted_claim`）。評価の dry-run・権限の拒否・承認待ちで止まった試行は `blocked_attempts` に残し、違反にしない。概要は
  安全違反の試行・ケースの数と、能力分類ごとの母数・判定・欠測・合格率（区間は分類のケースの数で求める）を出し、合格率には含めない。
  公開前の評価の門は、下書きを最後に変えた後の今の下書きの評価（取り消し・失敗を含むすべて）のどれかに安全違反があれば、合格率の門の
  設定に依らず公開を断る（評価し直して違反が出なかった回で打ち消さない）。
- 品質評価の評価者（回答の要点の判定・出力の項目の意味の判定）は、人が判定した校正セットで校正する（#66。
  `python -m app.cli.judge_calibration`。合成のセットは `agent/evaluation/judge-calibration/`）。dev / holdout に分け、holdout の
  件数・判定率・κ・誤合格の率で合否を決め（RAG / suite と同じ `pr_evaluation_scoring` の閾値）、観点（否定・数値・日付・条件・
  長さや丁寧さの偏り・回答の中の評価者への指示・未実行の完了報告など）ごとの誤合格 / 誤不合格・判定できなかった件と判定の理由を
  出す。評価者・指示を変えたら前の結果と比べ、校正セットが違えば比較できない理由を出す。安全・状態の遷移はコードの判定（上の `safety_check`）が正本で、
  LLM の判定で上書きしない。手順は [agent-judge-calibration.md](./agent-judge-calibration.md)。

- Run の実行構成の指紋（#67）: 組み込み Runtime は SDK の Agent を組み立てるたびに、指示の変わらない部分・モデルに渡すツールの定義・
  スキル・MCP の schema・モデルの設定・データの範囲の hash を `metadata.run_config`（予約の key）に残す（本文・資格情報は入れない。
  再開で構成が変われば `previous_fingerprints`）。ツール権限の承認 / 自動は指紋に入らない。開発・CI 用の記録再生
  `python -m app.cli.run_replay`（合成の記録。モデル・MCP・実の通信は 0）が、同じ構成で承認・拒否・中断・呼び先へ届く呼び出し・
  成果物が記録と同じかを確かめる。利用者向けの再実行（`POST /api/runs/{run_id}/replay`）とは別。手順は
  [agent-run-replay.md](./agent-run-replay.md)。

- 実行補助の除去実験（#69）: `POST /api/evaluations` の `disabled_aids`（`final_validation`・`tool_grounding`・`context_compaction`）で、
  その評価の Run（dry-run）だけ補助を外す（Run の metadata `evaluation_disabled_aids`。評価の Run でなければ読まない。権限・承認・
  データの範囲・予算は外せない）。`GET /api/evaluations/ablation` が全構成の評価と並べ、対応のある差と区間・悪化したケース・完了の
  仕方・モデル / ツール / MCP / RAG の呼び出し・token・p50 / p95・補助のコード経路が動いたかと、判定（adopt / reconsider /
  undetermined / not_applicable / not_comparable）を出す。外した評価は公開の合格率・回帰の門と「前回」に使わない（安全違反の拒否には
  数える）。手順は [agent-aid-ablation.md](./agent-aid-ablation.md)。

## 7. Snapshot migration

Snapshot v2 は runs/agents を持つ（旧版の `control_plane_state.runtimes/bindings`（#754）と `memory`（#756）は読み込んでも使わない）。

- v1 tool を一意に対応できる Skill へ推定。
- 変換不能 tool があれば `migration_required=true`, `enabled=false`。
- 既存 Run は model default により `runtime_id=legacy-native`。
- 旧エンジンの Memory・v1 Run（`X-Agent-API-Version: 1`）・planner は #756 で削除した。

## 8. UI information architecture

サイドナビは上に一般の利用者の画面（AI 活用：チャット / 実行履歴 / 承認）、その下に管理者の画面（Agent 構築：業務 Agent / スキル、改善・運用、セキュリティ設定、ユーザーとロール、運用設定、システム設定）を置く（#791。構成は docs/frontend-page-archetypes-spec.md）。Agent 画面では指示・Skill・モデルを選ぶ。

チャット（`/chat`。#768）は業務利用者の入口。使ってよい Agent を選び、会話の履歴（既定で閉じ、会話の欄の上端の行の左端の
開閉ボタンで開く。lg 以上は左のパネル、未満は side sheet。#889）・会話・入力欄を出す（RAG のチャットと同じ型）。1 往復が 1 Run で、`RunCreateRequest.thread_id` で会話を続ける
（省略すると新しい会話。`RunState.thread_id` に入る）。組み込み Runtime は同じ会話の完了した前の Run の質問と回答
（`kind="answer"` の成果物）と使ったツールの名前（結果は渡さない）を、3 製品共通の予算（`PLATFORM_CHAT_HISTORY_*`。新しい往復から
ターン数と文字数に入るだけ。#1818）で、`<conversation_history untrusted="true">` の未信頼のデータとして質問の前の利用者の
メッセージに付ける（assistant の発話としては渡さない）。会話の一覧・詳細は `GET /threads`・
`GET /threads/{thread_id}`（作った利用者だけ。別の利用者・別の Agent の会話は 404 で、続けることもできない）。
名前の変更は `PATCH /threads/{thread_id}`（`{"title"}`。1〜80 文字。最初の Run の `metadata.thread_title` に置く）、削除は
`DELETE /threads/{thread_id}`（#1817。作った利用者だけ）。削除は Run を監査・フィードバック・評価・利用量のために残し、会話からの参照
（`thread_id`）だけを外す（元の会話は `metadata.deleted_thread_id`）。回答の作成中・承認待ちの Run がある会話は 409。
回答の処理の経過（画面の `ChatProgress` と実行の詳細の処理の経過。#1359）は Run の trace（span の木）の投影で（#3226）、
span は共通の span の表（`PLATFORM_TRACE_SPANS`。案 A）にある。組み込み Runtime の実行の区間（`agent.execute` /
`agent.resume`）の `operation_trace(listener=...)` が span の開始・表示用のデータの変更・終了を知らせ、process の書き手
（`features/agent/trace_spans.py`。保存先が Oracle なら共通の `TraceSpanWriter` を API の lifespan と runtime dispatcher の
起動の後に始め、終了で止める。memory / file の保存先は同じ形の span を process の中に持つ）が、Run を実行した利用者を持ち主に
して表に書く。主の処理は queue に積むだけで DB を待たない。表に入れないものだけを Run に残す（Run の lock の中。
`record_builtin_span`）: 実行の全体の span の `trace_id`、工程の詳細 `span_details`（span id → ツールの内部名・引数と結果）、
記録した待ちの回 `trace_wait_rounds`、実行の区切りを確定したときに書けなかった span の数 `trace_dropped_spans`。終わった Run・
置き換わった実行の知らせ（停止の後に終わった span）は表に書かず、表で実行中のまま残った span は Run の終端に合わせて閉じる。
工程の span は表示用のデータ（`SpanDisplay`）で種類を持つ: モデルの呼び出し（`gen_ai.chat`）は応答がツールを求めたら
`plan`、最終の回答なら `respond`（呼び出しの後に決める）、ツールの呼び出し（`gen_ai.execute_tool`）は `tool`（実行しなかった
ツール〔却下・方針で拒否・上限・引数の誤り〕も `skipped` で残す）、承認・回答前の確認の待ちは、待ちが終わって再開した区間で
終わった区間の span（`agent.approval_wait` / `agent.confirmation_wait`）にする（待っている間は見出しの状態とカードで示す）。
再開の区間は同じ trace の、前の区間の子。最終の状態（完了・失敗・停止）は Run の状態が正本。画面は
`GET /runs/{run_id}/trace`（Run を今の判定で開き、その Run の trace id だけで表を読む。system_admin は同じ trace の全員の span、
それ以外は Run を実行した利用者の span。工程の詳細は Run から足し、Run の終端のイベントで閉じる）を共通の
`useTraceProgressSnapshot` で読み、実行中は読み直す。書けなかった span があれば「一部の処理だけを記録しています。」、表に書く
前の Run（`trace_store` が無い）は古い形式の案内を出す。MCP の呼び出し（`mcp.request`）は traceparent を HTTP header と
JSON-RPC の `params._meta` の両方で送り、呼び先（RAG / NL2SQL）がサービストークンの要求の traceparent を親にすると、呼び先の
span（持ち主はサービストークンの `sub` = Run の利用者）が Agent の木の `mcp.request` の下に出る。`mcp.request` は
OpenTelemetry の MCP の semantic conventions の `mcp.method.name`（`tools/call` なら `gen_ai.tool.name` も）を持ち、利用者向けの
処理の経過の木は `tools/call` だけを行にする（`initialize`・通知・`tools/list` は出さない。技術の trace の一覧には残す。#3272）。
Run の最初の区間（`agent.execute`）は、Run を受け付けた要求（画面・API キーの外部の MCP クライアント）の traceparent を親に
せず新しい trace を始め、要求の span は span link（`trace_link_context`）にだけ残す（RAG / NL2SQL と同じ
`operation_trace_parent` の規則。外部の要求が Run の trace id を決められないようにする。#3272）。
呼び先（RAG / NL2SQL）の span は、呼び先が付けた工程の名前（`display_name`。呼び先の画面と同じ名前）で出し、製品の
境目の行に製品名（「RAG · 文書の検索」）を添える（Agent の工程の定義では訳さない。#3272）。
支援タスクの状態（#1243）: 組み込み Runtime は Run の終わり（承認待ちで止めるときも）に、ツールの呼び出しから
支援タスクの状態を成果物（`kind="support_task"`・「支援タスクの状態」、`schema_version` 1）に残す。項目は会話・持ち主・
目的（会話の最初の質問）、分かっている条件（`rag_search` に渡した `conditions` と出力の `guide.known_conditions`。
値・出所・時刻、上書きした古い値）、確かめ中の問い（`clarifications`）、使った業務ガイド（id・版）、残った不足（`gaps`）、
根拠の参照（document_id・chunk_id・chunk_set_id・ファイル名。本文は持たない）、予算の消費（Run と会話の通しの
ツール・RAG の呼び出しの回数と時間）。同じ会話の次の Run は、同じ持ち主の前の完了した Run の状態を読み、短い
「支援タスクの状態」として指示の末尾に足す（条件は聞き直さず `conditions` に入れ、新しい発言の値で上書きする）。
状態は補助で、正本は RAG の回答と根拠。ツールを呼ばず、引き継ぐ状態も無い Run には残さない。
予算は `AGENT_MAX_RAG_CALLS_PER_RUN`（既定 6〔#1345。5 段の多段の質問と 1 回の言い換え〕。Run ごとの `rag_search`・`rag_retrieve_evidence`。
本文を読む `rag_read_source`・`rag_outline`・`rag_read_document`・`rag_read_section` は軽いので数えない）と
`AGENT_MAX_TOOL_CALLS_PER_TASK`（既定 60。同じ会話の通しのツールの呼び出し）。超える呼び出しは実行せず、ツールの結果
（`error_code="budget_exceeded"`、step は失敗）でモデルに知らせ、Run は失敗にしない。消費は Run の step から数えるため、
承認の後の再開で 0 に戻らない。失敗・取消の Run は状態を残さないが、その消費（実行したツールと、結果を受け取る前に
止まった呼び出し）は、前の完了した Run の状態に足して次の Run の会話の通しの予算に数える（#1277。失敗する Run を
繰り返して上限を超えさせない）。
回答の経路（#1283。RAG から Agent へは呼ばない・案内しない）: `rag_search` の結果の `outcome` が
`needs_environment_data`（資料だけでは確定できず、現場の値・記録の確認が要る）のとき、Control Plane がモデルへの
結果に `next_step` を足す（`rag_next_step`。記録する step の結果は RAG の結果のまま）。この Run に現場のデータを確かめる
道具（RAG 以外の MCP 接続のツール）があれば `action="continue_with_tools"`（`tools` に名前）、無ければ
`"answer_with_confirmations"`。スキルの指示は `next_step` に従うことを書く。支援タスクの状態の `route` に、この Run の
経路（`rag` / `rag_then_tools` / `tools` / `none`）・理由（最後の `rag_search` の対応）・`environment_data_required`・
続けた道具（`continued_with`）を Run の step から決定的に残す（`run_route`）。`outcome` が `needs_clarification` の
ときは `action="ask_clarification"`（`questions` に確かめる問い）を足し、手順・分岐ごとの答えより先に問いを返させる
（#1322）。
Agent が編成する根拠の取得（#2651）: RAG を使う標準のスキル（`business-rag-research`・`rag-and-structured-data`）は `rag_search`（RAG が回答まで作る）を使わず、`rag_retrieve_evidence` で根拠を集め、不足を確かめて引き直す・読み足す（`rag_read_source`・`rag_read_section`。図に依る質問だけ `include_image`）・回答を書くまでをモデルが行い、最終の検証（`rag_validate_answer`）は今までどおり Control Plane が行う。単純な質問は 1 回の取得の根拠で答え、分解・追加の探索・図の確認は要るときだけにする（planner・grader を足さない）。スキルは接続ごとに Agent 主体の取得を宣言する（`mcp_requirements` の `evidence_planning=caller`。SKILL.md は metadata の `production-ready-evidence-planning: "rag:caller"`）。宣言は公開の版に固定する要件の中にあるので、宣言の無い旧版のスキルを固定した公開版は今までどおり RAG が検索文を計画し、下書きの Run と再公開の後の Run だけが切り替わる。固定の中身の無い公開の版（#1855 より前の版と、組み込みの業務 Agent の版）は今のスキルで動くが、公開した時点の宣言を確かめられないので caller にしない（`AgentProfile.caller_planning_allowed`。「常に最新」〔`skill_policy=latest`〕を選んだ版は利用者が今のスキルを選んでいるので今の宣言に従う）（`agent_caller_planned_connections`。同じ接続を使うスキルのどれかが宣言すればその接続の取得はすべて caller）。宣言した接続では、組み込み Runtime が `rag_retrieve_evidence` に `planning=caller`（検索文はモデルが作った `query`・`queries` だけで、RAG は質問に応じた推論の LLM / VLM を呼ばない）と追跡 ID `request_id`（`<run_id>:<呼び出し ID>`）を決定的に付け、モデルに見せた schema から外す（`support_task.caller_planned_arguments` / `caller_planned_schema`。`planning` を持たない古い RAG には送らない）。利用者に確認しない Run は `rag_search` と同じく `skip_clarification` を付ける。取得が検索・回答プロファイルのルールの確認（`clarifications`）を返したら、`rag_search` の `needs_clarification` と同じ次の手（`evidence_clarification_step`）・確かめ中の問い（`apply_retrieve`）にする。取得が返す検索・回答プロファイルの承認済みのルール・用語の説明（`profile_knowledge`。#2701）は、根拠（`evidence`）と分けたままモデルへ渡し、回答の根拠（AI に渡した根拠・引用）と最終の検証の根拠には入れない。渡したことは Run の step の結果（全文）とログ `builtin_runtime_profile_knowledge_passed`（数と版だけ。`support_task.profile_knowledge_summary`）に残る（設定の画面の一致の確かめ〔preview〕とは別に、実行でモデルに渡った内容を追える）。
多段の質問（#1345・#1351。RAG は部品を出し、組み立ては Agent が行う。planner・workflow engine は作らない〔#756〕）: スキル `business-rag-research` の指示が、問いを段に分け、段ごとに前の段で分かった実体で `rag_retrieve_evidence` を呼び、
段の query は質問全体ではなく「実体（正式名・略号・ID）＋引く属性」だけにし（件数の `top_k`・`evidence_limit` は既定より小さくしない。多段の質問では省略する）、
資料が台帳・一覧で確かめると示したらその実体でその台帳・一覧を引き、同じ段で 2 回言い換えても根拠が出なければその段を
確かめられなかった点として次へ進み、比べる質問は実体ごとに引き、同じ文書の中の参照は `rag_outline` → `rag_read_document`
で読み、最後に質問全体で `rag_search` を呼び直さずに集めた根拠で答える手順を書く。Control Plane は根拠を集める・読む
ツールの結果に、本文の「第 N 章を参照」「別表 N 参照」など同じ文書の別の箇所への参照（`references`。文書・参照・読み方。
`text_references`）と、この Run で残る RAG の検索の回数（`rag_calls_remaining`）をモデルへの結果に足す（記録する step の
結果は RAG の結果のまま）。
章節を読み切る案内（#1438）: RAG の根拠には属する章節（`section`。文書の詳細の章節ナビゲーションの章節の id・頁の範囲・
文字数・親）が付く。スキル `business-rag-research` の指示は、質問が手順・一覧・条件のまとまりを求め、根拠の章節の文字数が
抜粋より長いときは `rag_read_section`（根拠の `chunk_id` か `section_id`）で章節を終わりまで読み、足りなければ `up` を 1 ずつ
増やして親の章節を読む（上がる回数・読む量はこの実行の残りのツールの呼び出しに合わせる）よう書く。Control Plane は、質問
（Run の目標と検索の query）が手順・一覧・条件の語（「手順」「すべて」「項目」「条件」など）を含むとき、根拠を集めるツールの
結果に、抜粋より長い章節（最大 3 つ。回答に使った根拠を先に）と読み方（`section_reads`。`section_read_note`）を足す。
`rag_read_section` の結果には、続きがあれば `next_cursor` で続きを読む案内を、読み切ったら（階層 2 以上の章節で、質問が
まとまりを求めるとき）親の章節へ上がる案内を足す。章節を読むかはモデルが決める（読み替えて実行はしない）。
章節の未読の続き（#2492）: 案内に従わずに続きを読まないまま答えると、最終の検証は主張が根拠に合うかだけを確かめるので、
後半の手順・前提の不足を見つけられない。質問が手順・一覧・条件のまとまりを求めるとき、Control Plane は Run の step から
章節ごとの読み取りの状態（`section_coverage`。同じ接続・文書・`section_id` の読み取りで `section_complete=true` に達したか、
上の章節を読み切ったか。`cursor` の呼び出しは、その cursor を返した章節に対応付ける）を決定的に追跡し、回答に使った章節
（回答が chunk の id を書いた・本文や題名と当たる。探索で読んだだけの章節は数えない）の続きが残っていれば（`rag_read_section`
で読み始めず、根拠を集めるツールの抜粋だけを使った章節も、章節が抜粋より長く chunk をすべて得ていなければ読んでいないとして
数える。文書を先頭から `rag_read_document` で終わりまで読めば読み切ったとみなす。#2503）、略号・別名と同じ
1 回の枠で、章節と `next_cursor` を挙げて差し戻す（経過の `tool_grounding` の `section_retry`）。続きの位置が古い
（`source_stale` / `cursor_invalid`）ときは `rag_outline` から読み直す案内にし、取り込み直した文書の同じ題名の章節を読み切れば
読んだとみなす。この実行のツールの呼び出しが上限に達していれば差し戻さず、取り消した Run も差し戻さない。それでも残った
章節は、回答の末尾の「読み切れていない章節」に理由（続きを読んでいない・文書が更新された・上限・失敗）と示し、最終の検証の
成果物に主張の照合とは別の `coverage`（`complete` / `incomplete` と章節）として残し、回答の対応の不足の印
（`unread_sections`。conditional）にする。
段の検索の決定的な補助（#1351。#1335 の評価で、Agent が `evidence_limit=5` を選んで答えの chunk が上限で切れ、同じ話題の
言い換えを繰り返して予算に達した）: (1) モデルが `rag_search`・`rag_retrieve_evidence` の `evidence_limit` を既定（ツールの
入力 schema の `default`。RAG の契約の `rag_search` は 12、`rag_retrieve_evidence` は検索する件数と同じ 50〔#1365・#3512〕）より
小さくしたら、Control Plane が既定に引き上げて呼ぶ（step には送った値を残す。既定より大きい値は変えない）。検索する件数の `top_k` も同じく（#1403。#1362 の再評価で、データの範囲で検索・回答プロファイルを固定した業務 Agent が `top_k` 5 / 10 を渡し、台帳の行が検索の結果に出ずに段を取りこぼした）、既定（契約の `top_k` の `default` は null で、RAG は省略時に 50 を使うので 50〔#3512〕。`RAG_TOP_K_DEFAULT`）より小さい値を既定に引き上げる。上限（入力 schema の `maximum`。`top_k` 100・`evidence_limit` 50）を超える値は上限に丸め、呼び先の入力検証で失敗させない。直したときはモデルへの結果に、直した値（`requested`・`sent`）と次から件数を省略する案内（`adjusted_limits`）を足し、モデルに見せるツールの schema の件数の説明に「多段の質問では省略する」を足す（`search_limit_schema`。実行の定義は契約のまま。`adjusted_search_limits`）。(2) 同じ Run で、同じツール・同じ範囲（検索・回答プロファイル・ナレッジベース・
フィルター・条件）の成功した検索と query が同じかほぼ同じ（NFKC・大文字小文字・空白・句読点と記号・助詞を除いて、
文字の 2-gram の Jaccard 係数が 0.8 以上。`repeated_query_note`）なら、モデルへの結果に繰り返しを止める案内
（`repeated_query`。回数・似た query・次の手）を足す（記録する step の結果は RAG の結果のまま。呼び出しは止めない）。
(3) 台帳の行の略号・区分（#1365。#1335 の再評価の D で、69 Run のうち 44 Run が 1 回の検索で答え、台帳の行の「担当部署: 経」の
「経」を引く次の段をしなかった。略号の表は 1 回目の結果に前後の文脈として 21〜142 位にしか出ない）: 根拠を集めた結果
（`rag_retrieve_evidence`・`rag_search`）に、この呼び出しの query の実体（正式名・略称・ID。NFKC・大文字小文字・空白と記号を
除いて比べる）が値に当たる台帳・一覧の行（表計算の 1 行の記録。根拠の `content_kind=record`、または表計算の 1 行の場所）が
あり、その行に略号・区分のような短い値（2 文字以下。数字だけの値は除く）があって、この Run の検索の query にまだ語として
出ていなければ、モデルへの結果にその値と意味を引く次の段の案内（`record_codes`。項目・値・行の実体・根拠と `next_step`。
5 件まで）を足す。案内だけで、呼び出しも回答も止めない。
案内を足してもモデルが引かずに答えることがある（#1958。#1865 の base で「担当部署: 安」を「安部の承認者」と答えた）ため、
回答を確定する前に Runtime が 1 回だけ確かめる（`support_task.unresolved_hops`・`builtin_runtime._require_hops`）: この Run の
根拠の結果の `record_codes` の値のうち、どの検索の query にも出ず、根拠の資料の本文（記録でない根拠）にも意味が出ていない
もの（2 文字は本文に含まれる、1 文字は「値」か「項目 値」の形）と、query の実体に当たる記録の略称・別表記の列の値（3 文字
以上）が根拠の資料の本文に出ているのに回答に無いもの（「略称・別表記: HRM」と保守計画の「ＨＲＭ: 毎月第 2 土曜日」）が
あれば、その値を挙げて「意味が要るなら引いてから答え直す。要らなければそのまま答える」を利用者のメッセージとして足して
Run を続ける（拒答のガード #1835 と同じ形。元の回答と差し戻しは経過の `tool_grounding` の `hop_retry` に残す）。確認の質問
だけの回答・承認で止まった Run・拒答にした Run は対象外。
挙げるのは回答が使っている値だけ（#1978。台帳の行の質問に関係しない列〔機密区分・重要度〕まで挙げると、ほとんどの Run で
差し戻し、要らない区分を引き直して正しい回答を崩した）: 対象は回答か質問が指す記録（この Run の根拠の記録のうちその行だけに
ある 3 文字以上の値が出る記録）だけ。略号・区分は略称・別表記の列を除き、回答に出る値（半角英数は前後が英数でない語として、
それ以外はその記録のほかの値を除いた回答に）で、質問が条件として挙げていないもの。意味の有無は `rag_search` の回答の文（台帳の
行の引き写しを除く。根拠の一覧は件数で切られる）も見る。略称・別表記は、回答が名前を挙げた根拠の資料の本文に出るもの
（回答がどの資料の名前も挙げなければ、どの資料でも）。差し戻しの後の回答の最終の検証で段落を外したときは、前の回答も同じ根拠で
検証し、前の回答のほうが外した内容が軽ければ（すべて外していない・外した段落が少ない）前の回答を使う（経過の `tool_grounding`
の `hop_reverted` に差し戻しの後の回答を残す）。
同じ 1 回の枠で、業務データの照会（`nl2sql_query`）を渡した Run で、資料の根拠のツールだけを呼んで `nl2sql_query` を呼ばず、
回答に資料に記載が無い・確かめられないと述べる段落（`answer_passages.is_absence_conclusion`）があれば
（`support_task.missing_data_lookup`。#1979。#1865 の測り直しで「モニターの単価は資料からは確認できません」と答えた）、
「業務の対象の値なら `nl2sql_query` で引いてから答え直す。業務データに無い値ならそのまま答える」を足して差し戻す（経過の
`tool_grounding` の `data_retry`。略号・別名が残っていればそちらを先にし、重ねない。利用者が業務データの確認を取り消した
Run は対象外）。
(4) 旧版の年度・版（#1405。#1362 の再評価の D で、「定期保守計画の 2025 年度と 2026 年度では」の比較に `include_superseded`
を渡さず、今の版だけで答えた）: `include_superseded` を渡さない根拠を集めた結果の `older_versions`（RAG が返す、当たった
今の版の文書の旧版。文書名と先頭の見出しのタイトル）のうち、タイトル・文書名に利用者の質問かこの呼び出しの query の年度・版
（西暦の年・和暦の年・第 N 版。NFKC で比べる）が当たる旧版があれば（質問が「旧版」「改定前」などを尋ねるなら全部）、
モデルへの結果に旧版も含めた検索し直しの案内（`superseded_versions`。旧版・当たった語と `next_step`。5 件まで。
`superseded_versions_note`）を足す。今の版の年度だけを尋ねる質問には出さない。案内だけで、旧版を既定で検索に含めない。
業務ガイドの照合（#1321・#1322）: 根拠を集めるだけの `rag_retrieve_evidence` は業務ガイド（確かめる条件・分岐・影響範囲）を
見ないため、モデルが業務ガイドを引かずに根拠を集めると、確かめる条件があっても分岐ごとに答えてしまう（#1317 の実環境の
評価）。モデルがその Run でその接続の `rag_lookup_guides`・`rag_search` を呼ばずに `rag_retrieve_evidence` を呼んだら、
Control Plane が同じ接続の `rag_lookup_guides` を 1 回呼ぶ（質問は利用者の質問と根拠を集めた質問。前の Run で問いを
確かめ中なら前の質問も含める。条件は前の Run までに分かった条件と、根拠を集めた呼び出しの `conditions`。検索・回答
プロファイルは根拠を集めた呼び出しのもの。モデルのツールと同じ境界を通し、step に残す〔trace_id は `guide_check_` で
始まる〕。予算・消費には数えない）。最上位の業務ガイドと次の手を、モデルへの根拠の結果に `guide_check` として足す
（`guide_check_note`。判断が `clarify` なら `ask_clarification`、`handoff` なら引き継ぎ、`branch` なら条件ごとに答える、
`answer` なら分かっている条件に当たる場合の手順だけを答え、係るときだけ影響範囲・承認を示す）。照合の結果
（業務ガイド・分かった条件・確かめ中の問い）は支援タスクの状態にも残す。モデルが自分で `rag_lookup_guides` を呼んだときも、同じ次の手を結果に `next_step` として足す（答える判断なら、業務ガイドは資料の根拠ではないので、根拠を集めて確かめてから答えるよう添える）。
業務ガイドの手順を段として引く（#2396。動的な多段）: `rag_lookup_guides` は手順の中身（目的・検索の手がかり`retrieval_hints`・参照の資料と頁 `references`・必要な根拠・終わりの条件）・当たる分岐・共通の資料・完了の条件を返し、分かっている条件で外れた分岐の手順は返さない。判断が `answer` / `branch` で手順があれば、次の手に手順ごとの検索の語（手がかり、無ければ題名）と参照の資料・頁（`steps`）と「手順を段として順に引く」を足す（`guide_steps.guide_steps_next_step`。この実行で残る検索の回数が手順の数より少なければ、手順の語をまとめて `rag_retrieve_evidence` を 1 回呼び、参照の頁は予算の外の `rag_read_document` で読む案内にする）。この Run の根拠（根拠を集める・読むツールの結果）が手順に紐づくかは決定的に判定する（参照の文書で頁が重なる・呼び出しの query に手順の手がかりが出る・根拠の本文に手がかりが出る）。回答を確定する前に、根拠で確かめられる手順（手がかりか参照を持つ手順）のうち根拠の無い手順があれば、略号・別名と同じ 1 回の枠で、手順と検索の手がかり・参照の頁を挙げて差し戻す（`guide_steps.unresolved_guide_steps`。経過の`tool_grounding` の `guide_step_retry`。略号・別名も残っていれば 1 つの差し戻しにまとめる）。手順ごとの根拠の有無は支援タスクの状態の `guide.steps` に残す。根拠の `references` に別の文書（`document_title`）があれば、同じ文書の参照（`text_references`）と同じ形で、参照先の文書名と表記で引く手がかりを結果の `references` に足す。
回答の最終の検証（#1246・#1277）: `AGENT_FINAL_VALIDATION_ENABLED`（既定 true）が true のとき、RAG の根拠を使った Run の
回答を保存する前に、Control Plane が（モデルではなく）根拠を返した MCP 接続ごとに `rag_validate_answer` を呼ぶ。渡すのは
その Run の質問・回答と、その接続の `rag_search` / `rag_retrieve_evidence` が返した根拠と `rag_read_source` / `rag_read_document` /
`rag_read_section` で読んだ chunk の参照（#1345。多段の質問は段の事実を読んで確かめるため。回答が引用した根拠（`chunk_id` を回答に
書いたもの。#1364）、回答の本文に固有の特徴（ID・コード・引用・見出し・同じ文字の並び・「組織規程 第 4 章」のような文書名＋章節の番号の参照を根拠の `section_path` と文書名に照らしたもの。多くの根拠が共有する
特徴は数えない）で当たった根拠（#1451。`chunk_id` を書かないモデルでも回答が使った根拠を見分ける）を先に、残りは
新しい呼び出しから順で、`rag_read_document` / `rag_read_section` でまとめて読んだ chunk のうち回答に当たらないものは
検索の根拠の後ろ。重複なし、接続ごとに 30 件まで。前の段の根拠を上限で落とさない。会話の続きでは、同じ会話の前のターン〔モデルに渡す往復の数まで〕の最終の検証で回答の段落を裏付けた〔`supported`〕根拠も、その Run で根拠を返した接続に 10 件まで入れ、回答に当たる根拠の後ろ・残りの根拠の前に置く。前のターンの事実を言い直した段落を今のターンの根拠だけで外さないため。本文は RAG が今の権限・データの範囲・版で読み直す。#1837）と、その接続の `rag_search` の `trace_id`
（`answer_trace_ids`。新しい順・10 件まで。#1527。RAG の回答の監査を通った説明と本文・根拠が同じ段落は、RAG がその判定を使って
モデルに渡さない。根拠は RAG が今の権限と版で読み直す）と、その接続の最も新しい `rag_search` の `requests`・`gaps`・業務ガイド（`guide`。
無ければ同じ接続の最も新しい `rag_lookup_guides` の最上位。判断が `answer` / `branch` で、検索・回答プロファイルが
分かるときだけ。分かっている条件を `conditions` に入れる）で、RAG の決定的な検査（要求の充足・手順の順序と分岐・
影響範囲・承認。#1276）も動かす。利用者が回答前の確認で答えた内容（この Run で答えた `ask_user_confirmation` と、前の Run の
確認に答えて作った Run の `clarification_response`。「確認せずに回答」は除く）は、確認事項の文と答えを前提（`premises`。10 件まで。
#1898）として渡し、RAG は資料ではない前提として根拠と並べて、答えをそのまま述べた段落と、資料の規則を答えに当てはめた結論を
確かめる（前提は資料の内容の裏付けには使わない）。この Run で成功した業務データ（NL2SQL）の照会の結果（`status=done`。照会の
質問・実行した SQL・列と先頭の 50 行。10 件まで）は `data_results` として渡し、件数・金額・一覧の段落と、資料の規則を結果に
当てはめた結論を、資料の根拠だけで「確かめられない」として外さない（#1950）。段落ごとの出典（`passage_citations`。段落の位置
`answer_range` とその接続の根拠の `chunk_id`。#104）も渡す: 業務 Agent の回答は使ったツールと根拠を本文とは別に示すので段落に出典を
書かないことが多く、RAG は段落の実際の出典の集合だけで裏付ける（未引用の資料で裏付けない）ため、渡さないと正しい段落もすべて
`unassessed` になる。出典は「回答の根拠」が検証の判定の無いときに使う決定的な照合（段落が書いた文書名と頁・章節の参照・引用・同じ
文字の並び。`answer_evidence.validation_passage_citations`）で作り、段落そのものに当たる根拠が無い段落（表の値の行など）は回答全体が
出典にした根拠にする。回答のどこにも当たらなければ渡さない。本文に出典を書いた段落は RAG が本文の出典を使い、渡した出典で広げない。
契約に `passage_citations` の無い古い RAG には渡さない。使ったツールの名前だけの行（「- `rag__rag_retrieve_evidence`」）は検証の前に
外す（`without_lookup_notes`）。呼び出しはモデルのツールと同じ境界（ポリシーの「拒否」・監査・Run の利用者の
サービストークン・step）を通し、「承認」は待たない。予算には数えない。接続ごとの判定は段落（`answer_quote`）ごとに
まとめ（どれかの接続が矛盾と判定すれば矛盾、そうでなくどれかの接続が裏付ければ裏付けあり）、結果は成果物
（`kind="answer_validation"`・「資料との照合」。接続ごとの結果は `connections`）に残す。valid でなければ、段落の印を付ける（#2738）。本文から外すのは根拠と
矛盾する段落（`contradicted`。RAG が根拠の ID を照合できずに判定を変えても、監査の元の判定 `audit_status` が矛盾なら矛盾。
#2622）だけで、根拠で確かめられない（`unsupported`）・出典を確かめられない（`citation_error`。引用が根拠に無ければ
`quote_unmatched`）・確かめが終わっていない（`unassessed`）・図の画像を読めていない（`figure_not_read`）段落は本文に残して
未確認（`unverified`）の印を付け、RAG の規則の検査（`target_mismatch`・`unsupported` の `value_unbound`）は警告（`warning`）の印にする。
節の項目は段落の 80 文字までの引用と理由の短い文（「根拠で確かめられない」など。検証のモデルが書いた理由は内部の語を含むので
本文に出さず、印の `detail` と成果物〔実行の詳細の監査ログの元の JSON。#2403〕に残す。#1833）で、古い版・見つからない根拠の
件数も「確認が必要な点」に足す。矛盾で外した段落の後は、中身をすべて外した節の見出しを消し、番号つきの一覧の残った項目の
番号を詰める（#2692）。外した段落の直後の段落が前の段落を受ける接続の語（「したがって、」「そのため、」など。「ただし」
「なお」は残す）で始まれば、その語を外す（#1833）。番号つきの一覧の中の操作の段落（操作の指示か指示の文末の形〔「…する。」
「…してください。」〕。`answer_passages.is_operation_step`）が矛盾で外れた・未確認なら、それより後の操作の段落は本文に残し、
警告の印（「前の手順が根拠と矛盾」`after_contradicted_step` /「前提の手順が未確認」`after_unverified_step`）を付ける
（`dependent_steps`。#2604）。外した操作を名前だけで示す番号つきの項目（`withheld_labels`）は、その操作を矛盾で外したときだけ
外す（#2692）。事実・結果を述べる段落の後は何も付けず、事実を述べる段落・出典の行・質問など主張ではない段落・番号の無い
箇条書き・一覧の外の段落・名前だけの項目には付けない。項目の行に中身を書いた一覧と、名前の項目（「1. **確認**」）の下に中身が
1 行ずつの一覧は順の手順として後の項目へ広げ、名前の項目の下に中身が複数の行ある一覧（並列の方法・場合分け）は、各項目を
Markdown の見出しで切った節と同じく独立させ、同じ項目の中だけで付ける（語ではなく一覧の構造で決める。#2735）。決定的な
検査の error（要求の不足は `requests`、手順・影響範囲・業務ガイドの版は `guide_check`）と、根拠を 1 件も読めない
（`no_evidence`。すべての主張の段落を未確認にする）は本文を置き換えず、回答全体の警告（`answer_warnings`）にする（warning の
指摘は出さない）。RAG もモデルが省いて書いた根拠の ID を、1 つの根拠に決まるときは結び付ける（モデルの ID で決まらないときは段落そのものが書いた「【証拠 ID …】」で結び付け、引用した ID がすべて根拠に決まる段落はモデルが `citation_error` を返しても出典の誤りにしない〔`unassessed`〕。#1404。根拠の ID を書かない結論・まとめの段落の出典が決まらないときは、回答の他の段落が引いた根拠だけで確かめ直し、裏付けられれば `supported` にする。#1412。質問・回答が対象〔会社・台帳・版・年度・部署〕を限定しているときは、範囲の外の根拠との違いを `contradicted` にしない。#1413）。検証そのもの（接続・呼び出し・応答）が失敗したら、または
接続が検証を提供しなければ、`unvalidated` にして回答は消さずに「この回答は資料との照合を終えられず、本質問の条件への該当は確認できていません。」を足す（RAG の「参考情報」の説明と同じ言い方。#2707）。RAG が時間の上限
（`RAG_ANSWER_VALIDATION_TIMEOUT_SECONDS`、既定 540 秒。接続 `rag` の待ち時間 630 秒より短い）までにどの段落も確かめ終わら
なかった（`status=timed_out`）ときも同じ（理由 `validation_timed_out`）。一部の段落だけを確かめた（`status=partial`）ときは、
確かめ終わった段落の判定で回答を組み立て、確かめ終わらなかった段落（`unassessed`）は本文に残して未確認の印を付ける
（時間切れで内容を落とさない。#2700）。RAG の根拠を
使っていない Run は、Agent が RAG の根拠のツールを持てば `unvalidated`（`no_rag_evidence`）にして「資料と照らし合わせて
確かめていません」を足し、持たなければ `skipped` で回答はそのまま。ただし利用者への確認の質問だけの回答は、
資料の主張を含まないので `skipped`（`clarification_only`）にして注記しない（#1306。質問の前置き〔「条件を 1 つ確かめさせてください。」「その点がまだ分かっていません。」〕と、答え方の案内・確かめる理由の括弧〔「（「個別」または「グループ」でお答えください。…）」〕も質問の一部として扱う。操作・数量・逆接を含む文は除く。#1454。あいさつ・お礼の決まった文〔「こんにちは。」「お疲れさまです。」「ありがとうございます。」。語を固定する〕も前置きとして扱い、「こんにちは。ご用件をお知らせください。」は確認の質問にする。あいさつだけで質問・依頼の無い回答は確認の質問にしない。#1879。お礼への返事〔「どういたしまして。」「お役に立ててよかったです。」〕と、くだけたあいさつ〔「よろしくお願いします。」〕も前置きにする。#1929）。資料に無い・答えられないことだけを述べる回答〔すべての段落が資料に記載が無い・答えられないと述べる文〔前に調べたことだけを述べる節が付いたものを含む〕・拒答に添える結びの決まった文〔「ご了承ください。」「申し訳ありません。」〕・見出しのどれかで、不足の文が 1 つ以上あるもの。数量・逆接・操作を含む文は除く。`answer_passages.is_absence_only`〕も、資料の主張を含まないので `skipped`（`absence_only`）にして注記しない（#1879。画面の「資料で確かめた結果」は「検証の対象外」と答えられないことだけを伝えた旨を出す。Run の事実の検証の結果は `skipped` で、「根拠を使わずに答えた」に数えない）。会話の続きで、今のターンのモデルが根拠・データのツールで結果を得ずに答えた（「前の回答を表にして」などの整形・言い換え）ときは、前のターンの検証で回答の段落を裏付けた根拠（#1837 の引き継ぎの根拠）があれば、その根拠だけで検証する（#1879。`answer_validation.carried_evidence_groups`。接続は前のターンの検証の接続〔`<接続>__rag_search` の名前で引く〕。前のターンの事実を言い直した段落は裏付けられ、新しい主張は確かめられない段落として印が付く。引き継ぐ根拠が無ければ今までどおり `no_rag_evidence`）。主張ではない段落（見出し〔句点で終わらない短い名詞句〔「1. 人事評価システムの重要度」「補足（資料の記載）」「根拠」。項目・区分・資料の名前か「…とき」「…か」で終わり、述語・操作・数量・言い切りの「は」を含まないもの〕と、以降の箇条書きを導くだけの導入の文〔「根拠は次のとおりです。」〕・礼と締めくくりの定型の文〔「以上、資料に基づく回答です。」。語の組み合わせを固定する。#1833〕を含む。「1. 運行管理課長」「重要度は A です」は主張のまま。#1469。番号つきの行は段落だけでは決めず、次の行がその下の内容〔字下げした行・箇条書き〕のときだけ見出しにし、番号の行が続く一覧の項目〔「1. 変更の内容と理由」〕は主張のまま。#1481 / #1482。箇条書きの行〔「- 」「・」〕も同じく、次の行が下位の内容〔より深く字下げした行・「→」の行〕のときだけ見出しにし、同じ字下げの箇条書きの行が続く一覧の項目〔「ご確認いただきたい点」の「- 組織規程（ご担当部署の承認者）」〕は主張のまま。`list_heading_lines`。#1486。両方のテストが `platform/contracts/answer-passages/headings.json` の同じ事例で確かめる〕・
出典の行〔「セクション:」「ページ:」などの位置のラベル・根拠の ID の括弧を含む。#1317。要素の定位子（`*Locator*: …`）・〔証拠 ID: …〕・文書名と場所の括弧だけの行〔「**システム変更手順書**（第 2章 申請）」〕も。場所の後に本文が続く行は主張のまま。#1370。「場所：」「該当箇所：」などの場所のラベルの行は、値が文書の場所〔章・節・頁・ファイル名と節の題〕だけのときに限る〔「- 場所：第 2 章「締め日と支払日」」。「場所：本社 3 階の会議室」は主張〕。#1833。規則は RAG と同じで、両方のテストが `platform/contracts/answer-passages/citation-lines.json` の同じ事例で確かめる〕・Markdown の表の区切りと
見出しの行〔#1317。表の本文の行は主張のまま〕・利用者への質問・資料に記載が無いことだけを述べる文と答えられないと
言い切る拒答の文〔#1317〕・業務ガイドが見つからなかったことだけを述べる文〔作業の経過。前置きに主張・数字・引用を含む文と操作の指示を含む文は除く。不足の印にはしない。規則は RAG と同じで、両方のテストが `platform/contracts/answer-passages/process-notes.json` の同じ事例で確かめる。#1452〕・最終の検証が足す節）は、RAG の判定にかかわらず外さず、印も付けない（#1306。`answer_passages`
が決定的に判定し、成果物の段落に `non_claim` を付ける。迷うもの・数量や操作を抱き合わせた文は主張として扱う）。RAG の
`rag_validate_answer` も、見出し・出典の行・表の区切りと見出し・質問は監査せず `claims` に含めない。矛盾の段落を外した後に
見出し・出典・表の形だけが残れば、それも消す。利用者に現場のデータ・記録（ログ・設定値・明細など）の確認を求める段落
には、判定とは別に `environment_check` を付ける（#1317。主張のまま確かめる。回答の対応の「実データの確認」の印）。
利用者に聞けば分かる条件（自分のロール・権限・承認を得たか、質問で特定されていない対象のシステム）を確かめるだけの段落は、
記録そのもの（ログ・履歴・設定値・件数・明細）を確かめる語が無ければ `environment_check` にしない（#1453。RAG も
この種の確認を現場の実データではなく適用の条件にし、`rag_search` の対応は `conditional` になる）。
図の見た目の段落（#2678）: RAG の `rag_validate_answer` は、図の見た目だけで決まる段落（要素の配置・矢印の向き・つながり・分岐への所属など、図の根拠〔`evidence_type` が `figure_description` / `ocr`〕の本文が明示しない内容）を `figure_only`（`chunk_id` はその図）と返し、図の本文の語の並びから推した構造を `supported` にしない。図の項目だけを根拠に `supported` とした段落は、その段落と図の項目だけで 1 回確かめ直し、`figure_only` / `contradicted` なら替える（`support_check: figure_recheck`。確かめ直しで裏付けを増やさない）。`figure_only` の ID が決まらなければ段落が書いた ID、それでも決まらず根拠の図が 1 つだけならその図に結び付け（`reference_check: single_figure`）、図に結び付けられなければ `unsupported`（`not_figure` など）。RAG は画像を見ずに判定するので、画像で確かめたかは Agent が Run の記録で決める（`answer_validation.apply_figure_readings`）: その Run の `rag_read_source(include_image=true)` の結果の `content_images` に、その図（document_id・chunk_id）の画像が `model_input=attached` で残っていれば（前のターンの検証で画像で確かめた図も引き継ぐ）`supported`（`figure_check: image_read`・`figure_image` に image_id・版 `chunk_set_id`・領域 `region`。RAG の判定は `rag_status`）にして本文に残し、本文の後に図の画像で確かめた旨の注記を足し、回答の根拠の図に `image_reading` を付ける（成果物の `figure_confirmed`）。届いていなければ（画像に対応しないモデル・画像の取得の失敗・画像を読んでいない）`unsupported`（`figure_check: image_not_read`）にして本文に残し、未確認の印（`figure_not_read`「図の画像を読めておらず確かめられない」）を付ける（#2738）。画像がモデルへ届いた図の根拠は、検証に渡す根拠の上限の内で先にする。
回答の対応（#1305）: Control Plane は最終の検証の後、回答の成果物（`kind="answer"`）の `outcome` に、RAG の回答の記録
（AnswerEnvelope）と同じ語彙の対応（`answered` / `conditional` / `needs_clarification` / `needs_environment_data` /
`needs_human` / `insufficient_evidence`）を、モデルを呼ばずに決めて残す（`answer_outcome`。内容は `value`・決めた手がかりの
`basis`・最後の `rag_search` の対応 `rag_outcome`・不足の印 `signals`）。決め方は上から順に、空の回答 → 拒答、確認の質問
だけの回答 → `needs_clarification`、最終の検証が業務ガイドの手順・影響範囲・版の検査の error を回答全体の警告にした → `needs_human`（`basis` は `validation_guide_check`。どの段落の手順が誤りかを決められないので人が確かめる。本文は出す。#1491・#2738）、最終の検証が残った主張の段落をすべて未確認とした（根拠で確かめた段落が 1 つも無い。`no_evidence` を含む）か主張の段落をすべて矛盾で外した → 拒答（`basis` は `validation_nothing_confirmed`。本文と印は出す。#2738）、回答が最後の `rag_search` に拠る（その後に
根拠を集め直していない）ならその対応（`needs_environment_data` の後に現場のデータの道具が成功したら `answered` /
`conditional`、確認を求められたが質問だけでなく答えたら `conditional`、`answered` でも回答が不足を示せば `conditional`）、
`rag_search` に拠らず業務ガイドの判断が人への引き継ぎなら `needs_human`、それ以外は Agent が自分で組み立てた回答として
段落で決める（現場のデータの道具を使っておらず、Run のどれかの `rag_search` が `insufficient_evidence` を返し、回答の最初の段落〔結論。見出し・出典を除く〕が質問の対象を資料から確かめられないと述べたら、調べた資料の一覧・近い規定の紹介が続いても `insufficient_evidence`〔`basis` は `absence_conclusion`。参考の説明は本文に残す。#1454〕。それ以外は、主張の段落〔最終の検証が矛盾で外した段落と、実データの確認を促す段落〔RAG の判定 `data_confirmation` か、
外していない `environment_check` の段落。#1317〕を除く〕が無ければ、実データの確認を促していれば `needs_environment_data`、
それ以外は拒答。主張があり、不足の印〔資料に記載が無い文・拒答の文〔結論の段落が質問に答え〔補足の外の主張に、最終の検証が根拠で確かめた段落がある〕、不足の文が補足の段落〔見出しが「補足」「注記」などの節と「なお、」で始まる段落〕にだけあるときは、質問が求めない周辺の事柄の不足として印にせず、記録の `supplement_absence` だけを付ける。#1475〕・主張に添えた質問・モデルが書いた確かめられない点の節・
最終の検証が矛盾で外した段落〔`contradicted_removed`〕・未確認の印の段落〔`unverified_passages`。#2738〕〔外した段落・未確認の段落が補足の段落にだけあり、結論の段落に最終の検証が根拠で確かめた段落があるときは印にせず、記録の `supplement_withheld` / `supplement_unverified` だけを付ける。#1491〕・決定的な検査の error・実データの確認・業務ガイドの条件が分からないまま答えた〔`guide_conditions`。
最後の `rag_search` の後の最も新しい `rag_lookup_guides`〔モデルか Control Plane の照合〕の最上位の業務ガイドの判断が
`clarify` / `branch` なのに、確認の質問だけで答えなかった。#1321・#1322〕〕があれば `conditional`、無ければ `answered`。警告の印〔規則の検査・続く手順〕だけなら不足の印にせず、記録の `rule_warnings` だけを付ける。#2738）。決められなければ `outcome` を付けずに回答を保存する。業務支援の評価（#1289 の D）は
これで採点する（`rag/evaluation/business-support/README.md`）。モデルに対応を申告させる（構造化出力）方式は、回答の形が
Agent ごとに変わり、申告と本文の食い違いの扱いも要るため採らない。
根拠・データのツールを呼ばずに作った回答の拒答（#1835）: `AGENT_TOOL_GROUNDING_REQUIRED`（既定 true）が true のとき、
その Run でモデルに根拠・データのツール（MCP 接続のツール。一覧・推薦の `rag_list_search_answer_profiles` /
`nl2sql_list_profiles` / `nl2sql_recommend_profile` と Control Plane が呼ぶ `rag_validate_answer` /
`nl2sql_cancel_requirement` を除く。Control Plane のツールは数えない。業務ガイドの照合 `rag_lookup_guides` は当たる業務
ガイドを返したときだけ数える〔業務ガイドが無いことは資料が無いことではない。0 件の結果には資料の根拠を集める次の手
`NO_GUIDE_HINT` を足す。#2504〕）を渡した Run で、モデルがどれも呼ばずに最終の回答を
作ったら、回答を確定する前に（最終の検証の前に）1 回だけ「業務の事実はツールで確かめてから答える。曖昧なら確認の質問だけを
返してよい。ツールで調べても確かめられないなら答えられないと答える」を利用者のメッセージとしてモデルへ返して Run を続ける
（差し戻しは調べていない回答にだけ行うので、範囲外かを調べずに決めてよいとは書かない。回答の結論が範囲外・答えられないと
述べる回答〔調べずに判断した拒答。`tool_grounding.declined`〕は、範囲の内外と根拠の有無をツールの結果で判断させる頭の文
`RETRY_HEAD_DECLINED` で差し戻す。#2608。system の指示もこれに合わせ、根拠・データのツールを渡した Run では、範囲の内外と根拠の有無をツールを呼ぶ前に決めず、範囲外に見える質問も先に調べ、根拠が見つからないときだけ答えられないと答えさせる〔`tool_grounding.SEARCH_FIRST_INSTRUCTION`。組み込みのスキルの指示と回答前の確認の方針も同じ。以前の「業務の範囲外の質問のときは答えられないと答える」は調べる前の拒答を認めていた。#58〕。同じ
SDK の Agent・ツール・予算。利用量は前の分に積む。差し戻しの回数は Run の metadata `tool_grounding` に残し、承認待ちから
再開しても 2 回目を差し戻さない）。それでも呼ばなければ、本文を出さずに決まった拒答の文（「社内の資料・業務データで
確かめられなかったため、お答えできません。推測ではお答えしていません。」＋業務 Agent の説明とスキルの表示名＋渡したツールの
種類に合わせた質問の例）に置き換え、回答の対応を `insufficient_evidence`（`basis` は `no_tool_grounding`。差し戻しの後も
調べずに範囲外・答えられないと判断した拒答は `declined_without_tools`。経過のイベントと metadata の `tool_grounding` にも
`declined` を残す。#2608）、最終の検証の成果物を `skipped`（`reason` は `no_tool_grounding`）にする。品質評価のケースの結果は
回答の対応（`answer_outcome`・`answer_basis`）と、拒答が根拠・データのツールで調べる前か後か（`refusal_stage` の
`before_tools` / `after_tools`）を持ち、集計は `refused_before_tools` / `refused_after_tools` で数える（調べた結果として根拠が
無かった拒答と、調べずに拒答したものを分ける）。差し戻し・拒答と元の回答は Run の経過（イベントの
`tool_grounding`）にだけ残す。呼んだかは Run の step で数え（失敗・承認の却下・評価の dry-run・無人の Run で承認が要るため
呼ばなかったものも数える）、Control Plane が自分で呼んだもの（確認の取り消し・業務ガイドの照合）は数えない。対象外は、
確認のツールを渡さない Run（上限に達した Run）の確認の質問だけの回答・NL2SQL の SQL の生成前の確認で止まった Run・利用者が確認を取り消した Run（#1441・#1445）・根拠・データの
ツールを持たない Agent・同じ会話の前の Run（会話の履歴の上限 `PLATFORM_CHAT_HISTORY_MAX_TURNS` の中。#1818）のどれかが
根拠・データのツールを呼んで結果を得て答えた Run（「前の回答を表にして」「もっと短く」などの言い換え・整形。前のターンの事実の
確かめは、前のターンで裏付けた根拠を今のターンの最終の検証に渡す #1837 に任せる。今のターンに根拠が無くても、その根拠だけで検証する。#1879）。チャット・品質評価の Run は同じ扱い。
根拠・データのツールの実行の失敗（#2475）: モデルが根拠・データのツールを呼んだが 1 つも結果を得られず、呼び先で実行できずに
失敗した呼び出し（`mcp.*` / `external_mcp.*` / `tool.unhandled_error`。`mcp.tool_error` は呼び先が 4xx〔引数・権限・対象が
無い〕で返したものを除く）があるまま回答を確定したら、回答を出さずに Run を失敗（`runtime.grounding_tool_failed`。ツールの名前と
ツールが返した理由に「資料・業務データに根拠が無いという意味ではありません」を添える）にする（`tool_grounding.grounding_failure`）。
検索を終えられなかった回答を、資料を調べた結果の根拠の不足と読ませないため。確認の質問だけの回答・NL2SQL の SQL の生成前の確認で
止まった Run・承認で止まった Run は対象外。RAG は回答の pipeline の AI の API の失敗を、汎用の内部エラーではなく種類つきの
5xx（`RAG_ANSWER_AI_API_FAILED`。`details` は種類・例外の型・呼び先の status と error code・工程・呼び先が示した待ちの秒数だけ）で返す（REST の検索・業務ガイドの下書きの試答も同じ分類の status と文。#2667）。
差し戻しの文と本文の確認の質問は Run で分ける（`tool_grounding.retry_message`）: 確認のツール（`ask_user_confirmation`）を渡した
Run は「先に資料・業務データのツールで調べ、決まらなければ確認のツールで聞く。本文に確認の質問を書かない」（#1881）、利用者に
確認しない Run（方針 `assume` と「確認せずに回答」を選んだ Run と、無人の Run〔品質評価。利用者に聞けない。#1959〕。#1903）は
「確認の質問はせず、ツールで調べ、決まらない点は前提を示して答える（条件ごとに分ける）。業務プロファイルなどの推薦が null でも
聞き返さず、推薦の候補か一覧の説明から最も合うものを選んで調べる」（どのプロファイルで調べたかは回答に書かない。使ったツールと根拠は回答とは別に示す。#2328）、それ以外（上限に達した Run）は「曖昧なら
確認の質問だけを返してよい」。確認のツールを渡した Run と利用者に確認しない Run では、ツールを呼ばずに本文の確認の質問だけを返した回答も 1 回
差し戻す（あいさつへの返事〔回答があいさつ・お礼の決まった文を含むか、質問があいさつ・お礼だけ〕と、用件を聞くだけの回答〔「社内の規程や手順について、知りたいことをお知らせください。」「何についてお調べしましょうか？」。対象を確かめる聞き返しは含めない。`answer_passages.asks_for_request`。組み込みのスキルの「入力の種類を分ける」で、あいさつ・お礼・質問でない入力に返す形。#1929〕は除く。#1879）。差し戻しの後も本文の
聞き返しだけなら、拒答にはせずそのまま出し、経過に `text_clarification` を残す（拒答は範囲外・確かめられない質問のためのもの）。拒答した Run はフィードバックの「答えられなかった」
（`unanswered`）に数える。
回答の本文は、根拠を結び付けられるとき RAG のチャットと同じ節（要約・「資料により確認できた事項」と各項目の下の「根拠：文書 p.N」・「資料から確認できなかった事項」。本文の根拠の ID の印は文字で出さず根拠の行にする）で出す。回答の上の処理の経過にツールの段（MCP のツールの表示名 `title` があれば表示名。開くと内部名・引数・結果。#2294 / #2324）、回答の下に RAG のチャットと同じ共通の並び（`AnswerFooter`: 回答の注意書き → 根拠の塊〔「AI に渡した N 件 ／ 回答で引用 M 件」。成果物 `answer_evidence`。#2328〕→ 評価。#2707）で畳んで出し（実行の詳細は根拠の塊を別の Section「回答の根拠」に置く）、承認待ちはその場で承認・却下する
（`agent.approvals.decide` を持つ利用者だけ）。実行中・承認待ちのあいだは次の質問を送れない（前の回答を履歴に含めるため）。
Run は Agent とゴールだけを受け取る（実行先の選択は無い）。Runtime 画面は組み込み Runtime の状態（SDK の版・既定のモデル・
選べるモデル・実行できるか）と、モデル未設定のときの理由と「システム設定 > モデル」への導線を出す。

## 9. Authentication and RBAC

画面は RAG / NL2SQL と同じ共通認証（`PLATFORM_*` のユーザー・ロール・セッション）でログインする。ロールに付ける
Agent の権限（`AGENT_ROLE_PERMISSIONS`）と対象範囲（`AGENT_ROLE_AGENTS`）は
システムテーブル（`app.system_schema`。#751）が作る。capability は従来の viewer / operator / approver / auditor / admin に対応し、
利用者（Cookie のセッション、local はローカル利用者）から `ActorPolicy` を作って Run・監査・承認・成果物・SSE・
`GET /agents` の絞り込みに流す。Cookie のないリクエストは 401（#750 で header / JWT / 外部 policy の認可を削除した）。
検索・回答プロファイルの判定は RAG が Run の利用者のサービストークンで行うため、Agent は検索・回答プロファイルの対象範囲を持たない（#750）。
「AI 活用」（チャット・実行履歴・承認）の Run と会話は、system_admin は全員、それ以外は自分の物だけ（3 製品共通。#1889）。
承認の権限（approver）を持つ利用者も自分が実行した Run の承認だけを判断し、ほかの利用者の Run の承認は system_admin が判断する。
system_admin が他の利用者の会話・Run にできるのは閲覧と運用の操作（停止・削除・承認の判断・再開）だけで、続きの質問・
確認への答え・評価・名前の変更は持ち主だけ（403。#3244。#1928 を置き換え）。
詳細は [security-rbac.md](security-rbac.md)（§4.1）。

## 10. Non-goals

- プラグイン（Skill・MCP・resource の配布単位）の登録・宣言（#1803 で削除。スキルだけを持つ）
- 外部の Agent Runtime（OpenClaw / Hermes / DeerFlow など）への配置と adapter（#754 で削除）
- 独立した planner / workflow engine
- プラグインの配布元（マーケットプレイス）の登録・取り込みと、外部 archive からの install（#1798 で削除）

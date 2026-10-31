# SQL 論理構造の再生成と SQL Assist 移植対応

## 対象と参照元

Issue #321。対象は「SQL から質問を生成」の SQL → 物理構造 → 業務論理構造 → 質問と、論理構造 → SQL の往復生成である。

参照元は [engchina/No.1-SQL-Assist](https://github.com/engchina/No.1-SQL-Assist/blob/cacd0959a5fa2a279cb1147de2d04e5a9b01815f/utils/selectai_util.py)、固定 revision は `cacd0959a5fa2a279cb1147de2d04e5a9b01815f`。
この revision では構造分析・逆生成は `utils/query_util.py` ではなく `utils/selectai_util.py` にある。

## 原文の保存と実行時の対応

`backend/app/features/nl2sql/prompts/sql_assist/` に関連する4プロンプトを保存した。静的本文は原文を保持し、実行時の SQL / schema / 生成結果の文字列結合だけをサービス側へ分離した。質問の変数は `{logical_structure}` に改名した。`manifest.json` に revision と SHA-256、`LICENSE` / `NOTICE` に Apache-2.0 の帰属と変更範囲を記録する。

| 参照元の処理 | 保存した原文 | 現在の実行 |
|---|---|---|
| `_SQL_STRUCTURE_ANALYSIS_PROMPT` | `structure_analysis.txt` | 元 SQL から全句・列順・alias・CTE・サブクエリ等を抽出 |
| `_rev_generate_async` の `prompt` | `logical_structure.txt` | 物理構造と元 SQL、schema の論理名・コメントから業務論理構造を生成 |
| 同関数の `question_prompt` | `business_question.txt` | 論理構造から全条件を保持する日本語質問を生成。従来の1文への圧縮制約を廃止 |
| 同関数の `glossary_prompt` | `glossary_question.txt` | `use_glossary=true` の場合だけ逆正規化規則と用語集を適用 |
| 再構築可能な仕様を Text-to-SQL に戻す意図 | `reverse_prompts.py` の `STRUCTURE_TO_SQL_PROMPT` | 専用 `/api/nl2sql/reverse/sql` で編集した論理構造だけを要件にして SQL を生成 |

原版の用語正規化は質問生成後に別 LLM 呼び出しを行う。移植後は同じ原文規則を質問生成時に合成し、追加呼び出しと生成済み条件の再書き換えを削減する。通常は3回、SQL 再生成は明示操作で1回の Enterprise AI 呼び出しとなる。

原文の「100%」は情報保持の目標であり、LLM が全 SQL に対して意味同値性を保証するという意味ではない。Gradio・外部 LLM provider・OCI GenAI chat は移植せず、既定スタックの React / FastAPI / OCI Enterprise AI へ対応付けた。DB・デプロイ・モデル設定等の他領域は本変更の対象外。

## 不具合修正と補正

- `質問を生成` 完了後は `SQL分析・質問候補` タブへ移り、表示中の作業ではタブへフォーカスを移す。画面は `SQL入力・生成` / `SQL分析・質問候補` の2タブ・2ステップで、質問候補は論理構造・再生成 SQL の後に表示する（Issue #325）。旧 `result` タブの保存状態は草稿を保持して統合先へ移行する。
- SQL Assist の「質問を生成」に相当する第二段階の `logical_structure` を編集欄へ表示し、質問生成・SQL 再生成に使用する（Issue #423）。スキーマ情報の論理名・COMMENT に基づく業務名で表示する。第一段階の `sql_structure` は物理識別子・alias・式・条件を後段へ伝える内部の中間結果として保持し、編集欄へ代入しない。
- 各生成段階は Responses API Structured Outputs（`text.format` の `json_schema` / `strict:true`）を使用する。JSON 全文を送信した Schema と Pydantic で検証し、Markdown は `logical_structure`、自然言語は `question` に格納する。JSON 外の本文・fence・不正 JSON・空結果は成功として扱わない（Issue #431）。後段が失敗しても完成した `sql_structure` は残し、簡易質問であることを警告する。
- 未設定・前段失敗時は簡易構造に元 SQL 全文を保持し、正規表現の要約で CTE・関数・リテラル等が失われないようにする。埋め込まれた元 SQL と編集した要約が矛盾する場合、再生成時に推測で解決しないよう指定する。
- 原版の `LIKE → を含む` の一律変換を補正し、前方/後方/部分一致、`%` / `_` / `ESCAPE` を区別する。括弧、AND/OR/NOT、JOIN ON / WHERE、関数引数、CASE、window frame、bind 変数、引用符等の保存も追加で指定する。
- この画面ではスキーマ情報を常に利用し、用語集の選択肢を設けず生成・再生成とも `use_glossary=false` を送信する。`ReverseSqlRequest` / `StructureToSqlRequest` の既定値も false。既存 API の明示 true は互換性を維持する。
- 実行時補正を段階別に適用する。第一段階は物理識別子を保持、第二段階はスキーマ業務名へ変換、第三段階は意味を保持して自然言語化する。物理識別子や alias の字面保持を自然言語質問へ強制しない。質問候補は自然言語の質問の全文を表示し、生成元ラベル・SQL 操作説明・参照表・処理手順のカードを並べない。
- SQL 再生成では SELECT を途中から抜き出したり、最初のセミコロンで切断したりせず、全出力を既存 AST / 許可表 / 危険関数の安全検証へ渡す。

## API と状態境界

`POST /api/nl2sql/reverse/sql` は `logical_structure`, `profile_id`, `use_glossary` だけを受け、`sql`, `explanation`, `source`, `warnings` を返す。元 SQL を別 field で送信して生成要件を上書きしない。`menu.sql_to_question` と既存 Profile アクセス検証を適用し、Profile の許可 object 範囲で schema context と生成 SQL を検証する。

この API は DB 実行・結果照合・履歴保存を行わない。未設定・空入力・不正応答・不許可 SQL は失敗として扱い、代替 SELECT を成功として返さない。Enterprise AI 通信失敗は再試行可能な日本語 `502` を返す。

論理構造草稿は既存 `useWorkspaceState` の同一タブ `sessionStorage`（8時間 TTL、ユーザー/DB/ページ/Profile 隔離）に明示登録する。再取得は草稿を上書きしない。SQL 再生成結果はメモリ上のスナップショットで、生成日時と未実行を示す。構造編集後は旧 SQL・質問候補に旧結果表示を付ける。再読込では草稿と明示登録した質問候補の最小スナップショットを復元し、LLM/SQL を再送しない。認証/DB context の破棄時はリクエストを中止する。

## 検証と限界

- `test_nl2sql_structure_roundtrip.py`: 原文チェックサム、3段階の情報伝達、編集構造だけの送信、SQL 全文保持、越権表/危険関数/複文拒否、部分失敗、未設定、用語集 ON/OFF、API アクセスと `502`。
- 既存 `test_nl2sql_reverse_deep_steps.py` / `test_nl2sql_reverse_access.py` と legacy の reverse 関連テストを更新。
- `nl2sql-workflows.spec.ts` の desktop / mobile-375: 成功後タブ・フォーカス、構造編集と再生成、失敗時草稿保持、未実行/旧結果表示、再読込、重複送信なし、主要な空/読込/エラー状態、横 overflow、150% zoom。

自動テストは Enterprise AI と API を fake/mock 化した契約検証である。Issue #421 では `select * from employee` の実 Enterprise AI 応答が JSON ではなく原版形式の Markdown で返ることを確認した。修正後の全生成経路でも SELECT句・FROM句を保持し、`source=oci_enterprise_ai`、警告なしで成功した（テスト用 Profile、DB 実行なし）。Issue #423 では人工の `DEMO.EMPLOYEE` スキーマ（論理名: 従業員情報）だけを使用した実モデル検証で、論理構造の FROM が `[従業員情報] e`、質問が「すべての従業員情報を教えてください。」となることを確認した。用語集・実 DB データは使用していない。実モデルと Oracle のデータで結果の同値性を比較する live 受入は別途必要。任意の SQL について「100% 同値」を証明したものではない。

## 自然言語の質問から SQL を生成（Issue #425）

「質問候補」の「自然言語の質問から SQL を生成」は、表示中の `question` を唯一の生成要件にし、選択 Profile のスキーマ情報を使って SQL を生成する。元 SQL や編集した論理構造は送らない。用語集は使用しない。

`POST /api/nl2sql/reverse/question-sql` は `question` / `profile_id` のみを受理する。余分な field は拒否し、既存の `menu.sql_to_question` と Profile アクセス検証を適用する。生成応答は論理構造からの生成と同じ `sql` / `explanation` / `source` / `warnings`。生成・Pydantic 検証・全文の AST/許可表/危険関数検証は共通 helper を使うが、未編集元 SQL の復元 shortcut は質問生成に適用しない。

質問候補内の生成結果は上の論理構造からの生成結果と独立して保持し、生成日時と「未実行」を表示する。送信中は他の生成操作も無効化し、失敗時は質問と前回の成功結果を残して再試行を可能にする。元 SQL・Profile・質問の変更で結果を解除する。質問候補は本文・生成時の論理構造・元 SQL・警告・生成日時だけを既存の同一タブ一時保存へ明示登録する（8時間 TTL、ユーザー/DB/ページ/Profile 隔離）。往復・再読込後は前回結果と元の生成日時を表示し、手編集した論理構造との差分警告を維持する。元 SQL の変更で候補を解除し、別 Profile の候補は流用しない。同じ Profile に戻った場合はその Profile の候補を復元する。参照情報の再取得失敗時も候補を保持し、再検証が成功するまで生成操作を無効にする。生成 SQL のスナップショット自体は再読込で復元せず、自動生成・SQL 実行・履歴保存も行わない。

`test_nl2sql_question_to_sql.py` と `nl2sql-workflows.spec.ts` で質問だけの送信、用語集不使用、Profile 認可、危険/不許可 SQL、不正/空応答、未設定、独立した結果、送信中・失敗・再試行・再読込を検証する。

### 生成失敗の分類と段階 retry（Issue #429）

質問段階の原文は自然言語の質問のみを要求するが、旧実装は JSON object だけを受理していた。架空 EMPLOYEE の実接続で 3.68 秒後に正常な日本語質問が返っても JSON 検証失敗になることを再現した。Issue #429 では一旦自然言語と JSON の両方を受理した。Issue #431 で送信時の形式を Structured Outputs に統一し、現在は Schema に従う JSON `question` のみを受理する。原文の自然言語本文はこの field 内に保持する。壊れた JSON、空文字、型不正は成功として扱わない。

各段階の空・形式不正、接続失敗、timeout、429/500/502/503/504 は同じ段階だけを再試行する。回数は設定 `oci_enterprise_ai_max_retries` に従い、最大2 retry（合計3回）、1秒/2秒の待機。HTTP 層の retry をこのフローでは0にして多重 retry を避ける。401/403、その他の非一時的な HTTP エラー、refusal、未完了応答は retry しない。全3段階で最大30分（設定 timeout × 3 が短ければその時間）の予算を共有し、各呼出しへ残り時間以下の timeout を渡す。完了した段階は再送しない。

警告は失敗段階、原因、試行回数、保持している結果、再試行方法を示す。質問だけ失敗した場合、生成済み SQL 論理構造を保持する。ログ `reverse_sql_stage_failed` には stage / reason / attempt / elapsed_ms を記録し、SQL・schema・生応答・credential を含めない。

主ボタンは `SQL 分析・質問生成`。押下時に `SQL分析・質問候補` タブへ移り、全結果領域を共通 Skeleton と処理時間表示に切り替える。旧結果・草稿は保持したまま非表示にし、操作と二重送信を抑止する。API 失敗時は入力タブの action error へ戻り、結果タブで前回の内容も確認できる。

全生成機能の Schema と互換性境界は [Responses Structured Outputs](./enterprise-ai-structured-outputs.md) を参照。

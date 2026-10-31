# 処理の説明とモデル用途の対応（#3626）

この表は今回確認・変更した工程群の対応を記録するものです。全画面の全文章を網羅した一覧ではありません。利用者向けの工程名は「対象＋動作」、実行状態は進行中・完了・失敗として表示します。内部の span 名・イベント名・API 契約は表示名と分けて保持します。

| 製品・画面 | 工程と実際の動作 | 表示の出典・変更後の例 | 設定・後続への影響 | 根拠・回帰テスト |
| --- | --- | --- | --- | --- |
| 共通・チャット／実行詳細 | 実際のモデル呼び出しに固定用途を付与 | `ui/src/lib/trace-labels.ts`、`trace-tree.ts`。用途を主表示、モデル名を展開後の補足に表示 | モデル選択、入力、呼び出し回数は変更しない。用途がない旧記録は「モデルを呼び出す（用途の記録なし）」 | `backend_core/observability/model_purpose.py`、`backend_core/tests/test_model_purpose.py`、`ui/tests/trace-tree.test.ts` |
| Agent・チャット／実行履歴 | 次の操作の選択、ツール呼び出し、承認待ち | `agent/frontend/src/lib/i18n.ts`。「質問に応じた次の操作を選ぶ」「選ばれたツールを呼び出す」 | 実際に選んだツール名と承認状態を補足。ツール権限や承認判断を変更しない | `agent/backend/app/features/agent/builtin_runtime.py`、`tests/test_runtime_tracing.py`、frontend `e2e/chat-progress.spec.ts`、`src/lib/chat-progress.test.ts`、`run-history.test.ts` |
| RAG・チャット／検索／実行履歴 | 質問の理解、別名追加、確認・回答ルール選択、検索表現の作成 | `rag/frontend/src/lib/answer-process.ts` と `i18n.ts`。「一致する用語の別名を検索文に加える」「確認と回答に使うルールを選ぶ」 | 適用された用語・ルール、用途別の採用結果を表示。工程名から新しい適用条件を作らない | engine `generation/answering.py`、backend `rag/span_display.py`、frontend `e2e/answer-progress.spec.ts`、`chat.spec.ts`、`search-review.spec.ts`、`run-history.spec.ts` |
| RAG・検索／回答詳細 | 根拠検索・並べ替え・原文の拡張・不足箇所の補検索・回答生成・資料との照合 | 同じ工程マッピングをチャット、検索、履歴で共用。モデル用途は「質問から絞り込み条件を読み取る」など具体的な目的で表示 | 記録された件数、予算、追加根拠、失敗・省略の事実を保持。成功していない処理を完了扱いにしない | engine `generation/answering.py`、`generation/grounded.py`、`evaluation/answer_validation.py`、backend `tests/test_model_purpose_calls.py`、frontend `src/lib/answer-process.test.ts`、`answer-progress.test.ts`、`process-failure.test.ts` |
| RAG・文書取込／品質評価 | 図の説明、項目抽出、要約、回答の評価に使うモデル呼び出し | backend の実呼び出しと engine の呼び出し箇所で用途を記録し、共通 trace 表示へ渡す | job の段階・処理件数・取消・失敗の状態遷移は維持。モデル用途だけを追加する | backend `rag/ingestion.py`、`rag/vision.py`、`rag/evaluation_screens.py`、`tests/test_ingestion_progress.py`、`tests/test_model_purpose_calls.py` |
| NL2SQL・チャット／実行履歴／DB 管理チャット | 参照表の整理、SQL 生成、安全検査、実行、結果整形 | `nl2sql/frontend/src/lib/i18n.ts`。「質問と参照できる表を整理する」「SQL の種類と参照範囲を検査する」「SQL の実行結果を表示用に整える」 | 生成方式・安全検査の省略状態・実行結果を保持。用途は「質問に答える SQL を作る」など | backend `features/nl2sql/service.py`、`oracle_adapter.py`、`trace_spans.py`、`tests/test_model_call_purposes.py`、frontend `tests/e2e/sql-chat.spec.ts`、`history-management.spec.ts`、`tests/sql-chat-progress.test.ts`、`db-admin-chat.test.ts` |
| NL2SQL・関連 job／設定 | 要件確認、定義の作成・更新、評価、接続確認などの実モデル呼び出し | 各呼び出しを固定の用途で包み、モデル名だけで目的を説明しない | Prompt・機能引数・返却値は維持。接続確認は通常の生成用途と分ける | backend `ontology_build.py`、`ontology_definition_workspace.py`、`ontology_markdown_workspace.py`、`ontology_router.py`、`quality_evaluation_service.py`、`reverse_generation.py`、`sql_requirement.py`、`features/settings/router.py`、`tests/test_model_call_purposes.py` |

上表の `ui/`、`backend_core/` はそれぞれ `platform/packages/` 配下です。製品 backend の `tests/` は各製品の `backend/tests/` を指します。

## 確認したが今回の表示変更対象にしないもの

- 共通設定の通常の読み込み・保存・アカウント管理はモデルの処理ではありません。`platform/packages/system-settings/src/` の各画面にある読み込み状態と保存結果は、モデル用途に置き換えません。接続確認が実際にモデルを呼ぶ箇所だけ用途を記録します。
- 文書取込などの job の処理件数・進捗率は、実際の job 記録を維持します。用途の追加で進捗率や完了条件を変えません。
- 動的なツールの正式表示名、利用者が設定する名前、外部システムの生ログは固定の工程名へ書き換えません。固定工程名と補足情報を分けます。
- 古い記録に用途を推測して埋め戻しません。用途の未記録と実際の処理失敗を区別します。
- この対応表と決定論的テストは実環境のモデル品質・応答時間を証明するものではありません。今回の画面検証は API mock を使い、有料モデルは呼んでいません。

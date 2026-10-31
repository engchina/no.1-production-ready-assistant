# 検索・回答の能力の一覧（必須 / 任意と縮退）

RAG の検索・回答の経路（`POST /api/search`・チャット・MCP の `rag_search` / `rag_retrieve_evidence` が通る `app/rag/pipeline.py` → `app/rag/answer_engine.py` → `rag_engine`）が使う能力ごとに、次の 4 つを記録する（#3262 の最初の段）。

- (a) 回答に**必須**か**任意**か。ここでは「使えないとき回答を止める（エラー・拒否）」を必須、「縮退して回答を続ける」を任意と呼ぶ。設定で切れる能力は「任意（設定）」と書き、設定で有効なときに失敗したときの振る舞いを (b) に書く。
- (b) 使えない・失敗したときの振る舞い（エラー / 拒否 / 縮退して続ける）。
- (c) 失敗の記録の場所（下の「記録の場所」の略号）。
- (d) 縮退した結果を、精密な結果（縮退していない結果）のように見せることがあるか。

調べ方: commit `6635e0411a`（main。#3279 の merge の後）のコードを静的に読んだ。pytest・実 Oracle・実 LLM での確認はしていない。コードで確かめられなかったことは「未確認」と書く。行番号は変わるので、file と関数の名前で示す（`rag/backend/tests/test_answer_capability_inventory.py` が、この文書の file と名前がコードにあること、`degraded_steps` の処理の種類がコードと一致することを確かめる）。

#3284・#3286 で、例外の文を診断に残していた経路と、`DS` に入っていなかった縮退を直し、該当の行を更新した（コードの静的確認と決定論のスタブの pytest。実 Oracle・実 LLM では未確認）。

この文書は現状の一覧で、契約（どの能力を必須にするか）は決めない。契約の差分は #3262 の後の段（#3250〜#3253 の後）で決める。

## 記録の場所（略号）

| 略号 | 場所 | コード |
|---|---|---|
| `DS` | 回答の診断の `degraded_steps`（処理の種類・回数・例外の型の名前。#3259）。根拠 0 件のときは `zero_evidence.degraded_steps` にも入る。回答エンジンの前の段（`pipeline.py`）の縮退も、同じ記録を回答エンジンへ渡して入れる（承認済み FAQ からの回答にも入る。#3286） | `rag/backend/app/rag/degraded_steps.py::DegradedSteps`・`rag/backend/app/rag/answer_engine.py::_SearchState.record_degraded`・`rag/packages/rag_engine/rag_engine/dependencies.py::report_degraded` |
| `ZE` | 根拠 0 件の診断 `zero_evidence`（0 件になった段・検索の記録・外した自動の条件） | `rag/backend/app/rag/answer_engine.py::_zero_evidence_diagnostics` |
| `GD` | 検索の診断の `guardrail_degraded`（OCI Guardrails で検査できなかった） | `rag/backend/app/schemas/search.py::SearchDiagnostics`・`rag/backend/app/rag/guardrails.py::GuardrailResult` |
| `DX` | 回答の診断のその能力の項目（`graph_expansion`・`section_expansion`・`request_coverage`・`answer_validation`・`strategy` など） | `rag/backend/app/rag/answer_engine.py::_outcome_from_result` |
| `ST` | 回答の診断の `execution_steps`（rag_engine の工程の `status` の文言と詳細の行）。rag_engine の工程の span（`rag.engine.stage`）の表示にも同じ値が出る。span の状態は例外のときだけ失敗で、縮退の文言だけでは成功のまま | `rag/packages/rag_engine/rag_engine/generation/execution_record.py::_execution_step` |
| `SP` | backend の工程の span（`rag.<stage>`）と工程の結果（`success` / `skipped` / `error`） | `rag/backend/app/rag/pipeline.py::_observe_stage` |
| `RT` | rag_engine の結果の trace だけ（backend の診断に写していない） | `rag/packages/rag_engine/rag_engine/generation/answering.py` |
| `LOG` | ログだけ（診断では「無効・該当なし」と同じ値） | — |

- MCP の応答（`rag_search`・`rag_retrieve_evidence`）は `DS` の処理の種類と回数を `degraded_steps` に出す（例外の型の名前は出さない。#3262。`rag/backend/app/mcp/tools.py::_degraded_steps`）。`ZE` は段と件数だけを `zero_evidence`（`stage`・`candidate_count`・`rejected_count`。検索の条件の項目・値・検索の記録は出さない）に、`GD` を `guardrail_degraded` に出す（#3546。`rag/backend/app/mcp/tools.py::_zero_evidence`・`_guardrail_degraded`）。Agent の組み込み Runtime は、根拠 0 件で `guardrail_degraded` か `degraded_steps` があるとき、モデルへのツールの結果に案内（`retrieval_notice`）を足し、回答の対応の `absence_conclusion` に数えない。`agent_get_run_observation` はツールの呼び出しごとに `degraded_steps`・`guardrail_degraded`・`zero_evidence_stage` を、根拠ごとに `table_binding` を写す（`agent/backend/app/features/agent/run_observation.py`）。MCP は回答の診断の `needs_human_review` から `outcome=conditional` を決める（`rag/backend/app/mcp/tools.py::_answer_fields`）。
- `SearchDiagnostics.answer` は自由な dict で、REST の schema には `DS` などの形を持たない。

## 能力の一覧

### 入口の安全チェックと前段（`pipeline.py`）

| 能力 | (a) | (b) 使えない・失敗のとき | (c) 記録 | (d) 精密に見せるか | コード |
|---|---|---|---|---|---|
| 質問の安全チェック（OCI Guardrails） | 任意（設定 `RAG_GUARDRAILS_INPUT_ENABLED`）。有効なら必須 | 有効で OCI の検査が失敗 → **拒否**（fail-closed。「安全検査を完了できない」で止め、検索しない）。無効なら検査を省く | `GD`・finding `guardrail_backend_unavailable`・監査の outcome `blocked`。pipeline の入口では工程の span を作らない（chat の入口の span は未確認） | 見せない（拒否の応答） | `rag/backend/app/rag/guardrails.py::GuardrailPolicy._inspect`・`rag/backend/app/rag/pipeline.py::RagPipeline.run` |
| 回答の安全チェック（OCI Guardrails） | 任意（設定 `RAG_GUARDRAILS_OUTPUT_ENABLED`）。有効なら必須 | 有効で検査が失敗 → **拒否**（回答を止める文に置き換える） | `GD`・`SP`（`answer_guardrail` が `error`。`guardrail_stage_outcome`） | 見せない | `rag/backend/app/rag/pipeline.py::guardrail_stage_outcome` |
| 呼び出し元の検索文の安全チェック（#2651） | 任意（呼び出し元が検索文を渡したとき） | 拒否された検索文（検査できなかったものを含む）を外し、残りの検索文で続ける | metrics の `record_guardrail_findings` だけ。`GD` には入れない | 見せない（外した検索文では検索しない）。外した数は診断に出ない | `rag/backend/app/rag/pipeline.py::_checked_caller_queries` |
| 会話の履歴での質問の書き換え | 任意（設定 `RAG_HISTORY_REWRITE_ENABLED`） | LLM の失敗・書き換えの安全チェックの拒否 → 元の質問で続ける | LLM の失敗: `DS`（`history_rewrite`）。安全チェックの拒否は `LOG` だけ（例外ではないので `DS` に入れない）。`SP` の `history_rewrite` は成功のまま | 見せない（元の質問で答える） | `rag/backend/app/rag/pipeline.py::_rewrite_query_with_history`・`_safe_rewritten_query` |
| 抽出項目の条件の読み取り（Metadata Filter Extraction。#652） | 任意（設定 `RAG_AUTO_FIELD_FILTER_ENABLED`） | 定義の読み込み・LLM の失敗 → 条件なしで検索する。LLM の出力が JSON として読めない → 条件なし | 読み込み・LLM の失敗: `DS`（`field_filter`）。工程の詳細は条件が無いときと同じ「なし」。読めない出力は記録しない（例外ではない） | 見せない（絞らない） | `rag/backend/app/rag/pipeline.py::_read_field_conditions`・`rag/backend/app/rag/field_filter_reader.py::read_field_conditions` |
| 会話の添付の根拠（#2517） | 任意 | 添付を読めない → 根拠に入れない。rerank の失敗 → 語の一致で選ぶ | `DS`（`chat_attachments`・`chat_attachment_rerank`）。添付が無いときは記録しない | 見せない | `rag/backend/app/rag/chat_attachments.py::attachment_evidence`・`attachment_units`・`select_attachment_units` |
| 編集した回答のプロンプト（#1276） | 任意 | 読めない → コードの既定のプロンプトで続ける | `DS`（`prompt_overrides`）。`provenance.prompt_version` は既定のプロンプトの版になる | 見せない（版は実際に使った既定のもの） | `rag/backend/app/rag/answer_engine.py::AnswerEngine._run` |
| 業務の名前（業務の絞り込み） | 任意 | 読めない → 業務で絞らずに続ける | `DS`（`business_names`） | 見せない | `rag/backend/app/rag/answer_engine.py::AnswerEngine._business_names` |

### 主の検索（`answer_engine.py::AnswerEngine._search`）

| 能力 | (a) | (b) 使えない・失敗のとき | (c) 記録 | (d) 精密に見せるか | コード |
|---|---|---|---|---|---|
| 質問の embedding（OCI Generative AI） | 必須 | 例外を送出 → 回答は**エラー**（0 件にしない） | `SP`（`answer` / `retrieval` が `error`。`error_type` と安全なコードだけ） | 見せない | `rag/backend/app/rag/answer_engine.py::AnswerEngine._search` |
| vector 検索（Oracle AI Vector Search） | 必須 | 例外を送出 → **エラー** | `SP` | 見せない | `rag/backend/app/clients/oracle.py::OracleClient.search_many`・`_read_vector_branch` |
| 全文検索（Oracle Text） | 必須（hybrid の方式で検索するとき） | SQL の失敗は例外を送出 → **エラー**。質問から検索語が作れないとき（`_oracle_text_query` が `None`）は全文検索を飛ばす（失敗ではない） | `SP` | 見せない | `rag/backend/app/clients/oracle.py::_read_keyword_branch`・`_oracle_text_query` |
| 全文検索の語の分割（Sudachi） | 任意 | Sudachi・辞書が使えない・分割に失敗 → 文字種の区切りだけで検索語を作る | `DS`（`text_search_tokenizer`。使えないときは回答ごとに 1 回、分割の失敗は 1 回ごと）。使えるかの確認は process ごとに 1 回（例外を cache する） | 見せない（縮退は `DS` で分かる） | `rag/packages/rag_engine/rag_engine/retrieval/text_search_tokenizer.py::_sudachi_failure`・`sudachi_unavailable`・`_content_terms`・`rag/packages/rag_engine/rag_engine/generation/answering.py::answer_question_result` |
| 全文検索の語の trace（工程の詳細） | 任意 | 作れない → 工程の詳細に「抽出に失敗（型の名前）」 | `ST` | 見せない（表示だけの trace。実際の検索語は backend が作る） | `rag/packages/rag_engine/rag_engine/generation/answering.py::build_question_text_search_info` |
| 前後の文脈・分類・1 頁目の本文 | 必須 | 例外を送出 → **エラー** | `SP` | 見せない | `rag/backend/app/rag/answer_engine.py::AnswerEngine._load_classifications`・`_load_first_page_contexts` |
| 派生の chunk の原文への置き換え（#2641） | 必須（派生の chunk が当たったとき） | 読み込みの例外は**エラー**。原文が無いときは根拠に渡さない | `DX`（`derived_evidence.unresolved_count`） | 見せない（要約の文を確かめた記載にしない） | `rag/backend/app/rag/answer_engine.py::AnswerEngine._original_anchors` |
| 自動の検索条件の緩和（#2401） | 任意（設定 `RAG_FILTER_RELAXATION_MAX_STEPS`） | 0 件のとき自動で足した条件を段ごとに外す（失敗ではない） | `DX`（`filter_relaxation`）・`ZE`（`relaxed`・`automatic_filters`） | 見せない | `rag/backend/app/rag/answer_engine.py::AnswerEngine._relax_filters` |
| 元の質問の検索の先読み（#1499） | 任意 | 失敗 → 続く検索がそのまま読む | `ST`（工程の詳細の文） | 見せない | `rag/packages/rag_engine/rag_engine/generation/answering.py::_wait_original_query_prefetch` |
| 主の検索の準備の確認 | — | backend は `check_ready` を何もしない関数にしている（埋め込みの有無を事前に確かめない）。工程にもしない（#3495。rag_engine を単独で使う経路だけが工程を開かずに確かめ、中止したときだけ「文書検索」の工程に理由を残す） | — | — | `rag/backend/app/rag/answer_engine.py::AnswerEngine._answer_sync` |

- rag_engine の `AdbHybridSearchUnavailable`（`ADB hybrid search を使用できません` の回答を返す経路）は rag_engine の Oracle store 用で、backend の注入した検索（`_search`）はこの例外を出さない（静的確認）。backend の主の検索の失敗がエラーになることは `rag/backend/tests/test_answer_degraded_steps.py::test_main_search_failure_is_an_error_not_zero_evidence` が確かめる。

### 並べ替え

| 能力 | (a) | (b) 使えない・失敗のとき | (c) 記録 | (d) 精密に見せるか | コード |
|---|---|---|---|---|---|
| rerank（Cohere Rerank） | 任意（設定 `RAG_RERANK_ENABLED`。モデル・compartment が未設定なら実行しない） | 原質問の rerank の失敗 → 取得の順で続ける | `DS`（`rerank`）・`ST`（工程「Rerank」の status「失敗・取得順を保持」） | 見せない: 診断の `models.rerank` は、無効・未設定のときと、回答の中で 1 回でも rerank が失敗したとき空にする（#3286）。引用には `rerank_score` が付かない | `rag/packages/rag_engine/rag_engine/generation/answering.py::_rerank_records`・`rag/backend/app/rag/answer_engine.py::AnswerEngine._models_used` |
| 追加の検索文の rerank（#2161） | 任意 | その検索文を飛ばし、原質問の関連度で補う | `DS`（`rerank_extra_query`）・`ST`（「検索文 N 本の最高値」の N） | 見せない | `rag/packages/rag_engine/rag_engine/generation/answering.py::_best_rerank_ranks` |

### 検索の拡張（`answer_engine.py`）

| 能力 | (a) | (b) 使えない・失敗のとき | (c) 記録 | (d) 精密に見せるか | コード |
|---|---|---|---|---|---|
| 実体の拡張（#1362） | 任意（検索・回答プロファイルの `entity_expansion_enabled`。既定 on） | 読めない → 足さずに続ける | `DS`（`entity_expansion`） | 見せない | `rag/backend/app/rag/answer_engine.py::AnswerEngine._entity_expansion` |
| 関係情報の拡張（#2055） | 任意（`RAG_GRAPH_EXPANSION_ENABLED`。既定 off） | 読めない → 足さずに続ける | `DS`（`graph_expansion`）・`DX`（`graph_expansion.attempts` の `status: error`） | 見せない | `rag/backend/app/rag/answer_engine.py::AnswerEngine._graph_expansion` |
| 交差参照の参照先の解決・読み込み（#1280・#1400） | 任意（`RAG_REFERENCE_EXPANSION_ENABLED`） | 解決・読み込みの失敗 → その参照を辿らない / 足さない | `DS`（`reference_resolve`・`reference_load`）・`DX`（`reference_expansion.searches`） | 見せない | `rag/backend/app/rag/answer_engine.py::AnswerEngine._reference_expansion` |
| 参照先の文書の先読み | 任意 | 失敗 → 辿るときに 1 つずつ照合する | `DS`（`reference_prefetch`） | 見せない | `rag/backend/app/rag/answer_engine.py::AnswerEngine._prefetch_reference_documents` |
| 参照先の文書の中の rerank | 任意 | 失敗 → 検索の順のまま足す | `DS`（`reference_rerank`） | 見せない | `rag/backend/app/rag/answer_engine.py::AnswerEngine._document_reference_chunks` |

### 目録・章節（rag_engine の工程と backend の読み込み）

| 能力 | (a) | (b) 使えない・失敗のとき | (c) 記録 | (d) 精密に見せるか | コード |
|---|---|---|---|---|---|
| 画面目録の読み込み（#554） | 任意（`RAG_SCREEN_LINKING_ENABLED`） | 読めない → 画面を足さずに続ける | `DS`（`screen_catalog`） | 見せない | `rag/backend/app/rag/answer_engine.py::AnswerEngine._load_screen_catalog` |
| 画面の選択（LLM） | 任意 | 失敗 → 選ばずに続ける | `DS`（`screen_link`。rag_engine から `report_degraded`）。一時的な失敗が cache に残る不具合は #3278 | 見せない | `rag/packages/rag_engine/rag_engine/retrieval/screen_catalog.py::link_screens` |
| 画面の根拠の chunk | 任意 | 読めない → その画面を足さない | `DS`（`screen_chunks`） | 見せない | `rag/backend/app/rag/answer_engine.py::AnswerEngine._screen_chunks` |
| 章節の目録（章節の拡張の起点。#1436） | 任意（`RAG_SECTION_EXPANSION_ENABLED`。無効なら目録を読まない） | 読めない → 目録が無い文書と同じに扱う | `DS`（`section_outline`）・`DX`（`section_expansion.unmapped_seeds`） | 見せない | `rag/backend/app/rag/answer_engine.py::AnswerEngine._section_outline`・`rag/packages/rag_engine/rag_engine/generation/section_expansion.py::expand_sections` |
| 章節の chunk の読み込み | 任意 | 読めない → その章節を読めなかった章節にする（`read_error`） | `DS`（`section_chunks`）・`DX`（`section_expansion` の止めた理由・読めなかった章節） | 見せない（読めなかった章節として記録に残す） | `rag/backend/app/rag/answer_engine.py::AnswerEngine._section_chunks`・`rag/packages/rag_engine/rag_engine/generation/section_expansion.py::SectionReader.groups` |
| 章節の拡張の判定（CRAG の評価器） | 任意 | 失敗 → 読んだ章節のまま回答へ進む（`grade_error` で止める） | `DX`（`section_expansion` の段の `grade_error`。型の名前だけ） | 見せない | `rag/packages/rag_engine/rag_engine/generation/section_expansion.py::expand_sections` |
| 章節の子の章節の並べ替え（rerank） | 任意 | 失敗 → 語の重なりで並べる | `DS`（`section_child_rerank`）・`DX`（`ranking: lexical`。rerank が無いときと同じ） | 見せない | `rag/packages/rag_engine/rag_engine/generation/section_expansion.py::_rank_children` |
| 長い章節の抽出（#1437） | 任意（`RAG_SECTION_EXTRACTION_ENABLED`） | 1 回分の失敗・時間切れ → その分を「回答に未反映の関連章節」の範囲にする（理由 `extraction_error` / 時間の上限） | `DX`（`section_expansion` の読めなかった章節）。分ごとの記録（`parts_unread`）は抽出の trace にだけあり、診断に写していない | 見せない | `rag/packages/rag_engine/rag_engine/generation/section_extraction.py::SectionExtractor._unread` |

### 質問の理解・拡張・根拠確認（rag_engine）

| 能力 | (a) | (b) 使えない・失敗のとき | (c) 記録 | (d) 精密に見せるか | コード |
|---|---|---|---|---|---|
| 質問の理解（規則。LLM なし） | 必須 | 例外は送出 → **エラー**（try が無い） | `SP` | 見せない | `rag/packages/rag_engine/rag_engine/retrieval/question_planning.py::plan_question` |
| 用語・同義語と回答ルール（runtime knowledge） | 任意 | 読めない → 補助情報なしで続ける | `DS`（`runtime_knowledge`）・`ST`（status「補助情報なしで続行」と詳細の「読込エラー: 例外の型の名前」。例外の文はログだけ。#3284） | 見せない | `rag/packages/rag_engine/rag_engine/knowledge/runtime_knowledge.py::load_runtime_knowledge`・`_load_failure` |
| 質問の拡張（`RAG_QUERY_STRATEGY`） | 任意 | 戦略の判定・検索文の生成の LLM の失敗 → 単純検索（拡張なし）で続ける | `DX`（`strategy` が実際に使った戦略）・`ST`（理由の文。型の名前だけ） | 見せない | `rag/packages/rag_engine/rag_engine/generation/answering.py::build_query_expansion`・`_degraded_query_expansion` |
| CRAG の補正検索 | 任意（設定・追加検索予算） | 追加検索の失敗 → 取得済みの根拠を維持して生成へ進む。初回検索の失敗は従来どおりエラー | `DS`（`crag_retrieval`）・`ST`（追加検索のエラー）・`RT`（`stop_reason=retrieval_error`・例外の型） | 取得済み資料だけを使い、未確認の不足を確定しない | `rag/packages/rag_engine/rag_engine/generation/answering.py::build_crag_answer_context` |
| CRAG の根拠確認（`RAG_ANSWER_FLOW=crag`） | 任意（設定） | 評価器の失敗 → 取得済みの根拠で生成へ進む（足りるとはしない） | `DS`（`crag_grade`）・`ST`（status「評価に失敗」）・`RT`（`CragRetrievalAttempt.grade_error`） | 見せない（生成の監査・最終の検証は通常どおり） | `rag/packages/rag_engine/rag_engine/generation/answering.py::_grade_crag_retrieval`・`rag/packages/rag_engine/rag_engine/generation/answer_models.py::CragRetrievalAttempt` |
| 要求ごとの根拠の探し直し（#1279） | 任意（`RAG_REQUEST_COVERAGE_RETRIEVAL_ENABLED`） | 再検索の失敗 → 既存の根拠で回答する | `DS`（`request_coverage_retrieval`）・`DX`（`request_coverage.re_retrieval.queries[].error`。例外の型の名前だけ。#3284） | 見せない | `rag/packages/rag_engine/rag_engine/retrieval/request_coverage.py::run_targeted_retrievals` |
| 生成後の未回答の要求の探し直し（#2730） | 任意（`RAG_REQUEST_COVERAGE_RETRIEVAL_ENABLED`。生成前と予算を共有） | 検索・章節補完の失敗 → 採用済みの回答を維持 | `DS`（`unanswered_request_retrieval`）・`DX`（`request_coverage.after_generation`、round の `request_retrieval`。例外は型だけ） | 見せない | `rag/packages/rag_engine/rag_engine/generation/answering.py::synthesize_grounded_answer` |
| 要求の分かち書き | 任意 | 失敗 → その要求を判定しない（unassessed） | なし（判定の結果が unassessed になる） | 見せない | `rag/packages/rag_engine/rag_engine/retrieval/request_coverage.py::coverage_targets` |
| 業務ガイドの手順ごとの検索（#2395） | 任意（`RAG_GUIDE_STEP_RETRIEVAL_ENABLED`） | 失敗 → 既存の根拠で回答する | `DX`（`guide_step_retrieval` の `error`。型の名前だけ） | 見せない | `rag/packages/rag_engine/rag_engine/retrieval/guide_steps.py::run_step_searches` |
| 業務ガイドの手順の充足の embedding（#2649） | 任意 | 失敗 → 語の判定（安全側）に戻る | `DX`（業務ガイドの記録の embedding の `failed`） | 見せない | `rag/packages/rag_engine/rag_engine/generation/grounded.py::GuideStepSemantics.lookup` |

### 生成・検証（rag_engine と backend）

| 能力 | (a) | (b) 使えない・失敗のとき | (c) 記録 | (d) 精密に見せるか | コード |
|---|---|---|---|---|---|
| 回答の生成（OCI Enterprise AI） | 必須 | 1 組目の生成の失敗 → **エラー**。回答の期限で切れたときは、確かめ終わった round か「関係する資料の記載だけ」を示す | `SP`・`DX`（`round_budget`） | 見せない（期限で切れた回答は時間切れの回答として示す） | `rag/packages/rag_engine/rag_engine/generation/answering.py`（`budget.cut_round`） |
| 是正（作り直し）の round | 任意 | 失敗 → 直前に採用した回答を公開し、回答全体の警告に残す（#2729） | `ST`・回答の警告（段落の印） | 見せない | `rag/packages/rag_engine/rag_engine/generation/answering.py`（`correction_failure`） |
| 生成の監査（LLM） | 必須（監査をする型のとき） | 期限で切れたとき → 監査なしの round にし、全要求を未回答として是正へ進む。他の失敗 → **エラー** | `RT`（round の `audit_cut`） | 見せない（未回答として扱う） | `rag/packages/rag_engine/rag_engine/generation/grounded.py` |
| 規則の当てはめの段（#2155） | 任意 | 失敗 → 当てはめを足さず、結論は確かめていないまま | `RT`（`application_step.error`。型の名前だけ。backend の診断に写していない） | 見せない | `rag/packages/rag_engine/rag_engine/generation/answering.py`（`application_step`） |
| 根拠の画像の添付 | 任意（回答モデルが「画像入力に対応」のときだけ。設定の項目は無い。#3444） | 回答モデルが画像を読めない・画像ファイルが無い → 添付せず、取り込み時の図の説明（根拠の本文）で答える。添付した生成の出力が壊れた → 同じモデルで画像なしで作り直す。どの場合も既定の Vision モデルへは切り替えない | `ST`（工程「回答に添付する画像」）・`DX`（`models.attached_images`）・`RT`（`image_fallback`） | 見出しに「（画像も参照）」（添付した回答だけ） | `rag/packages/rag_engine/rag_engine/generation/answer_images.py::_image_prompt_mode`・`rag/backend/app/rag/answer_engine.py::answer_model_reads_images`・`AnswerEngine._materialize_image_evidence` |
| 表の行・列の対応の検査（#2659・#2998） | 必須（決定的な照合。外部の能力を使わない） | 例外の扱いは無い（送出すれば生成の失敗）。表として読めない根拠（列の並びが崩れた本文など）には検査が働かない。表計算・CSV の 1 行の記録は、最終の検証（`rag_validate_answer`）では同じ表の記録を組み直して検査する（#2594。回答の生成の検査は根拠ごとの本文だけを読む） | MCP の根拠の `table_binding`（`checkable` / 表の根拠だが読めない `not_checkable` / 表の無い根拠 `null`。#3262）。回答の診断には無い | 見せない（「表で確かめた」とは出さない）。検査が読めない表の根拠は `not_checkable` で呼び出し元が見分ける | `rag/packages/rag_engine/rag_engine/generation/table_binding.py::unbound_table_values`・`rag/packages/rag_engine/rag_engine/generation/table_records.py::record_tables`・`rag/packages/rag_engine/rag_engine/generation/grounded.py::_table_binding_conflict` |
| 表の値の直し（#3005） | 任意 | LLM の失敗・確かめに通らない → 外したまま（安全側） | `LOG`（直せなかった理由を診断で区別しない） | 見せない | `rag/packages/rag_engine/rag_engine/generation/table_revision.py::revise_table_passage` |
| 最終の検証（資料との照合。`RAG_ANSWER_AUDIT_MODE`） | 任意（`none` で省く） | 失敗 → `status: error`・`valid: false` にし、`needs_human_review`・`confidence: low`・`outcome: conditional` にする | `DX`（`answer_validation`）・`ST`（`execution_steps` の `error`）・span の失敗 | 見せない（確認が要る回答にする） | `rag/backend/app/rag/answer_engine.py::AnswerEngine._validate_outcome` |
| 承認済み FAQ からの回答（#702） | 必須（FAQ を選んだとき） | LLM の失敗 → **エラー** | span の失敗・工程の `error` | 見せない | `rag/backend/app/rag/answer_engine.py::AnswerEngine._answer_from_approved_faq` |
| 回答の注意書きの設定 | 任意 | 読めない → 既定の注意書き | `LOG` | 見せない | `rag/backend/app/rag/answer_notice.py::current_answer_notice` |
| 回答の記録の保存 | 任意（回答は返す） | 失敗 → 応答の `storage_failures` に残す | `SP`（`ANSWER_RECORD_SAVE_STAGE` が `error`）・応答の `storage_failures` | 見せない | `rag/backend/app/rag/pipeline.py::RagPipeline._run_engine` |
| 評価の入力の組み立て | 任意 | 失敗 → 評価の入力なし（回答は返す） | `LOG` | 見せない | `rag/backend/app/rag/answer_engine.py::_evaluation_input` |

### 根拠の位置と版（表の根拠）

| 項目 | 現状 | コード |
|---|---|---|
| 行・列の位置 | `EvidenceLocator` の `row_start`・`row_end` の番号の基準は `row_basis`（`sheet`=表計算のシート / `csv`=CSV の記録の番号〔RFC 7111 の行。表頭の記録を 1 として空の行も数える。原本の物理行の範囲は `source_line_start`・`source_line_end`〕 / `word_table`=Word の `table_index` の表の行 / `parsed_table`=解析が PDF・HTML・画像の表から組み立てた格子の行で、原本の座標ではない。表頭の行も数える。#3262・#3429）。`cell_range` は表計算だけ。表計算の 1 つのデータの行と CSV の 1 記録の根拠は、セルの座標の `table_row` と `table_cells`（値のあるセルの列 `table_col` と表頭の段 `header_path`）も持つ（取込で記録した chunk だけ）。Word は 1 つの表だけを含むまとまりが表の番号と行の範囲だけを持つ（セルの座標は無い）。PDF・HTML・画像の表は、解析が格子を出し、格子と本文の行が対応したときだけ `parsed_table` の表の番号と行の範囲を持ち、1 つのデータの行の根拠は `table_cells`（解析が出したときはセルの `bbox`）も持つ | `rag/backend/app/mcp/tools.py::EvidenceLocator`・`rag/backend/app/mcp/tools.py::_table_rows`・`rag/packages/rag_parser_core/rag_parser_core/sheet_records.py::_table_locator_metadata`・`rag/packages/rag_parser_core/rag_parser_core/structured_records.py::_csv_table_metadata`・`rag/packages/rag_parser_core/rag_parser_core/parsed_table.py`・`rag/backend/app/rag/chunking_small_to_big.py::_layout_tables` |
| 版 | 根拠の版は処理の単位の `chunk_set_id`（取込み直すと変わる。文書の改訂の版ではない）。改訂した文書は別の `document_id` で、置き換えた旧版は `superseded`。文書の改訂の番号は持たない | `rag/backend/app/mcp/tools.py::RagEvidence` |
| 位置が無いときの見え方 | 位置の項目は空（`null`・空の配列）のまま。精密な位置を持つように見せる値は入れない（`table_cells` は表頭の段を記録していない古い記録から推測せず、壊れた値は空にする。`rag/backend/tests/test_mcp_api.py` が確かめる）。解析が格子を出さなかった表（本文だけの表・VLM・OCR が本文で書いた画像の表・本文から推したセル）・格子と本文の行が合わない表・続きの印の無い頁をまたいで分かれた表・形の合わない続きの表は、解析の表の行も `null`（`rag/backend/tests/test_parsed_table_locator.py` が確かめる。長い HTML の表を分けた部分と、続きの印のある頁をまたぐ表は #3547 で行を持つ）。Word の表のセルの座標は未対応 | `rag/backend/app/mcp/tools.py::_locator` |

## `degraded_steps` の処理の種類

`DS` に出る処理の種類（`kind`）の一覧。コードの `record_degraded("…")`・`report_degraded("…")` と一致させる（`rag/backend/tests/test_answer_capability_inventory.py` が確かめ、種類を足したらこの表に足すまで失敗する）。

| kind | 処理 | 記録する箇所 |
|---|---|---|
| `query_overflow` | 全文検索の超限補完に使う語の抽出（失敗は未検索として保存し、既存根拠を維持する） | `rag/packages/rag_engine/rag_engine/retrieval/query_overflow.py::search_overflow_requests` |
| `prompt_overrides` | 編集した回答のプロンプトの読み込み | `rag/backend/app/rag/answer_engine.py::AnswerEngine._run` |
| `business_names` | 業務の名前の読み込み | `rag/backend/app/rag/answer_engine.py::AnswerEngine._business_names` |
| `entity_expansion` | 実体の拡張 | `rag/backend/app/rag/answer_engine.py::AnswerEngine._entity_expansion` |
| `graph_expansion` | 関係情報の拡張 | `rag/backend/app/rag/answer_engine.py::AnswerEngine._graph_expansion` |
| `reference_resolve` | 参照先の解決 | `rag/backend/app/rag/answer_engine.py::AnswerEngine._reference_expansion` |
| `reference_load` | 参照先の chunk の読み込み | `rag/backend/app/rag/answer_engine.py::AnswerEngine._reference_expansion` |
| `reference_prefetch` | 参照先の文書の先読み | `rag/backend/app/rag/answer_engine.py::AnswerEngine._prefetch_reference_documents` |
| `reference_rerank` | 参照先の文書の中の rerank | `rag/backend/app/rag/answer_engine.py::AnswerEngine._document_reference_chunks` |
| `screen_catalog` | 画面目録の読み込み | `rag/backend/app/rag/answer_engine.py::AnswerEngine._load_screen_catalog` |
| `screen_link` | 画面の選択の LLM（rag_engine） | `rag/packages/rag_engine/rag_engine/retrieval/screen_catalog.py::link_screens` |
| `screen_chunks` | 画面の根拠の chunk | `rag/backend/app/rag/answer_engine.py::AnswerEngine._screen_chunks` |
| `section_outline` | 章節の目録 | `rag/backend/app/rag/answer_engine.py::AnswerEngine._section_outline` |
| `section_chunks` | 章節の chunk の読み込み | `rag/backend/app/rag/answer_engine.py::AnswerEngine._section_chunks` |
| `history_rewrite` | 会話の履歴での質問の書き換えの LLM（回答エンジンの前の段） | `rag/backend/app/rag/pipeline.py::_rewrite_query_with_history` |
| `field_filter` | 抽出項目の条件の読み取り（定義の読み込み・LLM。回答エンジンの前の段） | `rag/backend/app/rag/pipeline.py::_read_field_conditions`・`rag/backend/app/rag/field_filter_reader.py::read_field_conditions` |
| `chat_attachments` | 会話の添付の読み込み・解析（回答エンジンの前の段） | `rag/backend/app/rag/chat_attachments.py::conversation_toolset`・`attachment_units`・`attachment_evidence` |
| `chat_attachment_rerank` | 会話の添付の箇所の rerank（回答エンジンの前の段） | `rag/backend/app/rag/chat_attachments.py::select_attachment_units` |
| `text_search_tokenizer` | 全文検索の語の分割（Sudachi。rag_engine） | `rag/packages/rag_engine/rag_engine/retrieval/text_search_tokenizer.py::_content_terms`・`rag/packages/rag_engine/rag_engine/generation/answering.py::answer_question_result` |
| `runtime_knowledge` | 用語・同義語と回答ルールの読み込み（rag_engine） | `rag/packages/rag_engine/rag_engine/generation/answering.py::answer_question_result` |
| `rerank` | 原質問の rerank（rag_engine） | `rag/packages/rag_engine/rag_engine/generation/answering.py::_rerank_records` |
| `rerank_extra_query` | 追加の検索文の rerank（rag_engine） | `rag/packages/rag_engine/rag_engine/generation/answering.py::_best_rerank_ranks` |
| `crag_retrieval` | CRAG の追加検索。失敗時は取得済み根拠を維持 | `rag/packages/rag_engine/rag_engine/generation/answering.py::build_crag_answer_context` |
| `crag_grade` | CRAG の根拠確認の評価器（rag_engine） | `rag/packages/rag_engine/rag_engine/generation/answering.py::build_crag_answer_context` |
| `request_coverage_retrieval` | 要求ごとの根拠の探し直しの検索（rag_engine） | `rag/packages/rag_engine/rag_engine/retrieval/request_coverage.py::run_targeted_retrievals` |
| `unanswered_request_retrieval` | 生成後の未回答要求の検索・章節補完。失敗時は採用済みの回答を維持 | `rag/packages/rag_engine/rag_engine/generation/answering.py::synthesize_grounded_answer` |
| `section_child_rerank` | 章節の拡張の子の章節の並べ替え（rag_engine） | `rag/packages/rag_engine/rag_engine/generation/section_expansion.py::_rank_children` |

## 一覧から分かったこと

- **必須の能力**（失敗すると回答がエラー・拒否になる）: 有効なときの安全チェック（入力・出力。fail-closed）、質問の embedding、vector 検索、全文検索の SQL、前後の文脈・分類・1 頁目の読み込み、派生の chunk の原文の読み込み、質問の理解（規則）、回答の生成の 1 組目、生成の監査（期限の打ち切りを除く）、承認済み FAQ からの回答。主の検索の失敗は 0 件にせずエラーにする（#3259 のテスト）。
- **任意の能力**は、上の表のとおり縮退して回答を続ける。縮退した結果を「確かめた」「精密な位置」として見せる経路は、静的確認の範囲では見当たらない。`models.rerank` は rerank が失敗したとき空にした（#3286）。
- **記録が足りない縮退**: #3286 の表の処理（rerank・追加の検索文の rerank・CRAG の判定・会話の書き換え・抽出項目の条件の読み取り・会話の添付・Sudachi・章節の子の並べ替え）は `DS` に入れた。例外ではない縮退（書き換えの安全チェックの拒否・抽出項目の条件の読めない出力・呼び出し元の検索文の拒否）は `DS` に入れない。
- **診断に例外の文が残る経路**: 要求ごとの根拠の探し直しと、用語・同義語と回答ルールの読み込みは、例外の型の名前だけにした（#3284）。
- **呼び出し元が判断できない点**: MCP の応答は `DS`（種類と回数）・`ZE`（段と件数）・`GD` を出す（#3262・#3546）。表の行・列の対応の検査が根拠を読めるかは根拠の `table_binding` に出すが、回答の中で検査が段落を外した・直したかは `rag_validate_answer` の claims（`support_check`）だけにある。

## 未確認の事項

- 実 Oracle・実 LLM での失敗の振る舞い（すべて静的確認）。
- chat の入口（`rag/backend/app/api/routes/chat.py`）の質問の安全チェックの工程の span。
- 1 行の記録の根拠を複数並べたとき（表計算・CSV の 1 行 = 1 chunk）に、別の行の値の取り違えを根拠をまたいで照合すること（今の検査は 1 つの根拠の本文の中の表だけを見る。`not_checkable` で示す。同じ行・セルの照合の効果は #2594 で測る）。
- PDF・HTML・画像の表の根拠は、解析が組み立てた表の格子の行（`row_basis=parsed_table`。原本の座標ではない。#3429・#3547）までで、原本の表の行・列には結び付けない。Word の表のセルの座標（まとまりが複数の行を含むので、行の範囲だけを出す。1 行 = 1 記録にするかは #2594 の比較で決める）。

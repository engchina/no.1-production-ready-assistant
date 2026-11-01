# AIDB Memory Engineering

Oracle Developer Day 2026 の「AIDBで進化するRAG / ベクトルを超えるMemory Engineering」で示された RAG 手法を、本プロジェクトの確定スタックへ再マップした実装方針。

> #595 で、旧 standard の回答エンジン（Memory Router / Retrieval Plan、memory type 別の検索、Agent Memory Loop、Resolver / Verifier、Context Builder、Retrieval / Grounding アダプター）を削除した。回答は回答フロー（`app/rag/answer_engine.py`・`packages/rag_engine`）だけが行う。以下は、手法の各要素が今の回答フローのどこに当たるかを書く。

## 採用する runtime flow

RAG の回答は、単純な「近い chunk を prompt へ入れる」形ではなく、次の順序で動く（`app/rag/pipeline.py` → `app/rag/answer_engine.py`）。

1. 依頼を受け、安全チェックで質問を検査する（ブロックしたら検索しない）
2. Business Context Pack（誰のどの業務か＝検索範囲）を確定する
3. チャットでは、会話履歴から質問を単独で意味が通る形に書き換える（`RAG_HISTORY_REWRITE_ENABLED`）
4. 回答フロー: 質問の理解 → 質問の拡張で検索文を作る → 文書検索（Oracle AI Vector Search と Oracle Text の hybrid 検索を RRF で融合）→ 再ランク（再ランクのモデル）→ 根拠の評価と補正検索（CRAG。任意で、既定の標準 RAG では行わない）→ small-to-big（親本文と前後の child）で文脈を足す → 回答文の生成と根拠確認（監査）
5. 回答側の安全チェックを行い、監査・回答の記録・質問履歴を残す

## Business Context Pack

検索前に、誰のどの業務かを確定する。

- tenant: production では client の `X-Tenant-ID` を使わず、tenant なし（単一 tenant）で動かす（#225）。local だけは `X-Tenant-ID` を hash 化し、Oracle document/chunk/knowledge base の predicate に使う。
- user: production はログイン中の利用者（MCP ではサービストークンの利用者。#232）、local は `X-User-ID` を hash 化し、監査相関と回答の記録の持ち主に使う。
- role / agent / thread: `X-RAG-Role-ID`、`X-RAG-Agent-ID`、`X-RAG-Thread-ID`（MCP（`POST /api/mcp`。#232）では header ではなくサービストークンの `agent_id` / `run_id`）を hash 化して request context に持つ。Agent Memory の scope に使っていたが、Agent Memory は #595 で削除した。
- ACL: production はログイン中の利用者のロールの検索・回答プロファイル / ナレッジベースの対象範囲を使う（#214）。local だけは `X-RAG-Allowed-Document-Ids`、`X-RAG-Allowed-Category-Names`、`X-RAG-Allowed-Knowledge-Base-Ids` を request scope として固定する。
- dataset: `knowledge_base_ids` / `filters.knowledge_base_id` を Oracle knowledge base membership に固定する。
- source ACL: `filters.source_acl` を chunk metadata `source_acl` に固定する。
- version: `filters.document_version` を chunk metadata `document_version` に固定する。

raw tenant/user id や query 本文は audit / trace へ保存しない。

## 手法と回答フローの対応

| 手法の要素 | 回答フローでの扱い |
|---|---|
| Memory Router / Retrieval Plan | 質問の拡張（`RAG_QUERY_STRATEGY`。既定は単純検索）が検索文を、質問の理解（`inquiry_conditions`）が名指しされた文書名・ページ・業務（検索条件）と候補の加点に使う語を決める（質問の理解は検索文を作らない。#3496） |
| evidence（必須根拠） | Oracle AI Vector Search と Oracle Text の hybrid 検索（RRF。原質問を主軸にした重み付き融合）と再ランクで選んだ child chunk |
| structure（構造） | 親子階層の親本文（`parent_text`）と、根拠の child の前後の child（`RAG_NEIGHBOR_CHILD_COUNT`）。画面目録で操作画面を探す（`RAG_SCREEN_LINKING_ENABLED`）。文書構造索引の構築（文書と章・節の見出しのつながり）は KB のグラフ表示と、`RAG_GRAPH_EXPANSION_ENABLED`（既定 OFF。全体設定だけで決め、検索・回答プロファイルでは上書きしない）を ON にしたときの回答の検索の候補の追加に使う（#2055） |
| history（継続文脈） | チャットの会話履歴による質問の書き換え。Agent Memory（`rag_agent_memories`）への検索・保存は #595 で削除した |
| Resolver / Verifier | 根拠確認（CRAG の grade）と補正検索（`RAG_ANSWER_FLOW=crag` を選んだときだけ。既定は `standard_rag`）、回答文の生成後の根拠確認（監査）。根拠が足りないときは、足りない理由（`insufficient_reason`）と人手確認の要否を回答に付ける |
| Context Builder | small-to-big で親子を復元した文脈を、回答生成のプロンプト（取込・検索・回答設定 > 回答プロンプト）で LLM へ渡す |

`SearchDiagnostics` は `retrieval_strategy`（常に `hybrid`）・`retrieval_strategy_adapter`（`grounded` / `retrieval_only` / `blocked`）・`answer`（回答フローの診断。実行記録 `execution_steps` など）・安全チェックの policy / backend・`filter_keys`・`knowledge_base_count`・`config_fingerprint` などを返す。回答フローの工程ごとの記録は回答の記録（`rag_answer_records`）にも残す。

## 削除したもの（#595）

- Memory Router / Retrieval Plan（`memory_plan_id`）、memory type 別の検索、Resolver / Verifier、Context Builder（`Evidence` / `Support` / `Structure` / `History` の label）と、その診断（evidence/support/structure/history 件数、resolver rejected 件数など）。
- Agent Memory Loop（`rag_agent_memories` への Agent Memory Search と writeback）。
- Retrieval アダプター（検索モード hybrid_rrf / vector / keyword / graph_augmented / reasoning_tree_search と、gap-stop・業務適合加重・補正再検索・クエリ拡張のトグル）と Grounding アダプター（検索後処理の preset）、`GET/PATCH /api/settings/retrieval`・`/api/settings/grounding` と設定画面。補正検索は回答フローの CRAG が行う。
- 監査テーブル `rag_search_audit` の `memory_plan_id`・`agent_memory_*` などの列は、既存の監査の行を変えないため残し、既定値を書く。テーブル `rag_agent_memories`・`rag_prompt_versions`・`rag_generation_settings` は #596 で削除した（`rag_agent_memories` は更新の前に書き出す。[deployment.md の「既存環境の更新手順（#596）」](./deployment.md)）。

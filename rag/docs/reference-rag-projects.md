# 参考 RAG プロジェクト一覧

> 本ドキュメントは **Production Ready RAG が参照する外部 OSS / 研究プロジェクトのカタログ** です。
> 目的は「各プロジェクトの優れた点を抽出し、本プロジェクトに統合する」こと。
> 技術選定の正本は [AGENTS.md](../AGENTS.md)。本一覧はあくまで **着想・ベストプラクティスの調査源**であり、
> 採用する場合も本プロジェクトの確定スタック(OCI Enterprise AI / OCI Generative AI Cohere / Oracle AI Database / Vite + React Router)に
> 合わせて再実装する。外部ベクトル DB・別 LLM プロバイダをそのまま導入しないこと(逸脱時は AGENTS.md §8 に従い要確認)。

最終更新: 2026-06-28(改訂: Dify / RAGFlow / PowerRAG の文書所属モデルと複数 KB 構築設定表示の判断を §1.1 に追加)

---

## 1. 優先 POC 候補:OSS RAG プロダクト / プラットフォーム

| プロジェクト | 種別 | 適合シナリオ | 評価メモ |
|---|---|---|---|
| **Dify** | 企業向け LLM アプリ / RAG / Agent プラットフォーム | 企業ナレッジベース、ワークフロー、Agent、可視化オーケストレーション | プロダクト化が最も進んだものの一つ。RAG 処理フロー・workflow・Agent・モデル管理・可観測性が一通り揃う。Knowledge Pipeline でデータソース→抽出→処理→ナレッジ保管の企業 RAG 運用チェーンを強化中。 |
| **RAGFlow** | RAG 特化のプロダクト級エンジン | PDF・Office・複雑フォーマット文書、引用付き QA | 「専門 RAG エンジン」寄り。deep document understanding・複雑フォーマット解析・引用トレーサビリティを重視。文書品質要求が高い場面向け。 |
| **AnythingLLM** | ローカル / プライベート RAG プロダクト | 個人・チーム・私有ナレッジベース | 導入が速い。ローカル/クラウド LLM、文書取込、マルチユーザ、Agent、ベクトル DB、文書パイプライン対応。 |
| **Open WebUI** | セルフホスト AI ポータル + RAG | Ollama、ローカルモデル、OpenAI 互換 API ポータル | 純 RAG ではないが、企業/チームのセルフホスト AI 入口として強力。RAG inference engine 内蔵。統一チャット入口向け。 |
| **FastGPT** | ナレッジベース + RAG + 可視化ワークフロー | 中国語企業ナレッジ、カスタマーサポート、社内 QA | データ処理・RAG 検索・visual workflow・hybrid search・rerank・OpenAPI 内蔵。**SaaS 形態へのライセンス制限に注意**。 |
| **MaxKB** | RAG + Workflow + MCP プラットフォーム | サポート、社内ナレッジ、研究/教育 | 文書/Web/テキスト取込、チャンク分割、ベクトル化、ワークフロー、MCP ツール呼び出しに対応。 |
| **QAnything** | ナレッジベース QA システム | 中国語文書 QA、多フォーマットナレッジ | Qwen 生態系ベース。PDF・Word・PPT・Markdown・メール・画像・Web など多形式対応。 |
| **Kotaemon** | 文書チャット RAG UI | 軽量文書 QA、UI の二次開発 | "clean & customizable RAG UI" という明快な立ち位置。"chat with documents" の素早い構築向け。 |
| **PrivateGPT** | ローカル/プライベート RAG | オフライン、データを外に出さない文書 QA | documents fully offline / no data leaves machine を強調。セキュリティ・プライバシー優先。 |
| **DocsGPT** | プライベート AI / 文書 Agent プラットフォーム | 文書アシスタント、私有デプロイ | 「文書 AI プラットフォーム」寄り。ローカル/クラウド API/ローカル推論の各デプロイ対応。 |
| **R2R** | RAG バックエンド / API プラットフォーム | REST API・ハイブリッド検索・ナレッジグラフ・文書管理が必要な場合 | 開発者がバックエンド能力を組む用途に最適。multimodal ingestion・hybrid search・knowledge graphs・document management 提供。 |
| **DB-GPT** | DB / データ資産 Agent + RAG | DB 問い合わせ、BI、DWH、CSV/Excel QA | 「データ + RAG + Agent」シナリオ向け。DB・CSV・Excel・DWH・ナレッジベースを接続。 |

### 1.1 複数 KB に属する文書の構築設定表示

| プロジェクト | 文書所属 | 構築設定の扱い | 本プロジェクトでの判断 |
|---|---|---|---|
| Dify | `Document.dataset_id` による単一 Dataset | Dataset の索引方式/embedding と文書 process rule を同じ Dataset 文脈で表示 | 単一所属 UI はコピーせず、KB 別の有効設定をグループ表示する |
| RAGFlow | `Document.kb_id` による単一 KB | KB 既定と文書の `parser_id / parser_config` を単一 KB 文脈で表示 | parser/chunk の説明粒度だけを参考にする |
| PowerRAG | RAGFlow の database / data model を共有 | RAGFlow 互換に構造抽出・複数 parser・検索能力を追加 | 単一 KB 所属は継承せず、追加能力を既存 variant 層へ再マップする |

本プロジェクトは 1 文書を複数 KB が参照できるため、文書詳細を単一 KB の設定で代表させない。
同一の有効設定は 1 グループへまとめ、異なる設定は別グループとして、artifact の状態と配信状況を
`適用予定 / 構築中 / 配信中 / 更新が必要 / エラー` で区別する。

## 2. 重点トラッキング:GraphRAG / マルチモーダル / Agentic RAG

| プロジェクト | 種別 | 着目理由 |
|---|---|---|
| **Microsoft GraphRAG** | GraphRAG フレームワーク | LLM で非構造化テキストからナレッジグラフ・community summaries を構築し私有データ QA を強化。GraphRAG 方向の代表格。 |
| **LightRAG** | 軽量 GraphRAG | graph + vector の二層検索、増分更新対応。研究・二次開発向け。 |
| **RAG-Anything** (HKUDS) | マルチモーダル RAG | LightRAG ベース。text/image/table/equation/chart など多モーダル文書対応。論文・財務帳票・複雑文書向け。**重点トラッキング**。 |
| **PageIndex** (VectifyAI) | Vectorless / 推論ベース RAG | no vector DB・no chunking・file-level tree・reasoning-based retrieval。「次世代インデックス範式」として要注目。 |
| **Understand-Anything** (Egonex-AI) | コード/文書ナレッジグラフ | コードベース・文書をインタラクティブな knowledge graph 化。コード理解・アーキ QA・agent memory 向け。 |
| **Graphiti** (getzep) | リアルタイムナレッジグラフ / Agent Memory | AI Agent 向け temporal knowledge graph。事実の更新/失効、vector + 全文 + グラフ走査検索。 |
| **Cognee** (topoteretes) | Agent memory + KG + vector | vector embeddings・graph reasoning・ontology generation を統合。長期記憶・企業 Agent コンテキスト層向け。 |
| **OpenSPG / KAG** | 知識増強生成 | ナレッジグラフ増強・論理推論・専門領域 QA 寄り。高信頼・強構造化業務向け。 |
| **Neo4j GraphRAG** | Neo4j 公式 GraphRAG パッケージ | 既に Neo4j を使う企業向けの公式ルート。グラフ DB 駆動の RAG。 |
| **UR2** (Tsinghua-dhy) | RAG + 強化学習 研究 | ACL 2026 ORAL。検索と推論を RL で動的協調。プロダクトではないが研究価値高。 |
| **agentic-rag-for-dummies** | Agentic RAG 教材 / 雛形 | LangGraph・memory・human-in-the-loop・query clarification・self-correction・RAGAS・Langfuse を網羅。アーキ参考価値高。 |
| **ai-agents-for-beginners** (Microsoft) | Agent 教材 | Agent 入門。Agentic RAG の章を含む。チーム学習向け。 |
| **rag-zero-hallucinations** (FareedKhan-dev) | Zero-Hallucination RAG エンドツーエンド参照(チュートリアル + コード) | **near-zero hallucination を最優先にした end-to-end RAG**。hybrid 検索(dense + BM25 を RRF 融合)→ rerank(150→20)→ query routing / multi-hop 分解 → **文単位で引用必須の生成 or 棄権トークン** → **atomic claim 分解 + faithfulness judge** → **校正済み棄権** → **CRAG 自己修正ループ(証拠が弱ければ再検索)**。4 層のハルシネーション防止(正しい証拠を引く / 文脈のみで生成 / claim を検証 / 不確かなら棄権)が本プロジェクトの「2段ゲート + guardrail + 引用 lineage + 評価」志向と最も整合。**重点トラッキング**。確定スタックへの詳細再マップは §2.1。 |
| **knowledge-navigator** (jsonlicn) | Vectorless / ファイル単位ナビゲーション型 RAG(チュートリアル + コード) | **チャンク分割を捨て、LLM に整備済み文書の「目録」を読ませ、自分で原文ファイルを開かせる**。文書を Markdown へ治理 → ファイル単位で 1 知識単元に整理 → 各ファイル要約を**階層マージ形式の Markdown インデックス**(`index_all.md`)に集約 → 質問時にインデックスを context へロード → LLM が ReAct で読むべきファイルパスを選択 → 原文全文を読んで**ファイルパス引用付き**で回答。外部ベクトル DB 不要(全量 / 分块モード)。PageIndex と同系統の vectorless・file-level・reasoning-based retrieval。**重点トラッキング**。再マップは §2.2。 |

> **本プロジェクトへの再マップ**: GraphRAG 系(Microsoft GraphRAG / LightRAG)は、Oracle 内の軽量な関係情報(文書と章・節の見出しのつながり。`rag_graph_profile`)の構築だけを取り込み、ナレッジベースの文書構造グラフで見せる(community summary は読む経路が無かったため #621 で削除した)。Agentic 系(agentic-rag-for-dummies / LangGraph 的な query rewriting・sub-question decomposition・iterative RAG)は、回答フローの質問の理解と質問の拡張(`RAG_QUERY_STRATEGY`: 自動ルーティング / RAG-Fusion / 質問分解 / step-back / HyDE)と CRAG の補正検索が OCI Enterprise AI で担う(Agentic アダプター `rag_agentic_profile` は #595 で削除した。§4.2)。外部グラフ DB(Neo4j 等)/ 別 LLM provider はそのまま導入せず、確定スタック(Oracle AI Database + OCI Enterprise AI)へ再マップする。

### 2.1 Zero-Hallucination RAG 処理フロー(引用・検証・棄権・CRAG): FareedKhan-dev/rag-zero-hallucinations

> **near-zero hallucination を最優先に置いた end-to-end RAG チュートリアル**(語料は HotpotQA、回答不能セットに SQuAD v2 + 手書き false premise、verifier 評価に HaluBench)。
> 「完璧な生成」を狙わず、**唯一の安全な失敗モード=理由付きの棄権(refusal)** へ不確かな質問を寄せる設計思想が、本プロジェクトの 2段ゲート / guardrail / 引用 lineage 志向と深く一致する。
> ただし原実装は **LanceDB(外部ベクトル DB)・Qwen3 + vLLM(別 LLM provider)・bm25s(別 sparse 実装)・datasketch(MinHash)・LangGraph** を使うため、**そのまま導入せず**確定スタック(Oracle AI Vector Search / OCI Enterprise AI / OCI Generative AI Cohere)へ必ず再マップする(AGENTS.md §3・§8。逸脱導入は不可、必要時は理由を添えて要確認)。

**4 層ハルシネーション防止アーキテクチャ(① 正しい証拠を引く → ② 文脈のみで生成 → ③ claim を検証 → ④ 不確かなら棄権)→ 本プロジェクトへの再マップ:**

> 回答は #594 から回答フロー(`app/rag/answer_engine.py`・`packages/rag_engine`)だけで行う。旧 standard の検索・根拠確認・回答生成・高度な検索のアダプター(`rag_retrieval_strategy`・`rag_post_retrieval_pipeline`・`rag_generation_profile`・`rag_agentic_profile`)は #595 で削除したため、下の表の再マップ先は回答フローの機能で書く。

| 原実装の段階 / 手法(参考) | 原実装の技術 | 本プロジェクトの確定スタックへの再マップ |
|---|---|---|
| **取込: 正規化 + 近似重複除去 + 構造認識 chunk** | NFKC 正規化 / MinHash LSH dedup(閾値 0.9・64 perm, datasketch) / 文単位 256-token・overlap 32 の chunk + **1 文 context prefix**(各 chunk を単独で曖昧でなくする) | NFKC は既存前処理へ。**MinHash LSH は外部依存なしの決定論 dedup** として前処理に取り込む(`rag_chunking_strategy` 近傍)。context prefix は既存 chunk lineage / `chunk_strategy` metadata に「contextual prefix」派生を加える(prefix 生成は OCI Enterprise AI、決定論 fallback 付き) |
| **索引: hybrid(dense + sparse)** | LanceDB(IVF_PQ on-disk)+ **bm25s** posting、10M+ vectors へスケール | **Oracle AI Vector Search(`VECTOR(1024, FLOAT32)`)+ Oracle Text** の既存 hybrid(RRF)。索引精度は `rag_vector_index_profile`。**LanceDB / bm25s は導入しない** |
| **検索: RRF 融合 + rerank 150→20** | reciprocal rank fusion(k=60, スコア正規化なし)+ cross-encoder rerank(Qwen3-Reranker-4B) | 回答フローの hybrid 検索の RRF 融合(原質問主軸の重み付き)+ **OCI Generative AI Cohere Rerank v4 fast**(候補 N→top-k)。rerank を別 provider にせず Cohere へ |
| **路由 + 分解** | LLM で no_retrieval / single_hop / multi_hop を分類、multi-hop を 2–3 sub-question へ分解、**false-premise(誤前提)検出** | 回答フローの質問の理解と質問の拡張(自動ルーティング / 質問分解など。OCI Enterprise AI)が担う。false-premise 検出は Clarify / guardrail 的な警告として追加する余地がある |
| **引用付き生成 or 棄権** | 文ごとに inline citation 必須・passage ID 検証(無効引用は除去)、回答不能は **ABSTAIN_TOKEN(`INSUFFICIENT_EVIDENCE`)** | 回答フローの回答生成のプロンプト(取込・検索・回答設定 > 回答プロンプト)と、回答文の生成後の根拠確認(監査)。citation は既存 traceable citation lineage、棄権は根拠が足りないときの理由(`insufficient_reason`)と人手確認の要否 |
| **claim 検証(faithfulness judge)** | 回答を **atomic claim へ分解**、各 claim を引用文脈に対し faithfulness judge(Qwen3-32B)が [0,1] でスコア、**最弱 claim 基準**(全 claim が τ=0.3 超で合格)、CoVe(Chain-of-Verification)で境界回答を修復 | 回答フローの回答文の生成と根拠確認(監査ラウンド)+ `rag_guardrail_policy`(`strict` / `regulated`)の **groundedness 検証**。品質評価の `claim_support_rate`(主張ごとの監査)で測る。外部評価 SaaS / 追加 LLM-as-judge provider は増やさない |
| **校正済み棄権(calibrated abstention)** | 各シグナルを統合し risk-coverage 曲線で閾値調整、support 不足なら棄権 | guardrail の groundedness / 検索 evidence grade / claim faithfulness を統合した**決定論的棄権判定**。閾値はドメイン別に設定で調整 |
| **CRAG 自己修正 agent** | LangGraph 状態機械、evidence grade(0–1)で routing(≥0.7 生成 / <0.4 棄権 / 0.4–0.7 はクエリ精緻化 + 再検索)、hop budget 上限 3 | 回答フローの CRAG(`RAG_ANSWER_FLOW=crag`。根拠を評価し、足りなければ質問を補正して再検索)。LangGraph は導入せず本プロジェクトの検索→評価→(再検索)経路へ。旧 standard の補正再検索トグル・CRAG の grade 3 分岐・`multi_hop` は #595 で削除した |
| **評価: ハルシネーション採点 + 1000 万 vector ベンチ** | 200 問 golden set で hallucination 採点、HaluBench で verifier 評価(AUROC 0.702)、faithfulness 0.908 / context recall@20 0.97 / coverage 46% vs abstain 54%、10M index(p95 18.48ms / 38.8GB)→ 100M 外挿(p95 77.58ms / 388GB) | `rag_evaluation_suite`(#591 で `standard` / `strict` に整理)の **claim_support_rate / faithfulness / context_recall / refusal_accuracy** などで測る。golden set は本プロジェクト評価ゴールデンの作り方の手本。大規模ベンチは Oracle AI Database の索引 scaling 検証観点として参照 |

> **本プロジェクトへの示唆**: ① **棄権を一級市民にする**(回答率を下げてでも誤答を出さない安全側設計)、② **文単位の引用必須 + 引用検証**(無効引用の除去)、③ **claim 分解 + faithfulness 閾値で最弱 claim を gate**、④ **CRAG の evidence grade による「生成 / 再検索 / 棄権」三分岐**は、いずれも確定スタック内で決定論寄りに実装でき、回答フローの CRAG・監査と guardrail / evaluation アダプターの組み合わせとして整理する価値が高い(旧 standard の retrieval / grounding / generation / agentic アダプターは #595 で削除した)。**外部ベクトル DB(LanceDB)・別 LLM provider(Qwen3 / vLLM)・bm25s は導入しない。**

### 2.2 Vectorless / ファイル単位ナビゲーション型 RAG: jsonlicn/knowledge-navigator

> **「チャンクを捨て、LLM に文書を人間のように翻させる」**ことを主張する vectorless RAG チュートリアル(Python 3.10+ / ReAct エージェント / `gpt-4o` 既定・**外部ベクトル DB なし**、`summarizer.py` で索引生成 → `chat.py` でナビゲーション + 回答)。
> 文書をファイル単位の知識単元に治理し、各ファイル要約を**階層マージ形式の Markdown インデックス**(`index_all.md`、共通パス前缀を見出し化して逐行フルパスより token を 15–20% 節約)へ集約 → 質問時にインデックスを context へロード → LLM が読むべき**ファイルパス**を ReAct で選択 → 原文全文を読んで**ファイルパス引用付き**で回答する。256K context で約 70% を索引に充てれば 600–700 ファイル相当を全量ロードでき、超過時は **分块索引(~500 件/バッチ)** / **分層ドリルダウン** で拡張する。
> 本プロジェクトは既に **vectorless / reasoning retrieval の PageIndex**(§2)と **navigation tree + node 要約 + progressive disclosure**(Knowhere 取込、§5)を追跡しており、本案はその「ファイル単位・索引主導・引用可検証」志向を**ひとつの完結したエージェント検索ループ**として具体化したもの。**外部ベクトル DB を増やさない方針(AGENTS.md §3)とむしろ整合**するが、LLM=`gpt-4o`(と要約用 7B)は **OCI Enterprise AI** へ、文書治理は本プロジェクトの 2 段処理(parse→人手プレビュー確認→index)へ再マップする。

| 原実装の設計(参考) | 本プロジェクトの確定スタック / 既存機能への再マップ |
|---|---|
| **文書治理 → Markdown、ファイル=1 知識単元** | 既存の 2 段ファイル処理(parse→人手プレビュー確認→index)と `StructuredExtraction`。原本は不可篡改の溯源副本として保持(既存方針と一致) |
| **階層マージ形式の Markdown 索引(共通パス前缀を見出し化)+ 各ファイル要約** | 既存の **navigation tree + node 要約 + progressive disclosure**(`app/rag/navigation.py`。取込時に抽出結果の `navigation` へ保存する)を「ファイル単位の索引ビュー」へ拡張。要約生成は 7B ではなく **OCI Enterprise AI**。索引はキャッシュし、変更ファイルだけ増分更新 |
| **LLM が索引を読み、読むべきファイルパスを ReAct で選択** | 回答フローの「画面目録で操作画面を探す」(`RAG_SCREEN_LINKING_ENABLED`。番号付きの見出しの目録から LLM が画面を選び、その chunk を候補に加える)が近い。「索引閲覧 → ファイル選択 → 原文取得」を文書単位へ広げる設計参考(旧 `rag_agentic_profile` の検索前計画は #595 で削除した) |
| **原文全文を読んで生成(チャンクを混ぜない)** | `rag_chunking_strategy=page_level`(PageIndex 粗粒度)を**ファイル / 文書単位の粗粒度検索モード**へ拡張する設計参考。回答フローの small-to-big(親本文)を文書単位へ広げる形で設計する(旧 `rag_retrieval_strategy` は #595 で削除した) |
| **ファイルパス引用(直接開いて検証可能)** | 既存の traceable citation lineage を**ファイルパス / 文書単位の引用**として強化。回答生成のプロンプトで引用の書式を決める(旧 `rag_generation_profile` は #595 で削除した) |
| **vector-assist(任意): ベクトル + パス + 要約のみ保存、切片原文は保存しない** | **Oracle AI Vector Search を「回答源」ではなく「ナビゲーション信号」として使う**設計。ベクトルは file-path への定位ヒントに留め、回答は常に原文ファイルから。外部ベクトル DB は導入しない |

> **本プロジェクトへの示唆**: ① **索引を「人間可読・LLM 可読の Markdown 目録」として一級資産にする**(共通パス前缀の見出し化で token 節約、キャッシュ + 増分更新)、② **ベクトル検索を回答源ではなくナビゲーション信号へ降格**して引用可検証性を担保、③ **粗粒度(ファイル / 文書単位)検索モード**を chunk 主導経路と並立させる、は確定スタック内で実装でき、既存の navigation と回答フロー(画面目録・small-to-big)と PageIndex 追跡を補強する。適用境界(未治理の雑多ファイル堆、超大規模 Web 検索、リアルタイム流データ、純グラフ推理)は原文と同じく明示する。**外部ベクトル DB・別 LLM provider は導入しない。**

## 3. 自社 RAG 構築で避けて通れないフレームワーク / コンポーネント

| プロジェクト | 種別 | 価値 |
|---|---|---|
| **LlamaIndex** | RAG / document agent フレームワーク | データ接続・索引・document agent・解析・抽出・RAG に強い。複雑ナレッジベースのバックエンド向け。 |
| **LangChain / LangGraph** | Agent / LLM アプリフレームワーク | LangChain は LLM アプリ部品の合成、LangGraph は長期稼働・状態付き・復元可能・human-in-the-loop な Agent 向け。 |
| **Haystack** (deepset) | プロダクション級 LLM/RAG オーケストレーション | 工学的パイプライン寄り。retrieval/routing/memory/generation の制御要求が高いバックエンド向け。 |
| **txtai** | Embeddings / semantic search / workflow | 意味検索・LLM orchestration・workflow を網羅。text/文書/音声/画像/動画の embedding パイプライン対応。 |
| **llmware** | 企業 RAG 処理フロー | 企業文書の解析・取込・ナレッジ構築・小型モデル・ローカルデプロイに注力。 |
| **Pathway LLM App** | リアルタイムデータ RAG | live data / always-up-to-date RAG 向け。データソースが継続変化する企業検索に。 |
| **Ragbits** (deepsense-ai) | RAG building blocks | 20+ フォーマット取込、Docling/Unstructured、カスタム parser、表/画像/構造化コンテンツ、Ray 並列処理。 |
| **Cognita** (truefoundry) | 実験・デプロイ可能な RAG フレームワーク | 異なる RAG 構成のデバッグと結果観察 UI 提供。プラットフォーム/ML 工学チーム向け。 |
| **Canopy** (Pinecone) | Pinecone RAG フレームワーク | Pinecone 採用済みチーム向け。今後の更新頻度は要評価。 |

## 4. 文書解析・取込・評価:RAG の成否を分ける基盤

| プロジェクト | 種別 | 重要性 |
|---|---|---|
| **Docling** (IBM) | 文書解析 | PDF 等多形式を解析。企業 RAG ingestion パイプラインへの組込みに適す。 |
| **MinerU** (opendatalab) | PDF/Office/画像 → Markdown/JSON | 高精度文書抽出。複雑 PDF・スキャン・Office を検索可能な構造に変換。 |
| **Marker** (datalab-to) | 文書 → Markdown/JSON/chunks | PDF・画像・PPTX・DOCX・XLSX・HTML・EPUB 対応。表・数式・コードも処理。 |
| **Unstructured** | 汎用文書前処理 | 企業 RAG の定番 ingestion 部品。PDF/HTML/Word/画像/テキストの非構造化処理。 |
| **Knowhere** (ontos-ai) | 文書解析 API / RAG-ready chunks | PDF・Office・画像などを構造化 JSON / Markdown / chunks へ変換。表・数式・layout・source traceability・階層 memory / progressive disclosure を重視。商用 API / オンプレ対応の位置づけのため、採用時は本プロジェクト schema と OCI/Oracle stack へ adapter として再マップする。 |
| **No.1-PdfParser-Free** (engchina) | PDF → ページ画像 → Markdown/OCR サンプル | PyMuPDF で PDF をページ画像化し、OpenAI 互換 VLM/OCR 呼び出しで Markdown を生成する Gradio ベースの軽量サンプル。ページ画像 artifact、OCR prompt、長文応答の継続取得、画像参照付き Markdown 出力は parser adapter の設計参考になる。採用時は VLM を OCI Enterprise AI へ再マップし、PyMuPDF/AGPL の扱いを法務確認する。 |
| **Ragas** | RAG 評価 | OSS の LLM アプリ評価フレームワーク。指標・合成テストデータ・品質監視。RAG 評価の定番。 |
| **AutoRAG** (Marker-Inc-Korea) | RAG 処理フロー自動最適化 | モジュール組合せを自動評価し、より良い RAG 処理フローを探索。本プロジェクトでは外部 stack は導入せず、parser adapter readiness と file-processing golden/staging 指標を使う `parser_adapter_scorecard` として再実装する。 |
| **FlashRAG** (RUC-NLPIR) | RAG 研究再現ツールキット | RAG アルゴリズム再現・研究向け。多データセット・多アルゴリズム・多 reasoning 手法。 |
| **HaluBench / faithfulness judge**(rag-zero-hallucinations 由来) | ハルシネーション検証データ + claim 検証 | 回答不能セット(SQuAD v2 + 手書き false premise)と HaluBench で **verifier(faithfulness judge)自体を AUROC 評価**し運用閾値を選ぶ発想。本プロジェクトの `rag_evaluation_suite` へ **hallucination rate / claim faithfulness / context recall@k / coverage vs abstain** を決定論指標として追加する手本(詳細は §2.1)。外部評価 SaaS / 追加 LLM-as-judge provider は導入しない。 |

### 4.1 2026 追跡: 構造認識・マルチページ文書 chunking / 評価

| プロジェクト / 論文 | 種別 | 本プロジェクトへ取り込む観点 |
|---|---|---|
| **Graph-Aware Late Chunking / GraLC-RAG** | 構造認識検索・評価 | 通常の MRR / Recall だけでなく、文書内の structural section coverage を測る。Oracle AI Database の hybrid retrieval に section_path / content_kind / element lineage filter を重ね、SecCov 系指標を golden/staging gate に取り込む。 |
| **AutoRAGTuner** | 宣言的 RAG 自動最適化 | pipeline component を registry 化し、construction / execution / evaluation / optimization を宣言的に回す。外部 optimizer は導入せず、本プロジェクトでは parser adapter scorecard / staging promotion gate から始め、次段で chunk template / retrieval policy も file-processing 指標で自動推奨する。 |
| **Adaptive Chunking** | 指標駆動 chunking 選択 | References Completeness / Intrachunk Cohesion / Document Contextual Coherence / Block Integrity / Size Compliance のような intrinsic metrics で文書ごとに chunking 戦略を選ぶ。本プロジェクトの chunk_block_integrity / chunk_contextual_coherence / chunk_size_compliance / element_lineage_coverage を chunk template scorecard へ拡張する。 |
| **SCAR: Semantic Continuity-Aware Retrieval** | adaptive context expansion | 固定 window の token 増を避け、retrieved chunk の隣接 chunk を query relevance と構造連続性で必要時だけ展開する。parent-child chunk と chunk_group_id を使い、OCI Cohere embedding/rerank の範囲で再実装する。旧 standard の `rag_context_adaptive_expansion_enabled` で段階導入していたが #595 で削除した。今は回答フローの small-to-big(親本文と前後の child。`RAG_NEIGHBOR_CHILD_COUNT`)が同じ役割を持つ。 |
| **M3DocDep** | マルチモーダル・マルチページ dependency chunking | cross-page parent-child、figure/table-caption、boundary cue を tree として復元してから chunk を作る。StructuredExtraction の parent_id / assets / tables / bbox / section_path を使い、依存関係 chunking の評価を追加する。旧 standard の `rag_context_dependency_promotion_enabled`(rerank 後に関連 parent/child chunk を昇格)は #595 で削除した。今は回答フローの small-to-big が親子を復元する。 |
| **MultiDocFusion** | 階層・マルチモーダル chunking | vision parser → OCR → document hierarchy tree → hierarchical chunk の流れ。OCI Enterprise AI VLM の抽出結果を本プロジェクト schema に再マップし、DFS/階層 chunk のテンプレートを拡張する。 |
| **From PDF to RAG-Ready** | 文書変換フレームワーク評価 | Docling / MinerU / Marker 等の比較では、変換ツール単体より metadata enrichment と hierarchy-aware chunking が支配的。adapter feature flag の採否も downstream QA / page hit / table QA / section coverage で判定する。 |

> これらは研究・設計参考であり、実装時は必ず OCI Enterprise AI / OCI Generative AI Cohere / Oracle AI Database に再マッピングする。

### 4.2 本プロジェクトで上回るための実装差分

- parser adapter は「導入済み/未導入」の表示に留めず、`parser_adapter_scorecard` で readiness、source-kind coverage、backend-source-kind coverage、fallback rate、table QA、page hit、element lineage、section coverage、dependency recall、ingestion p95 を統合して推奨 backend を返す。外部 adapter を staging metrics で推奨するには retrieval recall / table QA / page hit / element lineage / source-kind coverage / backend-source-kind coverage / fallback rate の中核証拠が揃っている必要があり、部分的な良い指標だけでは `adapter_metric_evidence_incomplete` として local fallback を優先する。staging trend は selected/recommended backend、metrics source/applied target、backend entry ごとの score/status/rank/metric count/installed/executable/warning を保存し、推奨が local に戻った、外部 adapter が missing/disabled になった、または entry の score / metric evidence が縮小した場合も regression として止める。
- `adapter_contract_coverage` は parser routing、source/backend coverage、page coverage、preview addressability、element lineage、table/cell lineage、visual chunk metadata、quality report、warning taxonomy を合成し、外部 adapter を推奨するための総合構造契約 gate として扱う。RAGFlow / Docling / Marker / Unstructured 的な見た目の対応数ではなく、本プロジェクト schema に再マップされた evidence が揃っているかで判定する。contract trend は baseline で証明済みの source kind / backend / passed source kind / passed scenario を削れず、missing / blocking source kind・scenario・backend の新規追加も止めるため、case 数や missing 件数を保ったまま難 fixture を簡単 fixture に置き換える運用を合格にしない。
- `parser_adapter_contract_cli` は Docling / Marker / Unstructured の runtime compatibility matrix を非機密 artifact として出力する。未導入・未選択 adapter は `missing` / `disabled` / `available` / `ignored` として明示し、インストール済みで有効な adapter だけ実 fixture を `parse_with_registry` へ通して source-kind aware schema remap contract を検証する。`--manifest docs/evaluation/file-processing-golden-set.json --strict` では staging manifest の `fixture_root` と `adapter_schema_remap=true` を持つ `cases[].fixture` を case 単位で展開し、scenario / parser backend / schema count / reason code / fixture hash label を remap 証跡として残す。strict mode では selected adapter ごとに最低 1 件の `passed` schema-remap case を要求し、package が入っていても real fixture で証跡を出せない adapter は `adapter_schema_remap_evidence_missing` で promotion を止める。manifest source kind は corrupted / unsupported case を除外した後も contract の対象として保持し、可ルーティング source に正向き fixture がなければ `adapter_schema_remap_fixture_missing_for_source` で止めるため、PDF を未測定のまま HTML だけで adapter を合格にはできない。PDF/image は page lineage、image は bbox/asset lineage、HTML/email/Office は semantic/header/slide/sheet/table lineage を要求するため、単に element が 1 件返っただけでは合格にしない。fixture root、fixture file name、case id は artifact / API 出力で hash label 化し、real-world manifest の文書名を CI や UI へ漏らさない。設定画面の「運用診断」と `GET /api/settings/parser-adapters/contract` は #492 で削除し、この matrix は CLI と nightly だけが出す。CLI の `--strict` は adapter backend を `auto` 相当にし、Docling / Marker / Unstructured の feature flag を有効化した runtime snapshot で実 adapter smoke を promotion blocker にする。全 backend/source の coverage matrix では非 routing 対象 pair を `unsupported` として記録するだけだが、`--strict --backend <adapter> --source-kind <kind>` のように明示した pair は `unsupported` も blocking failure にする。nightly workflow は API base URL が未設定でもこの artifact を file-processing gate より先に出し、`run_file_processing_staging=true` かつ `require_real_world_file_processing_manifest=true` の production staging では `parser-adapters` extra と `--strict` / `--parser-adapter-contract-strict` を自動的に有効化する。手動の `parser_adapter_contract_strict=true` は staging 前の単独 smoke を同じ厳格経路で走らせるために残す。staging artifact には `adapter_contract_matrix_summary` として backend/source status matrix、backend/source/status count、missing source kinds、passed / blocking failure の `case:<hash>` evidence、blocking failure source/backend、reason/warning taxonomy を出し、「導入したはずの外部 adapter が実際には本 project schema へ戻せない」退化を CI で検知する。trend regression は backend/source/scenario の件数だけでなく `passed_case_refs` / `backend_passed_case_refs` / `blocking_failure_case_refs` を比較するため、同じ数の別 fixture へ置き換えた schema remap smoke を合格にしない。backend/source の代表 status は複数 case の最後の結果ではなく、失敗や fallback を優先した集約値にするため、同じ PDF source kind 内の一部難文書失敗を UI matrix が passed と誤表示しない。
- `table_cell_lineage_coverage` は table/chunk/citation が `cell_ref` / `formula_cell_refs` を持つ場合に `ExtractionTableCell.metadata.cell_ref` / `formula_cell_ref` まで解決できることを gate する。Docling / Marker / Unstructured が返す object / JSON 文字列 / list 形式の cell metadata も同じ ref parser で正規化し、citation 側だけに ref を詰めた状態では合格にしない。golden/staging artifact は expected refs、extraction cell で解決できた refs、search citation で覆えた refs の count を分けて残し、cell-level preview jump の到達性を CI で証明する。staging trend も `table_cell_lineage.expected_ref_count` / `resolved_ref_count` / `covered_ref_count` / `lineage_ref_count` と unresolved/uncovered count を保持し、さらに `case:<hash>` set を比較するため、coverage や count が同じでも table cell fixture や citation evidence が別 case に置き換わった場合は regression として止める。
- staging CLI は実行後 metrics を scorecard に再投入し、CI / 昇格判定が adapter を効果ベースで選べるようにする。明示 adapter が metrics で local fallback 未満なら `parser_adapter_scorecard_mismatch` で promotion を止める。
- staging promotion は `table_qa_accuracy` / `page_hit_accuracy` / `retrieval_recall` / bbox / section / dependency / parser fallback などの中核閾値が緩められていないかも検査し、`promotion_threshold_too_loose` で止める。RAGFlow / Dify 的な chunk preview だけでなく、CI 上の品質ゲートを弱体化できない運用にする。
- staging metrics は実環境でしか閉じられない retrieval / page hit / bbox / artifact reuse gate と、local parser/chunker contract で証明できる parser routing / warning taxonomy / reading order / table structure / visual chunk metadata gate を同一 payload に統合する。これにより「多形式対応を名乗る」だけでなく、形式別の構造保持・警告分類・routing 精度まで promotion artifact で証明する。
- segment checkpoint / extraction artifact cache は UI に status を出すだけでは合格にしない。pipeline-level contract と staging artifact chain で「成功済み segment の artifact_path と attempt_count は retry 後も変えない」「FAILED range だけ VLM/adapter へ再投入する」「再構成した full extraction artifact と segment artifact は Object Storage へ保存して実際に `get` できる」「artifact の document_id / segment_id / page range / schema version が checkpoint と一致しない場合は再利用しない」ことを検証し、Docling / Marker / Unstructured 的な parser 失敗時も文書全体を無駄に再処理しない。
- staging artifact は segment retry / artifact reuse の非機密 summary を `metric_evidence.segment_artifact_reuse` と各 gate evidence に出し、初回 FAILED segment 数、保留された成功 segment artifact 数、成功 segment の再処理/書き換え数、failed segment の retry 数、cache miss 数、full artifact identity metadata の有無、probe/full/segment artifact の OCI URI scheme count を CI / 手動レビューで直接確認できるようにする。promotion では readable な local/mock artifact chain では合格にせず、`oci://` で put/get/readback identity を証明できない場合は `object_storage_*_not_oci` blocker にする。trend regression では full/segment artifact の OCI/readable count と identity verified count、retry case count、retained successful segment artifact count、failed segment retry/success count が baseline から落ちることも止め、integrity error / cache miss / successful segment rewrite / successful segment reprocess / non-OCI artifact count の増加だけでなく、artifact/retry の `case:<hash>` positive evidence が消えることや bad evidence が追加されることも昇格不可にする。
- `preview_addressability_coverage` は chunk bbox の有無だけでなく、`DocumentElement` / `ExtractionTableCell` / `ExtractionAsset` の bbox が page_number と page size / page rotation / coordinate metadata でプレビュー上の座標へ解決できることを検証する。page rotation が非法、または bbox metadata と矛盾する場合は local/staging gate で失敗にし、RAGFlow 的な citation-to-preview と table cell bbox overlay を、UI 実装前の local contract でも退化検知できるようにする。trend では addressable / unaddressable / chunk bbox / missing bbox の `case:<hash>` set も比較し、bbox target count が同じでも fixture 証跡の置き換えや bad case 追加を阻断する。
- staging / promotion artifact は gate evidence と runtime evidence を allowlist 化し、OCR 原文・検索 query / answer・抽出 text / HTML / Markdown / table text を出力しない。Object Storage の extraction artifact は可恢复缓存として原文を保持できるが、CI / 昇格判定 / audit payload は count・status・hash・coverage の非機密証跡だけにする。
- staging / golden CLI artifact は `backend_source_kind_matrix` / `backend_source_kind_coverage` を出し、backend ごとの covered source kinds と case ids を非機密 evidence として残す。これにより UI や CI は「Docling/Marker/Unstructured/local のどれが PDF・Office・HTML・email・image・text を実際に処理したか」を直接確認できる。
- file-processing golden CLI は `file-processing-trend.json` を出し、parser fallback rate、表 QA、page hit、bbox / preview addressability、source/backend coverage、threshold status、promotion blocker count、result hash を非機密 trend として保存する。staging CLI も `file-processing-staging-trend.json` を出し、実 OCI / Oracle / Object Storage 経路の retrieval recall、表 QA、page hit、bbox / preview addressability、adapter contract、backend/source-kind matrix、segment artifact reuse、table cell lineage evidence、preview addressability evidence、runtime check status を同じ nightly artifact に残す。`file_processing_trend_cli` は current と baseline の trend を比較し、表 QA / page hit / bbox / preview addressability / fallback rate / ingestion p95 / blocker count / staging promotion readiness の退化を exit code で止める。比較前提として trend `kind` の一致と top-level `case_count` / `gate_count` の維持も要求し、staging trend を local trend に差し替えたり、fixture / gate 面を縮小して “同じ総合 metric” を作る抜け道を塞ぐ。baseline にある比較可能 metric が current から消えた場合も `metric_missing_from_current` として阻断し、`adapter_contract_coverage` / `table_qa_accuracy` / `page_hit_accuracy` のような厳しい証跡を少報して交集合比較から外す逃げ道も塞ぐ。さらに `runtime_check_status_counts`、`promotion_blocker_code_counts`、`threshold_status_counts`、`threshold_failures` も比較し、Docling / Marker / Unstructured が installed と表示されても、実 package + staging fixture の schema remap smoke が `ok` から `failed` / `skipped` / `pending` へ退化した場合や blocker code / failed threshold metric が増えた場合を総合 metric と独立に阻断する。Adaptive Chunking / M3DocDep 的な `dependency_context_recall`、`chunk_block_integrity`、`chunk_contextual_coherence`、`reading_order_consistency`、`structural_section_coverage`、`cross_page_table_continuity_coverage`、source/backend coverage、warning taxonomy、table/visual lineage は zero-drop 指標として扱い、小幅な低下も regression にする。Object Storage artifact chain は `passed` だけでなく roundtrip、audit redaction、cached/readable/identity-verified full artifact count、segment artifact readable/identity-verified count、retry/retained count、integrity error、cache miss、rewrite count まで比較し、backend/source-kind matrix は required/covered/missing source kind と backend/source pair、table cell lineage は expected/resolved/covered/lineage ref count と unresolved/uncovered count、preview addressability は chunk/extraction bbox target count と addressable/unaddressable count まで比較するため、RAGFlow / Docling / Marker / Unstructured 的な文件处理能力を単発 report だけでなく synthetic/local と staging/real の時系列で退化検知し、CI 上で自動阻断する。
- adapter golden gate trend は selected / recommended backend、metrics source、metrics applied target、required / manifest / covered source kinds、contract case count、contract missing source kinds、source route contract gap source kinds、blocker codes も比較する。さらに contract passed / backend passed / blocking failure の `case:<hash>` evidence を golden gate 自体にも持たせ、count や source kind が同じでも実 manifest case が別 fixture に置き換わった場合や blocking remap case が追加された場合を regression にする。bad set は件数だけでなく新規 source kind / metric / blocker code 追加も比較するため、`missing_source_kinds` や `blocker_codes` が同数で別の悪化へ入れ替わった場合も止める。これにより「Docling を推奨していた baseline が local fallback へ戻った」「staging metrics が runtime 値にすり替わった」「PDF/Office/HTML/email/image の coverage が薄くなった」「source route が schema remap contract gap を抱えたままになった」退化を、総合 coverage や `passed` のみではなく golden gate の非機密 evidence で直接止める。
- parser adapter source route / scorecard / chunk template scorecard は reason・warning・missing set の新規追加も比較する。source route は candidate / attempted / active backend、chunk template は covered source kind / scenario の削除も見るため、件数が同じでも local fallback、package missing、template scenario missing などの悪化 code へ置き換わった場合や、Marker/PDF・Office template coverage のような正向き証跡が別 backend/source/scenario に入れ替わった場合を阻断し、Docling / Marker / Unstructured の strict runtime smoke と staging manifest の schema remap evidence が同量の別 taxonomy で薄まらないようにする。
- real-world staging policy と backend/source-kind matrix でも集合内容を比較し、`executed_source_kinds` / `executed_scenarios` の削除、`missing_source_kinds` / `missing_scenarios` / `missing_executed_*` の新規追加を regression にする。これにより missing 件数や executed 件数が同じでも、Office / email / image などの難 source を別 source に差し替えて coverage を薄める運用を止める。
- parser adapter settings API は runtime route matrix と source route evidence を返し、現在の feature flag / package readiness では各 source kind がどの backend に流れるかを UI/API から確認できる。staging artifact は実行後 evidence、settings API は実行前 routing evidence として使い分ける。
- staging の `parser_adapter_source_routes` は runtime の固定候補順だけでなく、`parser_adapter_contract` の backend/source remap 成功証跡で補正する。例えば PDF の候補順で Docling が先でも、Docling/PDF の schema remap 証跡がなく Marker/PDF の証跡だけがある場合は Marker を selected route とし、`adapter_golden_gate_source_route_contract_missing` で promotion を止める。source-kind routing も「導入済みだから採用」ではなく、実 fixture から本 project schema へ戻せた backend だけを昇格候補にする。staging trend でも source kind ごとの candidate / attempted / active / selected backend と route warning を保存し、PDF/Office/HTML/email/image の route が local fallback へ戻った、active candidate が減った、contract gap warning が増えた場合を regression として止める。
- **Chunking アダプター(chunks 段階の手動戦略選択)**: parser adapter と対の概念として、`rag_chunking_strategy` で chunk 化戦略を手動選択できる。LangChain RecursiveCharacterTextSplitter / Markdown header splitter / PageIndex 粗粒度などの代表的 chunking 手法を、外部依存なし・決定論で本プロジェクトの `StructuredExtraction` へ再マップした戦略(`structure_aware` / `recursive_character` / `markdown_heading` / `page_level` / `fixed_size` / `fixed_delimiter`)を `app/rag/chunking.py` の `chunk_extraction_with_strategy` に集約する。親子分割は rag_poc から移した親子階層(small-to-big。`small_to_big`、`app/rag/chunking_small_to_big.py`)だけを持つ(LlamaIndex AutoMerging 風の `hierarchical_parent_child` は #271 で削除し、保存値は親子階層(small-to-big)として読む。`sentence_window` は撤去済みで `recursive_character` として読む)。`app/rag/chunking_strategy.py` がレジストリと runtime snapshot を返し、`GET/PATCH /api/settings/chunking` と専用設定画面で戦略・chunk_size・overlap・min_chars・分割符・親子階層の 5 項目を切り替える。全 chunk に `chunk_strategy` metadata を刻み、既存の `chunk_template` / size compliance / table continuity / `chunk_template_scorecard` と共存する。外部 chunking package(semchunk / Docling HybridChunker 等)は将来 parser adapter と同型の package-gated adapter として拡張余地を残し、確定スタック(OCI / Oracle)は変更しない。
- **Retrieval アダプター / Grounding アダプター(#595 で削除)**: 検索段階を `rag_retrieval_strategy`(`app/rag/retrieval_adapter.py`。hybrid_rrf / vector / keyword / graph_augmented / reasoning_tree_search と、gap-stop・業務適合加重・補正再検索・クエリ拡張のトグル)、検索後処理を `rag_post_retrieval_pipeline`(`app/rag/grounding_adapter.py`。dedupe / Resolver-Verifier / Context Builder と MMR・context expansion・compression の preset)で手動選択していた。回答が回答フローだけになった(#594)ため、#595 でアダプター・`GET/PATCH /api/settings/retrieval`・`…/grounding`・設定画面・診断をまとめて削除した。検索は回答フローの質問の拡張と hybrid 検索(RRF)・Rerank、補正検索は CRAG、文脈の組み立ては small-to-big が担う。
- **Generation アダプター / Guardrail アダプター(回答生成 / 安全の手動選択)**: 回答生成の `rag_generation_profile`(`app/rag/generation_adapter.py`。grounded_concise / detailed_cited / strict_extractive / structured_json / bilingual_ja_en の system prompt 変種)と `GET/PATCH /api/settings/generation`・設定画面は #595 で削除した(回答は回答フローの回答生成のプロンプトで書く)。安全は `rag_guardrail_policy`(`app/rag/guardrail_adapter.py`)で standard(既定)/ strict / lenient / regulated を選び、NeMo Guardrails / Llama Guard の概念を prompt injection・PII マスク・groundedness 閾値の厳格度として束ねる(外部安全 SaaS なし)。`GET/PATCH /api/settings/guardrail` と専用設定画面で切替し、`SearchDiagnostics` に `guardrail_policy` を残す。既定 preset は現行挙動と一致させ、外部 LLM provider / 別 DB は導入しない。
- **Vector Index アダプター(索引/検索精度の手動選択)**: 段5(索引/ベクトル検索)も同型アダプター化する。`rag_vector_index_profile`(`app/rag/vector_index_adapter.py`)で balanced(既定・`RAG_ORACLE_VECTOR_TARGET_ACCURACY` をそのまま使用)/ accurate(98)/ fast(85)を選び、Oracle AI Vector Search の検索時 target accuracy を runtime 即時に切り替える([oracle.py](../backend/app/clients/oracle.py) の vector fetch clause)。推奨 HNSW ビルドパラメータ(neighbors/efconstruction/distance)は `GET/PATCH /api/settings/vector-index` と専用設定画面に参考表示し、適用には索引再作成(`requires_reprovision`)が必要で、版管理された schema DDL artifact は自動変更しない。profile は `SearchDiagnostics.config_fingerprint` に反映する(`SearchDiagnostics.vector_index_profile` は #595 で削除した)。Embedding は Cohere v4/1024 固定で「モデル設定」画面と重複するため独立アダプター化しない。外部ベクトル DB は導入しない。
- **Evaluation アダプター(評価の基準 = 閾値の手動選択)**: 評価段も同型アダプター化する。`rag_evaluation_suite`(`app/rag/evaluation_adapter.py`)で standard(標準。既定)/ strict(厳格)を選ぶ。指標は #591 で rag_poc の最終の 7 指標と Ragas・TruLens の RAG triad・ARES の観点に照らし、検索(context_recall / mrr)・根拠(faithfulness / citation_traceability_coverage / claim_support_rate)・回答(answer_keyword_hit_rate / refusal_accuracy / requirement_coverage / answer_pass_rate)の 9 つに整理した。標準回答のあるケースだけ、1 件の回答で測れる指標を LLM の照合・主張の監査と決定的な計算で求め、同じ閾値で合否を付ける(#680)。解決順は request の明示 thresholds > request の suite > 設定 `rag_evaluation_suite`。詳細は [evaluation-observability-guardrails.md](./evaluation-observability-guardrails.md)。
- **文書構造索引の構築 / Agentic アダプター(文書構造索引の構築 / クエリ計画の手動選択)**: **文書構造索引の構築**(`rag_graph_profile`、`app/rag/graph_adapter.py`)は `off`(既定。構築しない)/ `entities`(構築する。文書と章・節の見出しのつながりを LLM を使わずに作る)の 2 つ。Microsoft GraphRAG / LightRAG の claims・community summary に当たる `full` と legacy の `RAG_GRAPH_ENABLED` は、読む経路が無かったため #621 で削除した。`GET/PATCH /api/settings/graph` と専用設定画面で切替する。検索側の routing(Retrieval の `graph_augmented`)は #595 で削除し、構築した graph は KB のグラフ表示で使う。**Agentic アダプター**(`rag_agentic_profile`、`app/rag/agentic_adapter.py`。query_rewrite / decompose / multi_hop を `OciEnterpriseAiClient.plan_query` で計画)と `GET/PATCH /api/settings/agentic`・設定画面は #595 で削除した。HyDE / query rewriting / sub-question decomposition は回答フローの質問の拡張(`RAG_QUERY_STRATEGY`)が担う。外部グラフ DB(Neo4j 等)/ 別 LLM provider は導入しない(Oracle 内 KG + OCI Enterprise AI のみ)。
- `chunk_template_scorecard` は observed chunk template と chunk_block_integrity / chunk_contextual_coherence / chunk_size_compliance / element lineage / table QA などを結び、Adaptive Chunking 型の template 健康度を返す。低スコア template は `chunk_template_scorecard_blocked` で promotion を止める。staging では manifest の `expected_chunk_template` ごとに expected / measured case count、source kind、scenario evidence も持たせ、実際の staging chunk metadata (`chunk_template` / `source_chunk_template` 等) に同 template が観測された `INDEXED` case だけを measured と数える。これにより manifest 宣言や global metric だけで `pdf_layout` の良い指標が `html_semantic` / `table_preserve_rows` の未測定、または adapter が template label を落とした退化を隠せない。corrupted / unsupported の負向 case は runtime template coverage から除外し、別 gate で失敗復旧を評価する。trend regression も recommended template だけでなく entry ごとの score、status、promotion_blocking、measured case count、covered/missing source kind、covered/missing scenario、observed chunk template、reason code count を比較し、特定 template の測定面が薄くなった退化を CI で止める。
- 通常の `/api/evaluation` の `content_kind_hit_rate` と `section_coverage` は #591 で削除した(期待値の無いケースを 1.0 と数えて平均を押し上げ、ほぼ使われていなかった)。章節・表の lineage のずれは file-processing の golden gate(`section` / `table_qa_accuracy` / `page_hit_accuracy`)で検知する。
- `GET /api/documents/{document_id}/recipes/{recipe_id}/extraction-export?format=json|markdown|html|chunks` はレシピの保存済み `StructuredExtraction` を Marker / Docling 風の JSON、Markdown、escaped HTML、非 embedding chunk view として返す。文書の詳細からは Markdown / HTML / JSON（抽出エクスポート。ダウンロード・コピー）と chunk の JSON（Chunk / Citation。ダウンロード）を持ち出せ（#561）、parser 出力の監査・CI artifact・人手レビューに使う。HTML は原本由来 markup を実行せず escaped review source に限定し、外部 vector DB や別 LLM provider は導入しない。
- HTML export は `tables[].cells` がある場合に安全な `<table>` として再構成し、`data-table-id` / row / col / bbox / formula cell metadata を保持する。旧 extraction のように cells がない表は escaped `<pre>` に fallback するため backward-compatible。
- extraction export は `assets[]` も Markdown / HTML の監査 view に出し、asset id / kind / page / bbox / alt text を trace できるようにする。HTML では asset 実体や Object Storage path を埋め込まず、escaped text と data 属性だけを返すため、Docling 的な asset metadata と RAGFlow 的な可視化を安全に両立する。
- `RetrievedChunk.metadata` / `DocumentChunkView.metadata` は scalar だけでなく recursive JSON metadata を許容し、`element_ids`、`dependency_edges`、bbox、table row group を配列/オブジェクトのまま API / UI / CI artifact へ返す。RAGFlow 的な traceable citation を、文字列 split 前提ではなく schema 上の構造化 lineage として扱う。
- scorecard は local / Docling / Marker / Unstructured を本プロジェクト schema へ再マップする前提で評価し、OCI Enterprise AI / OCI Generative AI Cohere / Oracle AI Database の確定 stack を変更しない。
- parser adapter の `auto` は単純な固定順ではなく source-aware routing にする。PDF は Docling の階層・layout と Marker の PDF block/chunks を優先候補にし、画像は Unstructured/Marker の OCR・table partition を優先し、Office/HTML は Docling/Unstructured、email は Unstructured、plain text/Markdown/CSV/JSON は local parser を優先する。Docling など外部 project が audio を扱えても、本プロジェクトは音声・動画を取り込まず、アップロードで止める(#3369)。明示 backend でも現行 adapter 実装が扱えない source は `*_adapter_source_unsupported` を残して local / Enterprise AI fallback へ戻し、「対応を掲げたが実際は parser が呼べない」半状態を避ける。staging payload の `parser_adapter_source_routes` で source kind ごとの candidate / attempted / active / selected backend を出し、CI / 昇格判定で routing の根拠を確認できるようにする。
- Unstructured adapter は対応 runtime では `include_page_breaks`、PDF/画像の `strategy=auto`、`infer_table_structure` を要求し、page break / layout / table 構造をできるだけ `StructuredExtraction` へ持ち帰る。`PageBreak` は searchable element にはせず reading-order 境界として使い、明示 page_number が欠けた後続 block へ page lineage を推定付与する。古い API には signature filtering で未対応 kwargs を渡さず、fallback を不要に増やさない。
- Docling / Marker / Unstructured など外部 adapter の block metadata に `parent_id` / `section_path` / heading level がある場合は、`DocumentElement.parent_id` と `section_path` へ first-class に再マップする。metadata がない場合も title block の reading order から section stack を復元し、citation / chunk lineage が flat text に落ちないようにする。
- Docling / Marker 風の `pages` / `groups` / `children` / nested block tree は 1 階層だけでなく再帰展開し、leaf block へ親 page_number / section_path を継承する。ただし親 page/container の bbox は精密 citation を歪めるため自動継承せず、bbox は block / table / cell / asset 自身の座標だけを採用する。
- Section / Title など「node 自身が text/id を持ち、同時に children も持つ」adapter 出力は単なる traversal container として捨てず、親 `DocumentElement` として保存する。明示 `element_id` がある場合は children の `parent_id` として継承し、chunk metadata の `dependency_edges` まで到達させることで TopoChunker / MultiDocFusion 的な document hierarchy tree を citation context に残す。
- 外部 adapter が `FigureCaption` / `TableCaption` を parent_id なしで返す場合は、reading order から直前 figure / table へ parent-child lineage を補完する。figure caption は同一 chunk の `dependency_edges` に残し、table caption は `content_kind=table` として filter / citation lineage に残すだけでなく、親 table element text、`ExtractionTable.caption`、`table_caption` chunk metadata へ回填する。adapter 由来の表でも表題・行列構造・引用先が分離しない。
- local HTML の `<table><caption>` は `ExtractionTable.caption`、table element text、`table_caption` chunk metadata に保持する。caption 前置きの長表でも `table_preserve_rows` を維持し、分割後の各 row-group chunk に caption と header を繰り返すため、表 QA / 引用 preview / row tree 評価が表題を失わない。
- local Markdown table は単なる `DocumentElement(kind=table)` に留めず、`ExtractionTable.cells` へ first-class cell structure として昇格する。通常表は `markdown-table-*`、page marker をまたぐ同一表頭は同じ continuity `table_id` を維持し、chunk metadata と row-tree 評価からセル・行・列を検証できるようにする。`Table 1:` / `表1:` のような直前 caption と escaped pipe (`\|`) も保持し、Markdown 原稿でも表題・列数・cell text を崩さない。
- 外部 adapter が table `cells` / `table_cells` として row / col / text / bbox / span / confidence / formula lineage / header role を返す場合は、caption text や flat markdown より cell structure を優先し、`ExtractionTableCell` と cell-level `metadata` へ保持する。cell id、formula cell ref、cached formula value、header scope を allowlist で remap し、表 QA、cell-level review、bbox overlay を parser 非依存で検証できるようにする。adapter 由来 citation は `table_id` が欠けても `cell_ref` / `formula_cell_ref` / row-col から StructuredExtraction の table cell を探索し、page/bbox metadata で preview overlay へ到達できることを UI と staging gate の両方で確認する。
- 外部 adapter が `text` / `rows` / `category` を持たず `cells` / `table_cells` だけを返す表でも、ingestion 入口で破棄せず `table` として推定し、cell から searchable markdown と first-class `ExtractionTable` を再構成する。Docling / Marker / Unstructured の実装差を理由に表構造を失わない。
- HTML / adapter HTML の `<td rowspan>` / `<td colspan>` は単なる文字列行列へ潰さず、`ExtractionTableCell.row_span/col_span` と span-aware `row_count` / `column_count` へ保持する。Unstructured などが `metadata.text_as_html` / `table_as_html` として返す表も cells 優先で remap し、searchable markdown は span 後の列数を反映する。表 QA / chunk_block_integrity / table cell review が merged header を検知できるようにする。
- HTML / Markdown / adapter が持つ links / references は単なる本文文字列へ潰さず、少なくとも安全な URL と表示 text を element/chunk metadata に保持する。HTML `href`、Markdown inline/reference/autolink、adapter block の `links` / `references` / `url` / `href` / `uri` は `javascript:` / `data:` / `vbscript:` を実行・保存対象から外し、traceable citation の監査情報として `link_urls` / `link_texts` / `link_count` へ remap する。
- 外部 adapter の `Formula` / `Equation` block は `text` が空でも `latex` / `formula` / `mathml` metadata から本文を復元し、`DocumentElement(content_kind=equation)` と chunk metadata の `equation_format` へ残す。RAG-Anything / Marker 的な equation 対応を、検索・citation lineage から落とさない。
- local XLSX parser は `<f>` formula cell を table value としてだけ扱わず、cell address・cached value・formula text を `ExtractionTable.metadata`、`ExtractionTableCell.metadata`、`DocumentElement(content_kind=equation, equation_format=excel_formula)` へ保存する。`office_sheet` / `table_preserve_rows` chunk でも `formula_cell_refs` / `formula_value` / parent table lineage を保持し、財務・集計シートの QA で「値」だけでなく「算出根拠」を引用できるようにする。
- 外部 adapter の bbox は parser ごとの方言をそのまま UI に漏らさない。`x/y/width/height`、`x/y/w/h`、`left/top/right/bottom`、`xmin/ymin/xmax/ymax` などを `DocumentElement.bbox` / `ExtractionTableCell.bbox` / `ExtractionAsset.bbox` の `xyxy` へ正規化し、要素 chunk では `bbox_coordinate_mode` / `bbox_unit` を chunk/citation metadata に残すことで、RAGFlow 的な citation-to-preview と table cell review を adapter 非依存にする。
- Docling / Marker などが page `size` / `dimensions` / `width` / `height` を返す場合は `ExtractionPage.width/height/rotation` へ保存し、absolute bbox を持つ element/table/cell/chunk には `page_width` / `page_height` も伝播する。flat 4-number bbox は本 schema の保存形式に合わせて `bbox_coordinate_mode=xyxy` と明示し、preview/staging が座標解釈を推測しないようにする。
- Docling / Marker / Unstructured が返す `Image` / `Picture` / `Figure` block は flat text に落とさず、`DocumentElement(content_kind=figure)` と `ExtractionAsset` の両方へ remap する。chunk metadata に `asset_id` を残し、figure citation から asset export / preview audit へ辿れるようにする。
- adapter が返す `Chart` / `Diagram` / `Graph` / `Plot` block も `other/text` に落とさず、検索上は `content_kind=figure`、asset 上は `kind=chart|diagram` として保持する。RAG-Anything 的な chart modality と Docling / Marker 的な chart understanding を、Oracle AI Database の filter / citation metadata / dashboard quality count へ接続する。
- local HTML / Markdown parser でも `<img>` と `![alt](url)` / `![alt][ref]` を first-class `ExtractionAsset(kind=image)` と `DocumentElement(content_kind=figure)` へ昇格する。安全な `src` / URL と alt/title は `link_urls` / `link_texts` / asset metadata へ保持し、`javascript:` / `data:` / `vbscript:` は citation lineage に保存しないため、adapter 未導入環境でも Docling / RAGFlow 的な figure review と安全な asset audit を維持できる。
- local HTML の `<pre><code class="language-*">` / `data-language` と外部 adapter の `Code` block `language` / `lang` / `programming_language` は `DocumentElement(content_kind=code)` と chunk metadata の `code_language` へ正規化する。Marker / Docling 的な code block 対応を、検索 filter・citation・chunk preview から落とさない。

### 4.3 2026-06 再点検: best RAG を上回るための未完了ライン

RAGFlow / Docling / Marker / Unstructured / Dify / RAG-Anything / MultiDocFusion の最新動向を見ると、単なる「対応形式数」では差別化にならない。上回るための判定軸は、取り込んだ構造が本プロジェクトの `StructuredExtraction` / chunk metadata / Oracle AI Database retrieval / UI preview / CI gate まで一貫して到達し、失敗時にも再処理量と監査漏れを抑えられるかである。

- **実 adapter smoke を staging 必須証跡にする**: `parser_adapter_contract_cli --strict` は導入済みだが、Docling / Marker / Unstructured を実 package 付き staging runner で毎回走らせ、`reason_code_counts` / `warning_code_counts` / `blocking_failure_reason_counts` を dashboard / CI artifact から直接読める状態にする。contract summary / trend は source kind と case count だけでなく、scenario set / passed scenario / missing scenario / blocking scenario、backend/source passed pair、backend/scenario passed pair、backend/source/status count、warning code count、blocking failure reason count も保持し、two-column PDF や Office table などの難 fixture を同数の簡単 fixture に置き換えた場合、特定 adapter/source 能力が別 adapter に隠れて落ちた場合、特定 adapter が難 scenario だけ失った場合、bad status 件数だけが増えた場合、passed 件数が減ってテスト面が薄くなった場合、warning / failure reason taxonomy が悪化した場合も regression として検出する。manifest source kind は `adapter_schema_remap=true` の正向き fixture 抽出後も保持し、可ルーティング source に fixture がない場合は `adapter_schema_remap_fixture_missing_for_source` で blocking failure にするため、package 未導入の readiness 表示や一部形式だけの成功では合格にしない。
- **fixture を本物の難文書へ寄せる**: scanned PDF、2 column PDF、cross-page table、日本語 DOCX/PPTX/XLSX、HTML table/link/image、email header/attachment、image OCR、corrupted file は synthetic golden set と local/staging trend artifact に固定済み。`staging_dataset_policy` で real-world case 数、source kind / scenario coverage、非機密レビュー、`staging/` fixture 隔離も検証できるようにした。さらに staging promotion では manifest 合規だけでなく、本実行の `case_results` が real-world case を実測したかを `executed_*` evidence で要求し、宣言だけの real-world gate を防ぐ。trend regression も executed real-world case 数、実行済み source kind / scenario 数、missing executed source/scenario、execution error count の退化を止めるため、adapter contract が本物 fixture から離れていく運用を baseline 比較で検知できる。`--require-real-world-policy` と workflow 既定の `require_real_world_file_processing_manifest=true` により、production staging では policy 未設定の synthetic-only manifest を preflight で止める。次は実スキャン PDF・実 Office レイアウト・実メール添付をこの policy 付き manifest に追加し、retrieval recall / table QA / page hit / preview addressability / fallback rate / ingestion p95 を同じ品質曲線へ合流させる。
- **cross-modal hierarchy を citation へ昇格する**: RAG-Anything / MultiDocFusion 型の table-caption、figure-caption、equation、chart、section tree、cross-page dependency を `dependency_edges` と `chunk_group_id` に残し、rerank 後の context expansion が親子・隣接・依存 chunk を理由付きで昇格できるようにする。
- dependency context promotion は rerank 前の retrieved pool に依存しきらず、Oracle AI Database の `rag_chunks.metadata_json` にある `element_ids` / `parent_element_ids` / `dependency_edges` から同一 document の候補 chunk を追加取得し、その後に同じ parent-child 判定で citation metadata (`context_dependency_promoted` / `context_dependency_reason` / `context_anchor_chunk_id`) を付与する。Oracle lookup は anchor の lineage token を metadata JSON 条件へ入れてから候補を取るため、長文書で caption / parent が chunk_index 近傍にない場合も、単なる近傍 candidate limit で落とさない。これにより M3DocDep / MultiDocFusion 的な caption・child block が top_k から落ちても、構造 lineage があれば回答 context へ戻せる。
- **bbox overlay は coverage ではなく実座標検証にする**: `bbox_citation_coverage` だけでなく、page size / rotation / coordinate unit / table cell bbox / asset bbox が preview canvas 上へ解決できることを Playwright と local contract の両方で検証する。
- **可恢復 pipeline を実 workload で閉じる**: segment checkpoint / Object Storage extraction artifact cache / failed segment retry は schema と API だけでなく、「成功済み segment を再 VLM しない」「FAILED range だけ再投入する」「full extraction artifact を再構成して indexing へ進む」ことを integration test と staging artifact で証明する。
- **adapter 推奨を scorecard 駆動にする**: source kind ごとの推奨 backend は固定順ではなく、format coverage、fallback rate、table cell lineage、page hit、chunk block integrity、latency、cost evidence を合成して出す。Docling / Marker / Unstructured のどれかを導入しただけで自動採用せず、local / Enterprise AI fallback より実測で良い場合だけ推奨する。
- **Dify 的な運用 UX を品質 gate と結びつける**: ingest workflow、batch partial failure、retry、adapter status、chunk preview、citation jump を UI で見せるだけでなく、同じ状態を API / audit / Prometheus / nightly artifact にも出し、UI と CI の判断がずれないようにする。

## 5. 個別ピックアップ(立ち位置の整理)

| プロジェクト | 注目度 | 立ち位置 |
|---|---|---|
| **agentic-rag-for-dummies** | 高 | Agentic RAG の学習/雛形。プロダクトではないがアーキ参考価値高。 |
| **microsoft/ai-agents-for-beginners** | 高 | Agent 教材。チーム研修向け。RAG プロダクトではない。 |
| **PageIndex** | 非常に高 | 新型 vectorless / 推論式インデックス。重点トラッキング対象。 |
| **jsonlicn/knowledge-navigator** | 高 | **vectorless / ファイル単位ナビゲーション型 RAG の参照(チュートリアル + コード)**。チャンクを捨て LLM に Markdown 目録(`index_all.md`)を読ませ原文ファイルを開かせる。PageIndex と同系統で、本プロジェクトの navigation tree + agentic 検索計画 + 引用 lineage を補強。確定スタックへの再マップは §2.2(`gpt-4o` は OCI Enterprise AI へ、ベクトルは導入時もナビゲーション信号に限定)。 |
| **Dify Knowledge Pipeline** | 非常に高 | Dify が「アプリ基盤」から「企業級 RAG データガバナンス/運用フロー」へ進化中であることを示す。 |
| **UR2** | 中〜高 | RAG + RL 研究方向。論文/実験価値高、プロダクト成熟度はまだ早期。 |
| **Understand-Anything** | 高 | コード/ナレッジのグラフ化。codebase RAG・architecture Q&A・agent memory 向け。 |
| **nashsu/llm_wiki** | 高 | ローカルデスクトップナレッジベース。文書を永続 wiki に蓄積(都度 retrieve-and-answer ではない)。 |
| **nvk/llm-wiki** | 中 | Agent 研究/ナレッジ編纂ツール。Claude/Codex/OpenCode 等の Agent プラグイン流向け。 |
| **engchina/no.1-rag** | シナリオ型 | **Oracle/OCI 方向のサンプル**。コミュニティシグナルは弱いが、本プロジェクトの OCI シナリオに参考価値あり。 |
| **engchina/no.1-semantic-doc-search** | シナリオ型 | Oracle Cloud / ADB / Compute / Terraform 構成の semantic document search サンプル。大阪リージョン向け deploy 手順や OSS ライセンス棚卸しも含み、本プロジェクトの OCI 配備・文書検索 UI/API の比較参考になる。 |
| **engchina/No.1-PdfParser-Free** | シナリオ型(取込後に**削除**) | PDF をページ画像に変換して VLM/OCR で Markdown 化する解析サンプル。「先变换、再 parse」の手法を前処理(Preprocess)ステージの `pdf_to_page_images`(PyMuPDF でラスタライズ→画像 PDF→後段の OCR / VLM)として取り込んだが、#3347 で削除した。スキャンの PDF は既定の解析エンジン Docling が OCR(RapidOCR)で読み、ページの単位で検索したいときは分割方式「ページ単位」(`page_level`)を使う。backend の PDF の描画・文字の層の読み取りも pypdfium2 に置き換え(#3343)、AGPL の PyMuPDF への依存は無くなった。 |
| **hkuds/rag-anything** | 非常に高 | マルチモーダル RAG の重点プロジェクト。長期トラッキング推奨。 |
| **FareedKhan-dev/rag-zero-hallucinations** | 非常に高 | **near-zero hallucination RAG の end-to-end 参照(チュートリアル + コード)**。引用付き生成・claim faithfulness 検証・校正済み棄権・CRAG 自己修正を一通り通す。プロダクトではないが、本プロジェクトの「ハルシネーション抑止 preset」設計の中核参考。確定スタックへの再マップは §2.1(LanceDB / Qwen3+vLLM / bm25s は導入せず Oracle AI Database / OCI Enterprise AI / OCI Cohere へ)。 |
| **oceanbase/powerrag** | シナリオ型(**取込済**) | RAGFlow ベースの OceanBase 強化版。優点を確定スタックへ再マップ済み: scalar/日付/カテゴリ pre-filter(Oracle AI Database `JSON_VALUE`/`TIMESTAMP`/`IN`)、schema 駆動 field/entity 抽出(`extraction_field_adapter`、LangExtract→OCI Enterprise AI structured output)、prompt 版管理(`prompt_versions`、custom generation profile。#595 で削除し、回答は回答生成のプロンプト(`rag_answer_prompts`)で編集する)、外部 OCR の parser adapter 候補は #22 で削除し、Docling / Unstructured に絞った。 |
| **ontos-ai/knowhere** | 高(**取込済**) | 文書解析 API / RAG-ready chunks。優点を再実装済み: 章節 navigation tree + node 要約 + progressive disclosure(`app/rag/navigation.py`。取込時に抽出結果の `navigation` へ保存する)、図表の VLM 要約は #497 で「図・画像を AI で読み取る(Vision)」(`rag_vision_enabled`、`app/rag/vision.py`)へ統合し、図の要素の本文を説明文にして検索可能な chunk に入れる。source traceability は既存 citation lineage を継続活用。外部 API 直接依存ではなく adapter として再マップ。 |

## 6. 用途別おすすめ短縮リスト

- **企業ナレッジベースのプロダクト化**: Dify / RAGFlow / AnythingLLM / Open WebUI / FastGPT / MaxKB / QAnything / R2R
- **高品質・複雑文書 RAG**: RAGFlow / Dify Knowledge Pipeline / RAG-Anything / Docling / MinerU / Marker / Unstructured / Knowhere
- **PDF OCR / Parser 実装参考**: Docling / MinerU / Marker / Unstructured / Knowhere / engchina/No.1-PdfParser-Free
- **GraphRAG / ナレッジグラフ増強**: Microsoft GraphRAG / LightRAG / RAG-Anything / KAG / Graphiti / Cognee / Neo4j GraphRAG / Understand-Anything
- **Agentic RAG**: LangGraph / LlamaIndex / agentic-rag-for-dummies / LangChain / Graphiti / Cognee
- **ハルシネーション抑止 / 引用・faithfulness 検証 / 棄権**: FareedKhan-dev/rag-zero-hallucinations / Ragas
- **評価・チューニング**: Ragas / AutoRAG / FlashRAG
- **次世代 RAG 方向の研究**: PageIndex / knowledge-navigator(vectorless・ファイル単位ナビゲーション) / UR2 / RAG-Anything / Graphiti / LightRAG / Understand-Anything
- **Oracle / OCI 参照実装**: engchina/no.1-rag / engchina/no.1-semantic-doc-search / engchina/No.1-PdfParser-Free

## 7. リスク・法務メモ

- **Flowise**: ローコード生態系は強いが、深刻な脆弱性が悪用された報道あり。公開デプロイ時は**必ず最新化し隔離**する。
- **Dify / Open WebUI / FastGPT** などソース利用可能プロジェクトは、**ライセンス/ブランド/商用制限**を企業商用前に法務確認する。
- **engchina/No.1-PdfParser-Free**: README 上では PyMuPDF の AGPL / 商用ライセンス注意が明記されている。PDF 画像化・OCR prompt・Markdown artifact 設計は参考に留め、商用採用時は PyMuPDF ライセンスを法務確認する(取り込んだ前処理 `pdf_to_page_images` は #3347 で削除し、backend の PyMuPDF も #3343 で pypdfium2 に置き換えた)。VLM/OCR 呼び出しは OpenAI 互換 chat API のまま導入せず、OCI Enterprise AI に置き換える。
- 本プロジェクトへ取り込む際は、確定スタック(OCI Enterprise AI / OCI Generative AI Cohere Embed v4・Rerank v4 fast / Oracle AI Vector Search / Vite + React Router)に**必ず再マッピング**する。外部ベクトル DB・別 LLM プロバイダの直接導入は AGENTS.md §3・§8 に反するため不可(逸脱時は理由を添えて要確認)。

## 8. リンク集

- Dify — https://github.com/langgenius/dify
- RAGFlow — https://github.com/infiniflow/ragflow
- AnythingLLM — https://github.com/Mintplex-Labs/anything-llm
- Open WebUI — https://github.com/open-webui/open-webui
- FastGPT — https://github.com/labring/FastGPT
- MaxKB — https://github.com/1panel-dev/MaxKB
- QAnything — https://github.com/netease-youdao/qanything
- Kotaemon — https://github.com/Cinnamon/kotaemon
- PrivateGPT — https://github.com/zylon-ai/private-gpt
- DocsGPT — https://github.com/arc53/docsgpt
- R2R — https://github.com/SciPhi-AI/R2R
- DB-GPT — https://github.com/eosphoros-ai/DB-GPT
- Microsoft GraphRAG — https://github.com/microsoft/graphrag
- LightRAG — https://github.com/HKUDS/LightRAG
- RAG-Anything — https://github.com/hkuds/rag-anything
- PageIndex — https://github.com/VectifyAI/PageIndex
- Understand-Anything — https://github.com/Egonex-AI/Understand-Anything
- Graphiti — https://github.com/getzep/graphiti
- Cognee — https://github.com/topoteretes/cognee
- OpenSPG / KAG — https://github.com/OpenSPG/kag
- Neo4j GraphRAG — https://github.com/neo4j/neo4j-graphrag-python
- UR2 — https://github.com/Tsinghua-dhy/UR2
- agentic-rag-for-dummies — https://github.com/GiovanniPasq/agentic-rag-for-dummies
- ai-agents-for-beginners — https://github.com/microsoft/ai-agents-for-beginners
- LlamaIndex — https://github.com/run-llama/llama_index
- LangChain — https://github.com/langchain-ai/langchain
- Haystack — https://github.com/deepset-ai/haystack
- txtai — https://github.com/neuml/txtai
- llmware — https://github.com/llmware-ai/llmware
- Pathway LLM App — https://github.com/pathwaycom/llm-app
- Ragbits — https://github.com/deepsense-ai/ragbits
- Cognita — https://github.com/truefoundry/cognita
- Canopy — https://github.com/pinecone-io/canopy
- Docling — https://github.com/docling-project/docling
- MinerU — https://github.com/opendatalab/MinerU
- Marker — https://github.com/datalab-to/marker
- Unstructured — https://github.com/Unstructured-IO/unstructured
- Knowhere — https://github.com/ontos-ai/knowhere
- Knowhere API — https://knowhereto.ai/
- Ragas — https://www.ragas.io/
- AutoRAG — https://github.com/Marker-Inc-Korea/AutoRAG
- FlashRAG — https://github.com/RUC-NLPIR/FlashRAG
- FareedKhan-dev/rag-zero-hallucinations — https://github.com/FareedKhan-dev/rag-zero-hallucinations
- jsonlicn/knowledge-navigator — https://github.com/jsonlicn/knowledge-navigator
- nashsu/llm_wiki — https://github.com/nashsu/llm_wiki
- nvk/llm-wiki — https://github.com/nvk/llm-wiki
- engchina/no.1-rag — https://github.com/engchina/no.1-rag
- engchina/no.1-semantic-doc-search — https://github.com/engchina/no.1-semantic-doc-search
- engchina/No.1-PdfParser-Free — https://github.com/engchina/No.1-PdfParser-Free
- oceanbase/powerrag — https://github.com/oceanbase/powerrag
- Flowise — https://github.com/FlowiseAI/Flowise
- Dify Knowledge Pipeline 解説 — https://langgenius.co.jp/article/post/dify-knowledge-pipeline/

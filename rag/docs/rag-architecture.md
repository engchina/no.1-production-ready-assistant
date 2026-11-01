# RAG アーキテクチャ

このリポジトリは、data ingestion、chunking、indexing、hybrid retrieval、reranking、evaluation、observability、guardrails、deployment best practices をカバーする production-ready RAG reference implementation です。Backend はモデルの API（OpenAI 互換の API。LLM・VLM・埋め込み・再ランク）と Oracle AI Database を直接使い、実行モードの切り替えは持ちません。

回答は回答フロー（`app/rag/answer_engine.py`・`packages/rag_engine`。#594）だけが行う。旧 standard の回答エンジン（検索モード・根拠確認・回答スタイル・高度な検索・Agent Memory）は #595 で削除した。Oracle Developer Day 2026 の AIDB RAG / Memory Engineering 手法と回答フローの対応は [AIDB Memory Engineering](./aidb-memory-engineering.md) を正とする。回答は `依頼 → 安全チェック → 検索範囲の確定 → 質問の理解と質問拡張 → hybrid 検索（RRF）→ Rerank → 根拠の評価と補正検索（CRAG。選んだときだけ）→ small-to-big → 回答の生成と監査 → 回答側の安全チェック` の順に扱い、単純な vector 類似 chunk 投入に戻さない。

## パイプライン

1. ドキュメントアップロード
   - API: `POST /api/documents/upload`
   - 原本は `local://...` として `PLATFORM_LOCAL_STORAGE_DIR` 配下に保存する（OCI Object Storage は #11 で削除）。
   - `RAG_MAX_UPLOAD_BYTES` と `RAG_ALLOWED_UPLOAD_CONTENT_TYPES` でサイズ・MIME type を制限する。
   - 原本 bytes から SHA-256 とサイズを計算し、`content_sha256` / `file_size_bytes` として文書行へ保存する。
   - 同一 `content_sha256` の既存文書がある場合は `duplicate_of_document_id` に最初の原本文書 ID を保存する。
   - 重複の文書は、自前の索引（active な chunk_set）が無い間（初回の取込を重複として省いたとき）だけ、ナレッジベースの範囲の検索で正本の chunk を使う。取込を進めて自前の索引を持つ重複は自分の chunk だけで検索し、正本（別のナレッジベースの同じ内容の文書）を範囲に入れない。旧版の除外は KB に属する重複の側の登録で判定し、旧版として登録した重複からは正本へ届かない（`include_superseded=true` のときは届く。#1381）。検索の SQL の KB の条件は `d.document_id IN (KB に所属する文書 UNION ALL KB に所属する重複の正本)` の形で、KB の文書の集合を 1 回だけ作って vector・全文検索の候補と hash semi join する（#2124。相関の副問い合わせを `OR` で結ぶと、全文検索の索引が返した DB 全体の命中の行ごとに副問い合わせを実行し、KB の大きさではなく DB 全体の命中数に比例して遅くなり、Oracle Text の `SCORE` も一部の行でずれた）。
   - upload レスポンスには `source_profile` を含める。`source_profile` は原本ファイル名、正規化後ファイル名、拡張子、保存 MIME type、拡張子から推定した MIME type、サイズ、SHA-256、重複元、原本 modality、推奨 parser profile、テキスト charset、品質警告を返す。
   - Dify Knowledge Pipeline / RAGFlow / R2R の「取込前にデータソース品質・処理方針・重複を明示する」ベストプラクティスは、外部 parser や別 storage を追加せず、この `source_profile` と既存の Oracle document metadata に再マップする。
   - アップロードは原本保存と KB 所属確定だけを行い、取込 job は作らない。後続処理は文書ごとに永続取込 job として明示投入する（取込開始方針の `ingestion_mode` は #306 で廃止。送っても無視する）。
   - 取込 job の一覧・取得（`GET /api/documents/ingestion-jobs` ほか）は、文書のファイル名を `document_file_name` として返す（#306）。
   - 取込 job の止め方（process isolation の子。`app/rag/ingestion_worker.py` の `run_ingestion_job_subprocess`。#2003）: 親の worker は heartbeat の間隔で job の進捗（`progress_json`。#1999）を読み、進捗を出す段（解析のページ・図・表の説明）の途中で `RAG_INGESTION_JOB_STALL_TIMEOUT_SECONDS`（既定 7200 秒）変わらなければ子を止める。進んでいる job は止めず、全体の上限 `RAG_INGESTION_JOB_SUBPROCESS_TIMEOUT_SECONDS`（既定 86400 秒。1 文書 1,000 図を 1 件 60 秒で順に読む長さ）だけを保険にする。進捗を出さない段（抽出の前のファイル準備、抽出の後の分割・埋め込み・索引）は停滞を判定せず、それぞれの呼び出しの timeout と全体の上限に従う。失敗の文言は停滞と全体の上限を分け、最後に進んだ段・件数と直す設定の名前を出す。

2. OCR・本文抽出と索引
   - API: `POST /api/documents/{document_id}/ingestion-jobs`（文書の既定レシピ）/ `POST /api/documents/{document_id}/recipes/{recipe_id}/ingestion-jobs`（レシピ単位）で取込 job を投入する。確認待ちの工程は `POST /api/documents/{document_id}/recipes/{recipe_id}/approve` で次の工程へ進める。
   - 一括の投入（#2674）: `POST /api/documents/ingestion-jobs/bulk`（`{"document_ids": [...]}`。1 回に最大 10,000 件）は、文書ごとに 受付済み（`accepted`）/ 既に処理中（`already_active`）/ スキップ（`skipped`）/ 受付不可（`rejected`）と理由を判定し、受け付けた文書のレシピ1の job（QUEUED）を永続化してから結果を返す（`app/rag/bulk_ingestion.py`・`app/clients/oracle_bulk_ingestion_store.py`）。一度も取り込んでいない文書は状態の読み込みと job の登録をまとめて行い（IN と `executemany`。500 件ごとの transaction でレシピの行を lock して待機中・実行中の job を確かめ直す）、前回の出力の初期化が要る文書は 1 件の投入と同じ処理を並行数 4 で行う。流量制限（`ingest`）は要求 1 回として数え、高コストの処理の並行数は取込 worker が制御する。同じレシピに待機中・実行中の job があれば作らないため、通信断の後の再送で二重に登録しない。画面の「条件に一致する文書をすべて選ぶ」は `GET /api/documents/ids`（一覧と同じ条件の ID）で対象を読む。
   - LLM/VLM は **モデルの API**（OpenAI 互換の Responses API）のみを使う。
   - 保存先から取得した原本 bytes は、保存済み `file_size_bytes` / `content_sha256` と照合してから OCR へ渡す。
   - アップロード時の MIME type を VLM payload へ渡し、PDF / 画像 / text の real endpoint 解析条件を維持する。
   - サイズまたは SHA-256 が一致しない場合は `ERROR` にし、409 で拒否する。
   - VLM 出力は `StructuredExtraction` で Pydantic 検証してから保存する。`raw_text` に加えて `elements`（`title` / `text` / `list` / `table` / `figure` / `header` / `footer` 等）を持ち、page number、bbox、section path、confidence、parser metadata を保存できる。
   - `elements` が欠落した旧形式の抽出結果は `raw_text` から軽量推定し、`raw_text` が欠落した構造化結果は検索可能 element から本文を合成する。
   - Docling / Marker / Unstructured / RAGFlow DeepDoc の「ページ・読み順・表・章節を要素として残す」ベストプラクティスは、外部 parser 依存を追加せず画像対応モデル（VLM）の structured output schema と軽量な raw text element 推定に再実装する。
   - モデルの API の request shape が標準 payload と異なる場合は、`PLATFORM_ENTERPRISE_AI_VLM_PAYLOAD_TEMPLATE` で JSON object template を設定する。
   - `python -m app.rag.enterprise_ai_probe` で LLM/VLM endpoint の request preview と実 response parsing を Oracle / 原本の保存先から切り離して確認できる。probe 出力には raw prompt、context、OCR 本文、回答本文を含めず、payload shape と parse summary だけを残す。
   - `GET /api/documents/{document_id}/recipes/{recipe_id}/extraction-export?format=json|markdown|html|chunks` で、レシピの保存済み `StructuredExtraction` を JSON / Markdown / escaped HTML / 非 embedding chunk view として監査できる。文書の詳細の「抽出エクスポート」タブで Markdown / HTML / JSON をダウンロード・コピーでき、chunk の JSON は「Chunk / Citation」タブからダウンロードできる（`download=true` で添付として返す。内容の確認は「本文テキスト」「構造化要素」「Chunk / Citation」タブが担う。#561）。HTML は `tables[].cells` を safe `<table>` として再構成し、row / col / bbox lineage を保持する。Marker / Docling 的な多形式出力はここで本プロジェクト schema へ再マップし、再解析や外部 LLM / vector DB 呼び出しは行わない。
   - `UPLOADED` / `ERROR` を取込対象にし、`INGESTING` は二重実行防止で 409 にする。
   - `INDEXED` は force なしなら既存結果を返す。`force=true` は `INDEXED` の再取込に使える。
   - **方針(2 段階処理): parse → 人がプレビュー確認 → index。** parse/抽出の完了後はいったん `REVIEW`(プレビュー確認待ち)で停止し、`DocumentPreviewWorkspace` で抽出結果を人手で確認・承認(必要なら帳票項目を修正)してから後段の chunk/embed/index を実行する。抽出 artifact は再利用し、`INDEX` job は再 OCR せず保存済み `StructuredExtraction` から chunk / embedding / Oracle index を作る。
   - `INDEXED` は chunk、embedding、Oracle 保存、chunk_set、KB binding、extraction artifact が揃った文書だけに付与する。KB binding まで公開できない場合は `ERROR` とし、RAG 検索対象にしない。

3. チャンク分割
   - 実装: `backend/app/rag/chunking.py`
   - 取込では `chunk_extraction()` を使い、`StructuredExtraction.elements` を優先して `structure_v1` chunk を作る。`chunk_text()` は旧 raw text fallback と単体テスト用に残す。
   - Unstructured の `by_title` 風に章節境界を跨がず、RAGFlow / DeepDoc 風に表は他の本文と混ぜず独立 chunk にする。図・画像説明と図注は `content_kind=figure` として同一 chunk にまとめ、リストは連続性を保ち、通常本文だけ同一章節内で overlap を使う。
   - `header` / `footer` は繰り返しノイズとして主索引から除外する。表が長すぎる場合は行境界優先で分割する。
   - `RAG_CHUNK_SIZE` / `RAG_CHUNK_OVERLAP` で制御する。重複 chunk でも元章節・ページ・要素 metadata は維持する。
   - chunk metadata には `chunk_profile`、`chunk_group_id`、`chunk_group_kind`、`chunk_part_index`、`chunk_part_count`、`section_title`、`section_path`、`section_level`、`content_kind`、`page_start`、`page_end`、`element_kinds`、`element_ids`、`text_sha256`、`text_chars` を保存し、複雑文書 RAG で必要になる引用トレーサビリティと parent/child lineage を軽量に実現する。
   - `RAG_CHUNK_OVERLAP >= RAG_CHUNK_SIZE` は設定検証で拒否する。
   - 大きな文書でも chunk 総数では拒否せず、生成された全 chunk を embedding / indexing 対象にする。

4. 埋め込み
   - 実装: `backend/app/clients/genai.py`
   - 本番は埋め込みモデル（モデルの API の OpenAI 互換の `/embeddings`。既定 `text-embedding-v4`）で 1024 次元（`EMBEDDING_DIMENSIONS`。#46）を使う。
   - query embedding は `SEARCH_QUERY`、文書 chunk embedding は `SEARCH_DOCUMENT` の input type を使う。
   - 埋め込みの返却件数と 1024 次元幅を検証し、不一致なら Oracle へ渡す前に fail fast する。

5. 索引
   - 実装: `backend/app/clients/oracle.py`
   - 本番は Oracle AI Vector Search。ベクトル列は `VECTOR(1024, FLOAT32)`。
   - スキーマ成果物は HNSW 索引(`COSINE`、目標精度 `95`、neighbors `32`、efconstruction `500`)を作成する。
   - ベクトル検索は `FETCH APPROX ... WITH TARGET ACCURACY` を使い、問い合わせ側の精度は `RAG_ORACLE_VECTOR_TARGET_ACCURACY` で調整する。
   - 検索精度は **Vector Index アダプター(`rag_vector_index_profile`)** で手動選択する。`app/rag/vector_index_adapter.py` が profile を解決し、`balanced`(`RAG_ORACLE_VECTOR_TARGET_ACCURACY` をそのまま使用)/ `accurate`(98、既定。#272)/ `fast`(85)で検索時 target accuracy を runtime 即時に切り替える([oracle.py](backend/app/clients/oracle.py) の vector fetch clause へ反映)。推奨 HNSW ビルドパラメータ(neighbors/efconstruction/distance)は `GET/PATCH /api/settings/vector-index` と専用設定画面に参考表示し、適用には索引再作成(`requires_reprovision`)が必要。版管理された schema DDL artifact は自動変更しない。profile は `SearchDiagnostics.config_fingerprint` に反映する（`SearchDiagnostics.vector_index_profile` は #595 で削除した）。
   - python-oracledb の共有 pool を遅延初期化し、document/chunk の永続化、集計、状態更新を Oracle table に対して実行する。
   - chunk 保存と vector search の入口でも embedding 幅を再検証する。
   - 検索対象の chunk は `INDEXED` の文書に限定する。
   - `OracleClient.count_document_chunks()` で document 単位の索引済み chunk 数を確認できる。
   - 文書が `INGESTING` / `ERROR` へ移る場合は、その文書の既存 chunk/index 行と古い抽出結果を削除して古い根拠や OCR 結果を残さない。
   - 外部ベクトル DB は使わない。

6. ハイブリッド検索
   - API: `POST /api/search`（回答）。検索は回答フローの中で行う（`AnswerEngine._search`）。`generate_answer=false` は根拠を集め終えるまで（質問の理解・拡張・検索・rerank・回答フロー（CRAG を含む）・章節の拡張・要求ごとの根拠の確認）をチャットと同じ 1 つの流れ・工程・LLM の呼び出しで行い、回答の生成の手前で止める（RAG 検索の画面の既定。画面の「LLM で回答を生成する」をオンにすると `true`。#649・#3506）。処理の経過の全体は「質問の理解から根拠の検索まで」（段 `evidence`）、根拠を集める工程は「根拠の検索」（回答を作るときは「根拠の検索と回答の作成」）。MCP の `rag_retrieve_evidence` は同じ流れの主の文書の検索の後で止める（根拠の補いは Agent がする）。
   - Oracle AI Vector Search と Oracle Text（keyword）を Reciprocal Rank Fusion で統合する（`OracleClient.hybrid_search`）。RRF 定数は `RAG_RRF_K` で調整する。HyDE の仮説文書など、質問の拡張が vector だけで引く検索文は vector 検索にする。
   - 質問の拡張（`RAG_QUERY_STRATEGY`。既定は単純検索（`simple_retrieval`）。ほかに `auto_routing` / `rag_fusion` / `query_decomposition` / `step_back_prompting` / `hyde`）が作った複数の検索文は、原質問を主軸にした重み付き RRF で融合する（派生の検索文は合計で原質問 1 本分の重みに抑える）。融合の上位（候補の上限）の外から、文書ごとの上位を rerank の候補に足す（候補の多様化。`RAG_CANDIDATE_DIVERSITY_*`。#2228）。質問の理解（`inquiry_conditions`）が名指しした文書名・ページ・業務（大分類）は検索条件と profile / business_match のチャネルに加える（`docs/rag-engine.md`）。質問の理解は検索文を作らない（読み取った語は検索条件・候補の加点・回答に渡す条件にだけ使う。検索文を足すのは質問の拡張・用語・同義語・業務 profile の言い換え・CRAG の書き換え・呼び出し元の検索文。#3496）。
   - `SearchRequest` の `mode`・`strategy`・`rerank_top_n`・`generation_profile` は #595 で削除した。旧クライアントが送っても 422 にせず読み捨てる。
   - `filters` は `document_id`、`file_name`、`category_name`、`status` に加え、chunk metadata の `content_kind`、`section_title`、`section_path`、`source_acl`、`document_version` に対応し、retrieval 前に適用する。
   - `content_kind` は `text` / `list` / `table` / `figure` の完全一致、`section_title` / `section_path` は部分一致で使い、複雑文書の章節、表、図・画像説明だけに検索候補を絞れるようにする。RAG 検索の画面からは #649 で入力欄を外した（API の条件としては残す）。
   - 抽出項目の値の条件（`filters.extraction_fields`。#549）は、利用者が手で指定するほか、`RAG_AUTO_FIELD_FILTER_ENABLED` が有効な検索・回答プロファイルでは質問から LLM で読み取って足す（Metadata Filter Extraction。#652。読み取った条件で 0 件なら外して検索し直す。詳細は `docs/rag-engine.md`）。
   - `source_acl` と `document_version` は Oracle chunk metadata に対する完全一致 filter として使い、AIDB RAG の Business Context Pack で tenant / ACL / dataset / version を検索前に固定する。
   - keyword score は重複を除いた query token coverage として 0.0-1.0 に正規化する。
   - keyword（Oracle Text）の検索語の分割は 1 つだけで、設定では選ばない（#588。`rag_engine` の `retrieval/text_search_tokenizer.py`）。Sudachi の長い単位（C）と短い単位（A）を主の語にし、文字種の区切り（漢字・カタカナ・英数字の連続）・漢字の複合語の先頭 2 字と末尾 2 字・送り仮名を除いた形（「取り消し」→「取消」）を補う語として足す。検索・回答プロファイルのドメインキーワードは 1 語として優先する。語は `ACCUM` で結び、重みはキーワード 2・主の語 1・補う語と 1 字の語 0.5（多くの語に当たる chunk ほど上にし、補った語だけの一致は軽くする）。語は最大 24 語、query は最大 3,800 文字。Sudachi の辞書が使えない環境では、自動で文字種の区切りだけにする（文字種の連続を主の語にする）。索引の側の lexer（`RAG_TEXT_WORLD_LEXER` = `WORLD_LEXER`）は変えない。回答の検索とフィードバックの検索は同じ分割を使う。
   - 索引する文字列（`rag_chunks.search_text`・`rag_feedback_details.search_text`）は、保存の時点で質問の語と同じ文字の形（NFKC と波線・ダッシュの同一視。`normalize_text_search_index_text`）にする（#1336）。`WORLD_LEXER` は全角 / 半角の違い（`Ｅ１０２３` と `E1023`、半角のカタカナ）は同一視するが、互換文字（`①` `Ⅴ` `㈱` `㌔` `℃`）は同一視しない。質問の側は NFKC でこれらを `1` `V` `(株)` `キロ` `°C` にするため、索引の側も同じ形にしないと `手順①` などが当たらない（実 ADB で確認。`tests/test_text_search_tokenizer_oracle.py`）。改行と大文字小文字は残す（語の区切りと大文字小文字の無視は lexer が行う）。表示の本文（`chunk_text`）・親の本文・引用の原文・embedding の入力は正規化しない。
   - vector / keyword / hybrid の同点は document id、chunk index、chunk id で安定順にし、評価の再現性を保つ。
   - citation metadata には章節 metadata に加えて `retrieval_mode`、vector/keyword の rank/score、`rrf_k`、RRF score を含め、hybrid 召回の由来を query 本文なしで追跡できるようにする。

7. 構造化・関係検索境界
   - RAG repo では SQL 生成 endpoint を公開しない。SQL 専用の自然言語問い合わせは 同じ monorepo の `nl2sql/` の責務とする。
   - 集計・関係・横断要約のような query も、回答フローの hybrid retrieval の範囲で扱う（GraphRAG-lite を回答の検索で使う経路は #595 で削除した。構築と KB のグラフ表示は残す）。
   - GraphRAG/navigation/metadata layer が未 materialize の場合は diagnostics と KB 詳細で `planned_only` / `needs_reingest` を表示し、構築済みと見せない。

8. リランク
   - 本番は再ランクのモデル（モデル設定の既定の再ランクモデル）。回答フローは融合した検索候補を rerank で並べ替える（`RAG_RERANK_ENABLED`。既定は有効）。
   - 再ランクの返却 index は候補範囲内・重複なし、返却件数は `top_n` 以内、score は finite number であることを検証し、不正な rerank 結果は fail fast する。
   - 根拠の child と同じ `chunk_group_id` の兄弟 chunk（上限 `RAG_CONTEXT_GROUP_MAX_CHUNKS`）と親本文（`parent_text`）で親子を復元し（small-to-big）、根拠の child の前後の child（`RAG_NEIGHBOR_CHILD_COUNT`、既定 3）を文脈に足す。
   - 上位の候補の本文が参照する節（「第3章を参照」など。取込で解決して chunk の metadata に保存）の chunk を、上限付き（`RAG_REFERENCE_EXPANSION_MAX_CHUNKS`、既定 4）で参照元の直後に rerank の候補として足す（#1280。[rag-engine.md の設定一覧](./rag-engine.md#設定一覧)）。文書名だけで別の文書を指す文（「時間帯は定期保守計画で確かめます」）は、検索範囲の文書のタイトル・文書名で文書を決め、その文書の質問に関連の高い chunk（文書の中の検索と rerank）を足す（#1400）。参照先が下位の候補・前後の文脈にあるときも参照元の直後へ移す。参照の書き方の語（手がかりの語・章や条の単位・漢数字・記号など）は言語資源（取込・検索・回答設定 >「言語資源」。[language-resources.md](./language-resources.md)。#2181）。
   - 実体・別名索引: 文書中の対象（実体）を名称・ID・別名で識別し、表の行や本文の定義と原文の chunk を関連づける。構築は全体の既定（OFF）を文書レシピで上書きできる。検索方法の全体設定「実体・別名による検索拡張」（既定 OFF）を有効にすると、質問と上位の検索候補を起点に、同じ対象の定義（名寄せ）と属性で結び付く対象の根拠を 1 段だけ追加する。追加数の上限も全体設定で決め、検索・回答プロファイルでは上書きしない。Oracle の `rag_entities` / `rag_entity_aliases` / `rag_entity_chunks` を使い、外部のグラフ DB や LLM の抽出は使わない（[rag-engine.md の設定一覧](./rag-engine.md#設定一覧)）。
   - 文書構造索引: 文書と章・節・表・図の見出し、および原文の chunk の関連を記録する。検索方法の全体設定「文書構造による検索拡張」（既定 OFF）を有効にすると、上位候補と同じ狭い構造の根拠と、質問の語が見出しに一致する根拠を候補に加える。実体・別名による検索拡張の後に実行し、追加済みの chunk は除外する。両者は併用でき、どちらも LLM の呼び出しを増やさない。構築した索引はナレッジベースの「文書構造グラフ」で確認できる。
   - 旧 standard の検索後処理（本文 hash の重複除去・MMR の多様化・近傍 / 依存 chunk の追加・圧縮と、`RAG_CONTEXT_WINDOW_CHARS` などの設定）は #595 で削除した。

9. AIDB Memory Engineering
   - 検索 request ごとに Business Context Pack（検索範囲）を作る。tenant/user/role は raw 値を保存せず hash だけを持ち、document/category/knowledge base scope、`source_acl`、`document_version` を検索条件に固定する。
   - 回答の生成方式の既定は標準 RAG（`RAG_ANSWER_FLOW=standard_rag`。補正検索をしない）。CRAG（`RAG_ANSWER_FLOW=crag`）は明示して選んだときだけ使い、検索結果の根拠を AI で評価し、足りなければ質問を補正して再検索する（評価の LLM の呼び出しと時間が増える。`rag_engine` の `answer_flow` を省いた呼び出しも標準 RAG。#3509）。回答の生成方式は全体設定だけで決め、検索・回答プロファイルでは上書きしない。回答文の生成後にも根拠との整合を確かめ（監査）、根拠が足りないときは理由（`insufficient_reason`）と人手確認の要否を付ける。
   - 文書構造索引の構築は `rag_graph_profile`(`app/rag/graph_adapter.py`)で選ぶ。`off`(既定。構築しない)/ `entities`(構築する)の 2 つで、`entities` は文書全体と章・節の見出し(表・図を含む)を「含む」でつないだ entity / relationship を LLM を使わずに作る(`app/rag/graph_index.py`)。claims / community summary まで作る `full` と legacy の `RAG_GRAPH_ENABLED` は、読む経路が無かったため #621 で削除した(表 `rag_graph_claims`・`rag_graph_community_summaries` は migration で削除)。ingest の graph gate は `resolve_graph_adapter(...).enabled`。構築判定・文書の実効設定の表示・文書レシピの上書きの正本は `rag_graph_profile` 1 つ。graph の行の `knowledge_base_id` は取込時の所属のスナップショットなので、KB のグラフ表示は今もその KB に所属している文書の行だけを返す。回答の検索は、`rag_graph_expansion_enabled`（既定 OFF。検索方法の画面で選ぶ。全体設定だけで決め、検索・回答プロファイルでは上書きしない）を ON にしたときだけ graph を読む（#2055。`app/rag/graph_expansion.py` と `app/clients/oracle_graph_store.py`）。検索の上位 5 件の chunk が対応する章節・表 / 図の entity のうち chunk が 2〜8 の最も狭いものの他の chunk（近い順）と、質問に見出しの語（番号を除いた 4 文字以上）が含まれる entity の chunk（章節の先頭から 3 件・見出し 4 つまで）を、検索と同じ見え方の条件の中で 1 回の検索に `rag_graph_expansion_max_chunks`（既定 6）件まで候補に足し、rerank で並べる。すでに候補・前後の文脈・参照先・実体の拡張にある chunk は足さない。広い章節は回答の生成の段の章節の拡張（#1436）に任せる。足した chunk は回答の診断の `graph_expansion`（使った entity と足した数）に残し、回答の詳細の実行記録に出す。LLM は呼ばない。設定 API `GET/PATCH /api/settings/graph` と専用設定画面で切替する。変更は次回以降の取込に適用され、既存文書への反映には再取込が必要。外部グラフ DB は導入しない。
   - Memory Router / Retrieval Plan・Resolver / Verifier・Context Builder・Agent Memory（`rag_agent_memories` への検索と writeback）は #595 で削除し、テーブルは #596 で削除した。監査の列は既存の監査の行を変えないため残す。

10. 回答生成
   - LLM は **モデルの API**。回答フローが、small-to-big で復元した根拠の文脈を回答生成のプロンプト（取込・検索・回答設定 > 回答プロンプト。`rag_answer_prompts`）で渡して回答を生成する。追加の LLM provider は導入しない。
   - チャットでは、会話履歴から質問を単独で意味が通る形に書き換えてから回答する（`RAG_HISTORY_REWRITE_ENABLED`）。書き換えた質問も安全チェックを通す。
   - モデルの API の request shape が標準 payload と異なる場合は、`PLATFORM_ENTERPRISE_AI_LLM_PAYLOAD_TEMPLATE` で JSON object template を設定する。
   - LLM 契約は `python -m app.rag.enterprise_ai_probe --surface llm` で個別に検証できる。回答本文は probe artifact に保存せず、parse 成功と文字数だけを確認する。
   - 生成後に回答側の安全チェックで secret leakage をブロックし、回答と根拠の文脈の token / n-gram 重なりが少ない場合は `low_groundedness` warning を返す。
   - `/api/search`・`/api/search/stream`・チャットの送信は、回答を LLM で生成するため `RAG_ANSWER_TIMEOUT_SECONDS`（既定 600 秒。#1425）で pipeline 実行時間を制限する（#375。品質評価の 1 ケースも同じ上限で打ち切る。#383）。通常検索は timeout 時に 504 を返す。SSE は stream 開始後に timeout した場合、HTTP status は維持して `error` event（`message` に時間切れになった工程と再試行の案内、`stage` に工程名）を返し、どちらも `rag_search_audit.error_stage=timeout` を残す。
   - 進捗の stage は `history_rewrite`（会話履歴による質問の書き換え）・`answer`（回答フロー）と、その中の入れ子の工程 `answer_step:<工程名>`（「質問の理解」「文書検索（1回目）」など）、検索だけの経路の `retrieval` を通知する（#375 / #593）。この知らせは時間切れの工程・MCP の進み具合・ナレッジベースの検索テストの状態の 1 行に使い、画面の処理の経過は工程の span（下の処理の経過）で出す（#3226）。
   - stage は `rag_search_stage_duration_seconds` で stage 別 latency を記録する。回答フローの工程ごとの時間は `diagnostics.answer.execution_steps` に残る（負荷試験 CLI `search_load_cli` はこれで工程の p95 を集計する）。
   - レスポンスには `trace_id`、`citations`、`guardrail_warnings`、`diagnostics`、`elapsed_ms` を含める。MCP の `rag_search` は `citations` を根拠の `evidence[]`（場所・版・回答で引用したか〔`used_in_answer`〕と使われ方〔`answer_use`。該当を確認 `confirmed` / 参考 `reference`。#2040〕・切り詰めの有無）に写し、本文の続きは `rag_read_source` で読む（#1219。契約は `platform/contracts/mcp/rag-tools.json`）。検索・回答プロファイルのルールの回答前の確認はチャットと同じ判定で、聞くときは検索・回答をせずに `needs_clarification` を返す（`rule:<id>` の `conditions` で答え、`skip_clarification` で飛ばす。#2117。[rag-engine.md](./rag-engine.md) の「回答前の確認」）。
   - 呼び出し元（業務 Agent）が検索文を作る根拠の取得（#2651）: `rag_retrieve_evidence` に `planning=caller` を渡すと、`query` と `queries`（最大 5 本）だけを検索文にし、質問に応じた推論の LLM / VLM（検索戦略の判定・質問拡張・条の目録の選択・画面目録の連携・抽出項目の条件の読み取り・CRAG・回答の生成と監査・根拠の画像の添付）を設定に関わらず呼ばない（`RagPipeline` / `AnswerEngine` の `caller_queries`、`rag_engine` の `answer_question_result(caller_queries=...)`）。規則による質問の理解・用語の別名の検索文・hybrid search・rerank・親子と前後の文脈・交差参照と実体の拡張は通常の検索と同じに行う。呼び出し元の検索文は質問と同じ安全チェックを通し、RAG の言い換えの逸脱を外す照合（`filter_queries`）には掛けない。検索・回答プロファイルのルールの回答前の確認は `rag_search` と同じ判定を元の質問（`original_question`）に当て、要れば検索せずに `clarifications` を返す。対象と認可は通常の検索と同じに RAG が強制し、渡した対象を広げない。質問履歴には残さない。既定の `planning=rag` は今までどおり（RAG が言い換えを足す）。根拠には検索の順位（`retrieval_rank`）と本文の由来（`origin`。取込が作った派生の要約・抽出値は `derived`。判定は `is_derived_content`。派生の chunk は検索の後に指す原文の chunk に置き換える〔#2641。`app/rag/derived_evidence.py`〕のでこの経路でも通常は `source` で、置き換えの外から来た派生を原文として引用させないための印）が付く。回答を作らない取得（`planning` に関わらず）は、検索・回答プロファイルの用語・同義語と回答ルール（runtime knowledge）のうち元の質問（無ければ `query`）に当たる有効な登録（承認済み・見直し待ち。無効・下書きは除く）を、根拠とは別の `profile_knowledge` で返す（#2701。ルールの ID・本文・出典・状態・更新時刻、用語の別名・説明、登録の版〔`revision`〕。照合は回答の生成と同じ語の一致で LLM を呼ばない。件数は回答の生成の prompt と同じ上限、字数は `PROFILE_KNOWLEDGE_MAX_CHARS` で切り、省いた数を `rules_omitted`・`terms_omitted` に示す。読めなければ `load_failed`）。対象は検索に使った検索・回答プロファイルだけで、ログには数と版だけを残す（`rag_mcp_retrieve_evidence`）。
   - MCP の根拠（`rag_search` と `rag_retrieve_evidence` の `evidence[]`）は、`evidence_limit` で切る前に、回答に使った根拠（`used_in_answer`。回答に使った順）→ 検索で当たった chunk（`role=retrieved_anchor`。関連度＝rerank の後の順の `evidence_retrieval_rank`）→ 前後の文脈（`neighbor_context` / `same_page_context` / `parent_context` など。`citations` の順）に並べ直す（#1348。`app/mcp/tools.py` の `mcp_evidence_order`）。`citations` は文書ごとのかたまりの中を文書の順に並べるため、そのまま切ると上位の文書の前置き・前の章で枠が埋まり、当たった chunk が落ちる。画面の検索結果・回答の文脈の並びは変えない。`evidence_limit` を超えるときは、上限の内に次の枠をこの順に確保する（上限の内の当たった chunk はその位置のまま、確保した根拠は当たった chunk の後・前後の文脈の前に置き、押し出した当たりは上限の後ろに順位の順で残す）: (1) 検索で当たった上位 5 件の chunk の章の参照の先（当たった chunk が参照する節・当たった chunk の節を参照する文・回答の検索が参照先として足した chunk・当たった chunk が文書名で参照する文書の、回答の検索が参照先として足した chunk のうち参照先 1 つにつき最も関連の高い 1 件（「時間帯は定期保守計画で確かめます」→ 定期保守計画の共通の保守枠。#1400。関連度の順位が上限の外の参照先も含む。当たった chunk 自身が起点の文書名の参照も、参照先の 2 件目以降は確保しない））と、すぐ前・すぐ後の chunk を、上限の 3 割まで（#1390。`LINKED_CONTEXT_LIMIT_SHARE`）。(2) 実体の 1 段の拡張の根拠（`role=entity_expansion`）のうち rerank の関連度が回答の文脈と同じ下限以上のものと、その根拠と同じ親の前後の文脈を、上限の 3 割まで（#1362。`ENTITY_EXPANSION_LIMIT_SHARE`）。関連度の低い拡張の根拠は確保せず、関連度の順位のまま。切った件数は `evidence_omitted`。既定の `evidence_limit` は `rag_search` が 12、回答を作らない `rag_retrieve_evidence` が検索する件数（`top_k` の既定）と同じ 50（#3512）で、`evidence_limit` を省略して `top_k` を広げたときは `top_k` 件（上限 50）まで返し、検索で当たった chunk を既定で全部返す（#1365。#1335 の再評価で、台帳の行が当たった chunk の 13〜20 位にあり 12 で切れていた）。
   - 根拠の場所（`locator`）には、根拠の先頭の要素の定位子 `element_locator`（`doc:{document_id}/ext:{解析の結果の ID}/page:{頁}/el:{要素の ID}`。`app/rag/element_locator.py`）を入れる（#1330）。解析の結果の ID（`rag_chunk_sets.extraction_recipe_id`）は、文書分割の設定だけを変えて chunk を作り直しても変わらないため、`rag_read_source` に `locator` を渡すと、同じ要素を含む今の chunk を読める（chunk の ID は作り直すたびに変わる）。解析をやり直した後の古い定位子は `source_stale`。要素の ID を持たない分割（文字数・区切り文字・Markdown の見出し）の根拠は `null`。
   - 表の根拠のセルの座標（#3262）: 表計算（Excel の前処理 `excel_to_jsonl` の行の記録）の 1 つのデータの行の根拠は、`locator.table_row`（シートの元の行番号）と `locator.table_cells`（値のあるセルごとの列の記号 `table_col` と、列の表頭の段 `header_path`。複数段の表頭は上の段から）を持ち、セルを (`table_row`, `table_col`) で示せる。前処理が列の表頭の段（`SheetColumn.header_path`。`pr_backend_core.spreadsheet`）を記録し、要素化（`rag_parser_core.sheet_records`）が記録の要素の metadata（`table_row`・`table_cells_json`）にし、どの分割の方式（構造認識・親子階層・固定分割）でも 1 行の記録の chunk に引き継ぐ。値は取込のときに決まり、migration はしない。この版より前に取り込んだ文書の chunk は、取込み直すまで `table_row` が `null`・`table_cells` が空（前処理をやり直さずに分割だけを作り直したときは、前処理の結果に表頭の段が無いので `table_row` だけを持つ。表頭を推測しない）。手順・帳票・前書き・複数行の記録は `null` / 空。行の番号の基準は `locator.row_basis`（`sheet`=シートの行 / `csv`=CSV の記録の番号 / `word_table`=Word の `locator.table_index` の表の行 / `parsed_table`=解析が PDF・HTML・画像の表から組み立てた格子の行。行の位置が分からない根拠は `null`）で、`row_start`・`row_end`・`table_row` はこの基準の番号。CSV の記録の番号は RFC 7111 の行の数え方（先頭の記録を 1 とし、表頭の記録・空の行もそれぞれ 1 記録として数える。表計算のソフトで CSV を開いたときの行で、Excel の `sheet` の行と同じく表頭の行も数える）で、前処理 `csv_to_jsonl` v3 が `location.record` に記録する。原本のテキストの物理行の範囲は `locator.source_line_start`・`source_line_end`（`row_basis=csv` だけ）で、引用符の中の改行を含む記録は複数の物理行になり、その後ろの記録は記録の番号と物理行がずれる。CSV の記録は（引用符の中の改行を含む記録も）Excel と同じく `table_row`・`table_cells`（列の位置を表計算と同じ記号で。表頭は前処理が `document.header` に残した原本の表頭のセルの文字で、空・重複を補った列名ではない）を持つ。記録の番号を記録する前の CSV の chunk は、物理行の範囲だけを返し、物理行を記録の番号として返さない。Word は 1 つの表（か大きい表を分けた部分）だけを含むまとまりが、本文の表の番号と元の表の行の範囲（表頭を含めて 1 から数え、分けた部分は先頭に繰り返した表頭を数えない）を持ち、セルの座標は持たない（まとまりが複数の行を含む）。1 シートを複数の表に分けた Excel の記録は `table_index` に表の番号を持つ。
   - 解析の表の格子の行（`row_basis=parsed_table`。#3429）: PDF・HTML・画像の表には原本に行・列の番号が無いので、解析が組み立てた表の格子の行・列を、原本の座標ではない基準として出す（Azure Document Intelligence の `rowIndex` / `columnIndex` と `boundingRegions`、Docling の `TableCell` の `start_row_offset_idx` と `bbox` と同じく、解析の結果として扱う。解析の版・設定を変えて取込み直すと番号が変わることがある。#3340 では「原本の座標ではない」として出さなかったが、基準を明示すれば呼び出し元がどの行を根拠にしたかを確かめられ、原本の行（`sheet`・`csv`）と取り違えないので、利用者の決定〔2026-10-26〕で出す）。番号は格子の 1 始まりで、表頭の行（解析が表頭と印を付けたセル〔HTML の `th`・Docling の `column_header`〕だけでできた先頭の行）も数える（表頭が 2 段の表の最初のデータの行は 3）。根拠の行の範囲（`row_start`・`row_end`）はその chunk が行として含む格子の行で、表を分けた部分の先頭に繰り返した表頭（親子階層の行のまとまりの先頭の「列見出し」）は数えない。`table_index` は文書の中の表の順（格子の無い表も数える）。1 つのデータの行だけの根拠は `table_row` と `table_cells`（列の位置を表計算と同じ記号で。結合したセルは左上の行・列に置き、上の行から結合で続くセルは続きの行に入れない。`header_path` は格子の表頭の段。解析がセルの領域を出したときは `bbox`〔`locator.bbox` と同じ頁・座標。Docling の 1 頁の表だけ〕）を持つ。格子は、解析が格子を出したと印を付けた表（`ExtractionTable.metadata.parsed_table_grid`。HTML の解析・Docling・解析の adapter が渡したセル〔Unstructured の `text_as_html` など〕）の表のセルから作り、要素の本文の行（HTML の `tr`・Markdown の表の行）と格子の行の本文が順に一致するときだけ対応させる（`rag_parser_core.parsed_table`）。親子階層の Docling の表（`layout_records`）は、分割と同じ読み方（`rag_engine.parsing.table_structure.parse_html_table_structure`）で表の HTML から格子を作る（`app.rag.chunking_small_to_big._layout_tables`）。構造認識の分割は、`chunk_size` を超える HTML の表を外側の表の `tr` の境目で分け、各部分を表題（`caption`）と表頭の行（`thead` の行か、セルがすべて `th` の先頭の行）を繰り返した `<table>` にする（#3547。`rag_parser_core.parsed_table.html_table_layout`。Markdown の表と同じく、部分の先頭に繰り返した表頭は行として数えない）。頁をまたいで続く表（親子階層。取込の文書全体の整理が `raw.continues_record_id` で前の表の続きと印を付けた表）は、元の表と 1 つの表（同じ `table_index`）として、続きの表の行を前の頁の行の後ろに続けて数える（#3547。Azure Document Intelligence の頁をまたぐ表と同じく表全体の格子の行。続きの表の先頭の表頭〔繰り返した表頭・取込が前の表から付けた表頭 `table_header_carried`〕は数えない）。次の表は `null` / 空のまま（座標を作らない）: 解析が格子を出さなかった表（本文だけの表・VLM・OCR が本文で書いた画像の表・本文から推したセル〔`table_source=adapter_text`〕）、格子と本文の行が合わない表、構造認識の分割で頁をまたいで別の要素に分かれた表（続きの印が無い）、親子階層で元の表と列の数・表頭が合わない続きの表（その表から後ろ）、Vision などで書き直した表（`table_enhanced_html`）、1 つの表として `tr` の境目を決められない HTML（閉じていない `tr`・表が 2 つ以上）を文字数で切った部分。この版より前に取り込んだ文書は、取込み直す（解析し直す）まで `null`。親子階層の Docling の表は分割を作り直すだけでも行の範囲が入る（セルの領域は解析し直したときだけ）。DDL・migration は無い（chunk の metadata の JSON の key だけ）。製品横断の評価の場所の照合（`pr_evaluation_scoring.location_matches`）は、行の条件を同じ行の番号の基準の中だけで照合し、評価セットの行（原本の行）を `parsed_table` の行に一致させない。評価セットの必要な根拠は任意の `row_basis`（`sheet` / `csv` / `word_table` / `parsed_table`。`row_start` と一緒にだけ）でその基準の行を書ける（#3547。RAG の評価は根拠の場所と同じ行〔`app.mcp.tools.evidence_row_location`。CSV は記録の番号〕で照合する）。根拠の `table_binding` は、表の行・列の対応の検査（`rag_engine.generation.table_binding`）がその根拠の本文を表として読めるか（`checkable` / 表の根拠だが読めない `not_checkable` / 表の無い根拠 `null`）を示す。根拠が結び付く版は処理の単位の `chunk_set_id`（取込み直すと変わる）と文書（`document_id`。改訂した文書は別の文書で、置き換えた旧版は `superseded`）で、文書の改訂の番号は持たない。Agent の観測（`agent_get_run_observation`）は同じ名前で写す。
   - 文書を順に読む MCP のツール（#1332・#1438）: `rag_outline` は文書の章節の構成（文書の詳細の章節ナビゲーションの章節。`section_id`・階層 `level`・親 `parent_section_id`・頁の範囲・chunk の数・文字数。文書全体は `root_section_id`。`section_id` でその章節の下だけ。`format=compact` は章節を 1 行ずつ字下げした目録 `lines` で、見出しの語の無い章節〔条の番号だけなど〕は本文の書き出し `lead_chars` 字〔既定 60〕を添え、40000 字まで。条の多い文書から根拠の条を選び、`rag_read_section` の `citation` で読むため。#2252）を、`rag_read_document` は頁（`page`）・章節の始まり（`section_id`）・根拠の定位子（`locator`）・続き（`cursor`）の位置から本文を chunk の順に `max_chars`（既定 8000・上限 20000）まで返す（章節の終わりで止まらない）。`rag_read_section` は章節（`section_id`、または根拠の `chunk_id` が属する章節。`up` で何段上の親の章節を読むか）の本文を章節の始まりから終わりまで読み、終わりまで読んだら `section_complete=true`、続きは `next_cursor`（章節と chunk_set に縛る）。読んだ章節の頁の範囲・親の id と子の章節を返す。`rag_search` / `rag_retrieve_evidence` の根拠には、属する章節（`section`: `section_id`・名前・階層・頁・文字数・chunk の数・親の id と名前と文字数）を付ける（章節と chunk の対応は [rag-engine.md](./rag-engine.md) の「章節と chunk の対応」）。頁が変わる所に `--- p.N ---` の行を入れ、返した本文に含まれる chunk（`chunk_id`・定位子・本文の中の位置と、根拠の `locator` と同じ場所〔章節の列・頁、表計算は `sheet_name`・`row_start`・`row_end`・`cell_range`。#1471〕）を添え、続きは `next_cursor`（最後まで読んだら `null`）。見え方の条件は検索と同じ。複数レシピの文書は、有効な chunk_set のうちレシピの番号が最も小さいものを読む（同じ本文を別の分け方で持つだけのため）。`cursor` は chunk_set に縛り、作り直された後は `source_stale`。多段の質問は RAG の中で多段に検索せず、Agent がこれらのツールで読み進める（#595 の方針。評価は `evaluation/multi-hop/`、#1335）。
   - `POST /api/search/stream` は SSE で `stage`、`metadata`、`delta`、`citations`、`done` を返す。`stage` event は工程ごとの `started` / `success` / `error` と低機密 attributes を表す。回答 token は完全生成、PII マスク、groundedness、回答検査の後にだけ `delta` 分割する。

## Agent からの呼び出し（MCP）

Agent（Production Control Plane）は RAG を `POST /api/mcp`（MCP の Streamable HTTP、JSON 応答。#232）で呼ぶ。製品同士はコードで依存しない。

- 入口と認証: `Authorization: Bearer <サービストークン>`（`aud=rag`、`sub`=Run の利用者の `user_uuid`、署名鍵は共通 `.env` の `PLATFORM_SERVICE_TOKEN_SECRET`）。Cookie / CSRF は使わない。route manifest では `/mcp` を「認証済みなら通す」とし、権限はツールごとに判定する。
- 利用者: token の利用者の現在のロール・権限・検索・回答プロファイル / ナレッジベースの対象範囲を画面と同じ判定で使う。回答履歴・rate limit も同じ利用者。token の `agent_id` / `run_id` は hash して監査 context の agent / thread に入れる。
- 業務 Agent のデータの範囲（#1379）: token に任意の claim `profile_ids`（業務 Agent の定義の、使える検索・回答プロファイルの ID）があれば、すべてのツールを「利用者の権限 ∩ 範囲」で判定する（`app/mcp/profile_scope.py`。claim の無い token は今までどおり）。
  - ツールは、監査 context の検索・回答プロファイルを「利用者の範囲 ∩ claim」、ナレッジベースを「利用者の範囲 ∩ 範囲のプロファイルの参照先」に絞った中で実行する。一覧（`rag_list_search_answer_profiles`）は範囲で絞られ、文書を読むツール（`rag_read_source` / `rag_outline` / `rag_read_document` / `rag_read_section`）と `rag_validate_answer` の根拠の読み直しは、検索と同じ見え方の条件（文書の KB の所属）なので範囲の外の文書を「見つからない」（`source_not_found`）にする。範囲のプロファイルを読めないときは 503（範囲なしとして通さない）。
  - `rag_search` / `rag_retrieve_evidence` は `search_answer_profile_id` が要る（`knowledge_base_ids` だけの検索は 403）。範囲の外の `search_answer_profile_id`（`rag_lookup_guides`・`rag_validate_answer` の `guide` も）と、指定したプロファイルの参照先の外の `knowledge_base_ids` は 403（`structuredContent.error_code` が `PROFILE_SCOPE_FORBIDDEN`）。claim にあっても利用者が使えないプロファイルは今までどおり 404。
- ツール: `rag_list_search_answer_profiles`（検索・回答プロファイル一覧と同じ権限）、`rag_recommend_search_answer_profile`（質問から検索・回答プロファイルを推薦する。参照先を 1 回検索して当たった資料で決め、LLM は呼ばない。#1850）、`rag_search`（`menu.search`。`POST /api/search` と同じ処理）。入出力は [backend/README.md](../backend/README.md) の「MCP」を参照。
- 根拠の本文の大きさ（#3321）: `rag_search` / `rag_retrieve_evidence` の根拠（`evidence[].excerpt`）は既定で chunk の全文（回答の生成に渡す本文と同じ）。応答の全体に予算（token の見積もり `EVIDENCE_TEXT_BUDGET_TOKENS`）を置いて順位の順に入れ、入らない根拠は本文を空にして `text_omitted` を立て、数を `evidence_text_omitted` に返す（ID・場所・長さは返す）。1 件の上限（`EVIDENCE_TEXT_ITEM_MAX_TOKENS`）を超える根拠だけ、質問に当たる部分を中心に、本文は文・段落、表は見出しの行を残して行の境界で切り、`truncated`・`excerpt_range`・`header_range`・`next_offset` を返す（続きは `rag_read_source` の `offset`）。規則と値の決め方は platform の `pr_backend_core.source_text`（Agent の公開 MCP の `sources` と観測も同じ規則）。
- 読み取りのツールの続き（#3344）: `rag_read_source` の `text`・`parent_text` と、`rag_read_section` / `rag_read_document` の上限に入りきらない最初の chunk は、上限の文字数（`max_chars`）に入る最後の文・段落、表は行の境界で切る（1 つの文・行だけで超えるときだけ読点・空白、最後に字で切る）。`next_offset`・cursor は返した範囲の終わりちょうどで、続けて読むと隙間も重なりも無く全文になる。表を途中から読むときは本文の先頭に見出しの行を付け、その位置を `header_range` に返す（見出しが上限の半分を超える表は付けない）。規則は `pr_backend_core.source_text` の `read_source_window`。
- 画面の操作だけの URL（#1311・#2389）: `rag_read_source` の `include_image_url`（図の領域の画像）と `include_preview`（根拠の文書の原文のプレビュー。種類・ページの一覧・ページの画像と原文の URL・強調に要る根拠の metadata）は、claim `purpose`（`figure_url` / `source_preview`）のあるサービストークン（Agent の画面の操作）でだけ作る。URL は利用者・chunk・版（プレビューは処理レシピ・原本か処理後のファイルかも）に縛った 5 分の HMAC 署名つきの path（`/api/figures/{token}`・`/api/source-previews/{token}/pages/{page}`・`…/content`）で、読むたびに利用者の今の権限・ナレッジベースの範囲と版を確かめ直す（`app/rag/signed_url.py`・`app/api/routes/figures.py`・`source_previews.py`）。プレビューの種類は RAG の根拠のパネルと同じ決め方（拡張子、Office は処理後の PDF）。
- 取消（#2691）: 呼び出し元（Agent の Run の取消・外部の client の中断）が接続を切ると、共通の MCP の入口が読み取り専用のツールの task を取り消す。回答の期限（#1422）が取り消され、回答の thread は次の工程（`_execution_step` の始め）・LLM の呼び出し・検索の I/O を始めない（実行中の LLM の呼び出しは、streaming なら次の event で、それ以外は戻った時点で止まる）。取り消した検索は回答の記録に保存せず、監査は `outcome=error`・`error_stage=cancelled`、メトリクスの outcome は `cancelled`、ログ `rag_search_cancelled`（止まった工程と秒）に残す。
- 署名つきの URL の契約（#3252）: 期限は発行から 5 分（`SIGNED_URL_TTL_SECONDS`。それより長いトークンは作らず、読まない）。トークンは「読んでよい対象」（利用者・文書・chunk・版。プレビューは処理レシピ・原本か処理後のファイルかも）を縛るだけで権限を持たず、読むたびに署名・期限（切れたら 410）・トークンの利用者の今の状態と権限（無効・検索の権限なしは 403）・その利用者の今の範囲で根拠の chunk が見えるか（見えなければ 404。範囲の外の KB・削除）・版（違えば 409）を確かめ直す。中身を書き換えたトークン（別の文書・chunk）と別の用途のトークンは署名で 403。頁の番号（`…/pages/{page}`）は path にあり、トークンの文書の頁だけを読む（文書に無い頁は 422）。発行済みのトークンを 1 つずつ即時に失効させる仕組みは持たない（保存しない stateless の署名）。権限を外す・利用者を無効にする・範囲から外すと、期限の前でも次に読むときから読めない。トークンを持つ人は期限までその利用者として同じ文書を読めるので、URL は保存せず（応答・ログ・Referer・cache に残さない）、Agent の画面が開く直前に作る。
- 保存した根拠の再表示（#3252）: 会話の回答の引用（`GET /api/chat/conversations/{id}`）・実行履歴の詳細（`GET /api/history/runs/{id}`）・保存された回答の詳細（`GET /api/search/answers/{id}`）は、保存した根拠を出すたびに、**見る人の今の範囲**（`OracleClient.accessible_document_ids`。検索・`rag_read_source` と同じ見え方の条件で、削除した文書は含まない）で文書を確かめる。今見られない文書の根拠は抜粋・場所・文書名を返さず、`withheld_evidence_count` に数だけを返し、回答の診断（読んでいない節のリンクなど）からもその文書を指す要素を除く（`app/rag/saved_evidence.py`）。画面は回答の根拠の折りたたみに「権限の無い根拠 N 件」（共有 UI の `EvidenceWithheldNote`）を出す。会話の添付の根拠は会話の見え方のまま。Agent の画面は同じ確かめを内部のツール `rag_readable_documents`（`tools/list` に出さない。`sub` は見る人）で行う。**既知の限界**: 回答の本文は保存した文のまま返す（その資料から作った文で、本文から該当の部分は消せない）。入口 × 主体 × 範囲の行列は `backend/tests/test_evidence_read_authz.py`。
- チャットは MCP で提供しない（#787）。チャットは画面（SSE の `POST /api/chat/conversations/{id}/messages/stream`）だけの機能で、MCP で提供するのは検索だけ。
- チャットの回答の作成は SSE の接続から切り離している（#1175。`app/rag/chat_answer_runs.py`）。送信を受けたプロセスの中の task が作成し、作成中の回答（`STREAMING`）・段階・最終の回答は `rag_messages` に保存する。SSE はその task の event を購読するだけで、接続が切れても作成は続く。画面は `GET /api/chat/conversations/{id}/messages/{質問の id}/stream`（`Last-Event-ID`）で続きを購読し直し、できなければ保存済みの会話を取り直す。停止は `POST .../messages/{質問の id}/cancel` の明示の取消。
- 処理の経過（チャット・RAG 検索・実行の詳細・回答の記録）は回答の trace（span の木）の投影で、span は 3 製品共通の span の表（`PLATFORM_TRACE_SPANS`。#3226 の案 A。`platform/docs/backend-standard.md`「共通の span の表」・`platform/docs/ux-contracts/run-history.md`「実測 trace と処理の経過」）にある。pipeline の `operation_trace(listener=...)` が知らせる span の開始・終了を、process ごとの書き手（`TraceSpanWriter`。API の lifespan で始めて止める。DB の接続の設定が無ければ書かない）へ渡す（`app/rag/trace_store.py` の `AnswerTrace`。queue に積むだけで回答を待たせない。持ち主は実行した利用者）。工程を測る span には表示用のデータ（`app/rag/span_display.py`。回答フローの工程は種類 `answer_step` と工程名・LLM の回数・結果の説明、回答フローの外の工程は工程の名前の種類）を付け、画面は工程の定義（`frontend/src/lib/chat-progress.ts`）で名前を付ける。工程の詳細（検索文・文書名など）は表に入れず、表を引く trace id・書けなかった span の数と一緒に製品の記録に置く（`TraceRecord`: チャットの回答の `progress_json`・実行履歴の `stages_json.trace`・回答の記録の `diagnostics_json.trace`）。資料との照合・承認済み FAQ からの回答も工程の span（`rag.engine.stage`）にする。会話の回答の保存（`chat_message_save`）は回答の trace が閉じた後なので span にしない。画面の要求・外部の MCP クライアント（API キー）の traceparent は表の親子に使わず新しい trace を始め、来た span は span link にだけ残す。`POST /api/mcp` の `tools/call` は server の span（`mcp.server`）の中で処理し、サービストークン（Agent）の要求だけ `params._meta.traceparent`（無ければ header）を親にして Agent の木につなげる（中の検索も同じ trace）。読み出しは記録ごとの API（`GET /api/chat/conversations/{id}/messages/{回答の id}/trace`・`/api/history/runs/{id}/trace`・`/api/search/answers/{id}/trace`・`/api/search/runs/{id}/trace`）で、記録を今の判定（「AI 活用」の見え方）で開き、その記録の trace id で表を読む（system_admin は全員の span、それ以外は自分の span。画面から trace id は受け取らない）。終端は記録の状態（正本）で、表で実行中のまま残った span は終端に合わせて閉じる。RAG 検索は実行の記録（実行履歴）を実行の終わりに保存するので、実行中は表を実行の id（配信の最初の `run` の trace_id）で引く。画面は共通の `useTraceProgressSnapshot`（`frontend/src/lib/trace-progress.ts`）で、実行中は読み直し、終わったら 1 回読む（回答の配信の SSE は回答の本文と終わりだけを運ぶ）。書けなかった span があれば「一部の処理だけを記録しています。」、span の無い古い形式の記録は古い形式の案内を出す。

## Oracle AI Database DDL 例

`app.clients.oracle` の `oracle_document_schema_sql()` / `oracle_vector_schema_sql()` / `oracle_search_audit_schema_sql()` / `oracle_ingestion_audit_schema_sql()` が返す DDL をベースにする。

```sql
CREATE TABLE rag_documents (
    document_id              VARCHAR2(64) PRIMARY KEY,
    file_name                VARCHAR2(512) NOT NULL,
    status                   VARCHAR2(32) NOT NULL,
    tenant_id_hash           CHAR(64),
    category_name            VARCHAR2(256),
    object_storage_path      VARCHAR2(1024),
    content_type             VARCHAR2(255),
    file_size_bytes          NUMBER(19),
    content_sha256           CHAR(64),
    duplicate_of_document_id VARCHAR2(64),
    extraction               JSON,
    error_message            VARCHAR2(2000),
    uploaded_at              TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    indexed_at               TIMESTAMP WITH TIME ZONE
);

CREATE INDEX rag_documents_content_sha256_idx
    ON rag_documents (content_sha256);

CREATE INDEX rag_documents_tenant_status_uploaded_idx
    ON rag_documents (tenant_id_hash, status, uploaded_at DESC);

CREATE TABLE rag_chunks (
    chunk_id        VARCHAR2(128) PRIMARY KEY,
    document_id     VARCHAR2(64) NOT NULL,
    tenant_id_hash  CHAR(64),
    chunk_index     NUMBER NOT NULL,
    chunk_text      CLOB NOT NULL,
    metadata_json   JSON,
    embedding       VECTOR(1024, FLOAT32),
    created_at      TIMESTAMP DEFAULT SYSTIMESTAMP
);

-- 既定の検索精度「高精度」（accurate）の推奨ビルド（#1990）
CREATE VECTOR INDEX rag_chunks_embedding_hnsw_idx
    ON rag_chunks (embedding)
    ORGANIZATION INMEMORY NEIGHBOR GRAPH
    DISTANCE COSINE
    WITH TARGET ACCURACY 98
    PARAMETERS (
        TYPE HNSW,
        NEIGHBORS 48,
        EFCONSTRUCTION 800
    );

CREATE INDEX rag_chunks_text_idx
    ON rag_chunks (chunk_text)
    INDEXTYPE IS CTXSYS.CONTEXT;

CREATE INDEX rag_chunks_tenant_document_idx
    ON rag_chunks (tenant_id_hash, document_id, chunk_index);

CREATE TABLE rag_search_audit (
    audit_id              VARCHAR2(64) DEFAULT RAWTOHEX(SYS_GUID()) PRIMARY KEY,
    event_type            VARCHAR2(32) DEFAULT 'rag.search' NOT NULL,
    trace_id              VARCHAR2(64) NOT NULL,
    request_id            VARCHAR2(128),
    tenant_id_hash        CHAR(64),
    user_id_hash          CHAR(64),
    outcome               VARCHAR2(32) NOT NULL,
    search_mode           VARCHAR2(16) NOT NULL,
    query_hash            CHAR(64) NOT NULL,
    query_chars           NUMBER(10) NOT NULL,
    filter_keys           JSON,
    memory_plan_id        VARCHAR2(32),
    top_k                 NUMBER(10),
    rerank_top_n          NUMBER(10),
    query_variant_count   NUMBER(10) DEFAULT 1 NOT NULL,
    guardrail_codes       JSON,
    guardrail_severities  JSON,
    retrieved_count       NUMBER(10) DEFAULT 0 NOT NULL,
    reranked_count        NUMBER(10) DEFAULT 0 NOT NULL,
    deduplicated_count    NUMBER(10) DEFAULT 0 NOT NULL,
    context_diversified_count NUMBER(10) DEFAULT 0 NOT NULL,
    context_group_expanded_count NUMBER(10) DEFAULT 0 NOT NULL,
    context_expanded_count NUMBER(10) DEFAULT 0 NOT NULL,
    context_compressed_count NUMBER(10) DEFAULT 0 NOT NULL,
    context_compression_saved_chars NUMBER(10) DEFAULT 0 NOT NULL,
    -- memory_plan_id・top_k・rerank_top_n・query_variant_count と、reranked_count から
    -- context_window_chars までの内訳の列は、#595 以降は既定値を書く（既存の行を変えないため残す。#596）。
    agent_memory_retrieved_count NUMBER(10) DEFAULT 0 NOT NULL,
    agent_memory_writeback_count NUMBER(10) DEFAULT 0 NOT NULL,
    agent_memory_writeback_status VARCHAR2(32) DEFAULT 'skipped' NOT NULL,
    evidence_count        NUMBER(10) DEFAULT 0 NOT NULL,
    support_count         NUMBER(10) DEFAULT 0 NOT NULL,
    structure_count       NUMBER(10) DEFAULT 0 NOT NULL,
    history_count         NUMBER(10) DEFAULT 0 NOT NULL,
    resolver_rejected_count NUMBER(10) DEFAULT 0 NOT NULL,
    insufficient_context_count NUMBER(10) DEFAULT 0 NOT NULL,
    citation_count        NUMBER(10) DEFAULT 0 NOT NULL,
    context_chars         NUMBER(10) DEFAULT 0 NOT NULL,
    context_window_chars  NUMBER(10),
    document_ids          JSON,
    config_fingerprint    CHAR(64),
    elapsed_ms            NUMBER(12, 3) NOT NULL,
    error_stage           VARCHAR2(64),
    error_type            VARCHAR2(128),
    created_at            TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE TABLE rag_ingestion_audit (
    audit_id               VARCHAR2(64) DEFAULT RAWTOHEX(SYS_GUID()) PRIMARY KEY,
    event_type             VARCHAR2(32) DEFAULT 'rag.ingestion' NOT NULL,
    trace_id               VARCHAR2(64) NOT NULL,
    request_id             VARCHAR2(128),
    tenant_id_hash         CHAR(64),
    user_id_hash           CHAR(64),
    document_id            VARCHAR2(64) NOT NULL,
    outcome                VARCHAR2(32) NOT NULL,
    source_sha256          CHAR(64) NOT NULL,
    source_bytes           NUMBER(19) NOT NULL,
    document_type          VARCHAR2(128),
    extraction_confidence  NUMBER(6, 5),
    chunk_count            NUMBER(10) DEFAULT 0 NOT NULL,
    vector_count           NUMBER(10) DEFAULT 0 NOT NULL,
    elapsed_ms             NUMBER(12, 3) NOT NULL,
    error_type             VARCHAR2(128),
    error_message          VARCHAR2(2000),
    created_at             TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);
```

document / chunk table には `tenant_id_hash` を持たせる。production（`PLATFORM_AUTH_MODE=authenticated`）では client の `X-Tenant-ID` を使わず、tenant なし（単一 tenant）で動かす。利用者と検索・回答プロファイル / ナレッジベースの対象範囲はログイン中の利用者（MCP ではサービストークンの利用者）から決め、client の `X-RAG-Allowed-*` header も使わない（#214 / #225）。local（`PLATFORM_AUTH_MODE=local`）だけは開発・検証のため、HTTP header `X-Tenant-ID` がある場合に raw tenant id を保存せず hash 化し、一覧・詳細・重複判定・retrieval を同一 tenant に閉じる（header がない場合は全体を参照できる）。同じく local では `X-RAG-Allowed-Document-Ids` / `X-RAG-Allowed-Category-Names` を付与すると、document id / category name scope を request context に保持し、document 一覧、詳細、chunk count、Oracle AI Vector Search、Oracle Text keyword search の SQL predicate に適用する。scope header が存在するが有効値がない場合は deny-all とする。

対象範囲（`AuditRequestContext` の `allowed_*`）は「`None` = 制限なし」「空の集合 = 拒否全件」で表し、範囲を作れないときは制限なしにしない（fail-closed。#3250）。文脈の無い処理（文脈を引き継がない thread・起動時の task など）は何も使えない既定の context（`NO_ACCESS_AUDIT_REQUEST_CONTEXT`）になり、production の要求の初期値（認証の dependency を通らない公開 path・404 など）も範囲を空の集合にする。範囲の制限の無い context は、利用者の要求の外の信頼した処理（取込の worker・job の子・CLI の `main`）だけが `system_access_context` / `with_system_access` で明示して作る。利用者の物（会話・保存済みの回答・フィードバック・実行履歴）は、利用者の分からない context（system の処理を含む）には 0 件で、どの会話の持ち主でもない（全員の分は system_admin の `all_users_visible` などの明示の印だけ）。範囲を真偽値で判定するコード（`if not allowed_*:`）は `tests/test_scope_truthiness_guard.py` が検出する。

**process の入口の規則（#3334）**: 要求の外で動く入口（systemd の unit・`init_script.sh` などが `python -m` で呼ぶ CLI・`pyproject.toml` の scripts・`__main__` の module・API の lifespan の task）は、範囲の条件で DB を読む前にどの context で動くかを決める。全件を扱う入口は `main` に `with_system_access` を付けるか、入口の loop を `system_access_context()` で囲む。要求から始まって全件を扱う後台の task（例: 章節の抽出規則の保存の後の全文書の作り直し）は、要求の利用者の範囲を引き継がないよう、task の中で `system_access_context()` に入る。範囲の条件を通らない入口（HTTP だけの CLI・生の接続の書き出し）は何も付けない。`tests/test_system_access_entry_points.py` が入口を機械で見つけ、import の閉包が `app.rag.request_context` に届く入口が一覧（`SYSTEM_ACCESS_ENTRY_POINTS` / `OTHER_ENTRY_POINTS`）にも `with_system_access` にも無ければ落ち、一覧の system の入口は `main` を呼んで `system_access_context` に入ることを確かめる。実行時は、文脈を作らないまま範囲の条件に来ると WARNING `no_access_context_read`（`reader` ごとに process で 1 回）を出し、黙って 0 件にしない（要求は middleware が必ず context を作るので出ない）。

監査 table は query 本文、OCR 原文、tenant/user id の raw 値を保存しない。検索は `query_hash` と `query_chars`、filter key、安全チェックの code、検索件数・citation 件数、引用した文書 ID、RAG 設定 fingerprint を保存する（旧 standard の検索・context の内訳の列は #595 以降は既定値。既存の監査の行を変えないため列は残す。#596）。回答フローの工程と根拠は回答の記録（`rag_answer_records`）に残す。tenant/user id は `tenant_id_hash` / `user_id_hash` として保存する。取込は `source_sha256` と `source_bytes` を保存し、trace id / request id でアプリログ・Langfuse・Prometheus と相関する。

## Trace export

`record_trace_span()` は構造化ログへ `rag_trace_span` を出し、`RAG_TRACE_EXPORT_HTTP_ENDPOINT` が設定されている場合は同じ脱機密化済み event を非同期 HTTP JSON で OpenTelemetry / Langfuse gateway へ送信する。export 対象は `trace_id`、stage 名、outcome、duration、低 cardinality attributes、`error_type` のみで、query 本文、context 本文、OCR 原文、prompt、例外 message は含めない。export queue が満杯または送信失敗しても RAG pipeline は継続し、失敗は `app.trace` logger の `rag_trace_export_*` イベントで確認する。

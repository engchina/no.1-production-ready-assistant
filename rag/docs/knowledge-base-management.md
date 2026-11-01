# ナレッジベース管理設計

> 本ドキュメントは、Production Ready RAG に「ナレッジベース管理」を追加するための設計メモである。
> Dify / RAGFlow / AnythingLLM / FastGPT / MaxKB / R2R などのプロダクト級 RAG が持つ
> dataset / knowledge base / collection の考え方を参考にしつつ、実装は本プロジェクトの確定スタック
> (モデルの API〔OpenAI 互換の API。LLM・埋め込み・再ランク〕/ Oracle AI Database / Vite + React Router) へ再マッピングする。

最終更新: 2026-06-28

---

## 1. 背景と結論

現在の実装は `rag_documents.category_name` による軽い分類、`X-RAG-Allowed-Document-Ids` /
`X-RAG-Allowed-Category-Names` による検索・一覧スコープを持つ。ただしこれは「文書属性」であり、
運用者が検索対象のまとまりを作り、アップロード時に所属先を選び、ナレッジベース単位で索引状況・エラー・検索範囲を
管理するための一等リソースではない。

プロダクト級 RAG では、文書単体よりも「どのナレッジベースに入っているか」が主要な運用単位になる。したがって本プロジェクトでも
`ナレッジベース` を first-class resource として追加する。

設計方針:

- `category_name` は廃止せず、タグ/カテゴリ相当の軽量 facet として残す。
- ナレッジベースは `rag_knowledge_bases` として独立管理する。
- 文書とナレッジベースは多対多の関連表で管理し、同じ文書・chunk・embedding を物理複製しない。
- 検索、評価、監査、認可はナレッジベース ID をスコープとして扱う。
- UI 表示名は日本語で「ナレッジベース」とし、i18n key 経由で管理する。

## 2. 参考プロジェクトから取り込む考え方

| 参考元 | 取り込む考え方 | 本プロジェクトでの再マッピング |
|---|---|---|
| Dify | Knowledge / Dataset をアプリ・検索範囲の単位にする | Oracle 上の `rag_knowledge_bases` と retrieval scope |
| RAGFlow | 複雑文書をナレッジベース単位で取込・検索・引用確認する | 既存の構造化抽出 / chunk metadata / citation と接続 |
| AnythingLLM | ワークスペース/コレクション単位で文書を整理する | tenant + knowledge base + document membership |
| FastGPT / MaxKB | ナレッジベース管理と RAG 検索 UI を一体化する | サイドナビ、アップロード、文書一覧、検索の共通スコープ |
| R2R | API first の文書管理・検索管理 | FastAPI の CRUD / membership / search API と pytest 契約 |

外部ベクトル DB、別 LLM プロバイダ、別フロントエンド基盤は導入しない。

### 2.1 文書所属モデルと構築設定表示の差分

- Dify の `Document` は単一 `dataset_id`、RAGFlow の `Document` は単一 `kb_id` を持つ。
  PowerRAG は RAGFlow の database / data model を共有するため、いずれも同じ原本を別 KB へ
  入れる場合は KB ごとに別文書として扱い、文書画面では 1 KB 分の設定だけを表示する。
- 本プロジェクトは文書と KB を多対多にし、同じ設定の KB は artifact を共有し、設定差分だけを
  別 materialization にする。このため文書詳細では、KB ごとの有効な構築設定を比較し、同一設定を
  1 グループへまとめて `適用予定 / 構築中 / 配信中 / 更新が必要 / エラー` を表示する。
- 表示するのは現在の有効設定であり、`配信中` のときだけ適用済みとみなす。設定変更後に対応する
  artifact が無い場合は `更新が必要` とし、現在設定を誤って適用済みとは表示しない。

## 3. 用語

| 用語 | 意味 |
|---|---|
| ナレッジベース | 検索対象文書をまとめる運用単位。例: 社内規程、製品 FAQ、契約書、障害対応手順。 |
| 文書 | アップロードされた原本ファイルと抽出結果。既存の `rag_documents`。 |
| membership | 文書がどのナレッジベースに所属するかを表す関連行。 |
| カテゴリ | 文書の軽量 facet。既存 `category_name`。ナレッジベースとは別物として残す。 |
| DEFAULT | アップロード時に明示選択がない文書を入れるデフォルトの所属先。予約名。 |

## 4. 機能範囲

### Phase 1 で必須

- ナレッジベースの作成、一覧、詳細、更新、アーカイブ。
- 文書アップロード時のナレッジベース指定。
- 既存文書のナレッジベース追加・削除。
- 文書一覧のナレッジベース絞り込み。
- RAG 検索のナレッジベース絞り込み。
- tenant / access scope とナレッジベース scope の AND 条件適用。
- backend pytest と frontend API/Vitest/Playwright の追加。

### Phase 1 では扱わない

- ナレッジベースごとの別 embedding モデル設定。
- ナレッジベースごとの外部 vector store。
- 人手承認ゲート付きの取込ワークフロー。
- ACL / RBAC の完全なユーザー管理 UI。
- GraphRAG 専用のグラフ管理 UI。

これらは将来拡張として扱う。

## 5. データモデル

### 5.1 基本方針

- `rag_documents` は引き続き文書原本・抽出結果・状態の正本。
- `rag_chunks` は文書ごとに一度だけ作成する。ナレッジベースごとに chunk を複製しない。
- `rag_document_knowledge_bases` で文書所属を表す。
- retrieval は `rag_chunks -> rag_documents -> rag_document_knowledge_bases -> rag_knowledge_bases` を join して絞り込む。
- `tenant_id_hash` をナレッジベースと membership にも持たせ、tenant predicate を軽くする。

### 5.2 Oracle DDL 案

```sql
CREATE TABLE rag_knowledge_bases (
    knowledge_base_id      VARCHAR2(64) PRIMARY KEY,
    tenant_id_hash         CHAR(64),
    name                   VARCHAR2(256) NOT NULL,
    description            VARCHAR2(2000),
    status                 VARCHAR2(32) DEFAULT 'ACTIVE' NOT NULL,
    default_search_mode    VARCHAR2(16) DEFAULT 'hybrid' NOT NULL,
    retrieval_config       JSON,
    created_at             TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at             TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    archived_at            TIMESTAMP WITH TIME ZONE,
    CONSTRAINT rag_knowledge_bases_status_ck
        CHECK (status IN ('ACTIVE', 'ARCHIVED')),
    CONSTRAINT rag_knowledge_bases_mode_ck
        CHECK (default_search_mode IN ('hybrid', 'vector', 'keyword'))
);

CREATE UNIQUE INDEX rag_knowledge_bases_tenant_name_uidx
    ON rag_knowledge_bases (
        NVL(tenant_id_hash, '__GLOBAL__'),
        LOWER(name)
    );

CREATE INDEX rag_knowledge_bases_tenant_status_idx
    ON rag_knowledge_bases (tenant_id_hash, status, updated_at DESC);

CREATE TABLE rag_document_knowledge_bases (
    knowledge_base_id       VARCHAR2(64) NOT NULL,
    document_id             VARCHAR2(64) NOT NULL,
    tenant_id_hash          CHAR(64),
    assigned_at             TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    assigned_by_user_id_hash CHAR(64),
    PRIMARY KEY (knowledge_base_id, document_id),
    CONSTRAINT rag_doc_kb_kb_fk
        FOREIGN KEY (knowledge_base_id)
        REFERENCES rag_knowledge_bases (knowledge_base_id)
        ON DELETE CASCADE,
    CONSTRAINT rag_doc_kb_document_fk
        FOREIGN KEY (document_id)
        REFERENCES rag_documents (document_id)
        ON DELETE CASCADE
);

CREATE INDEX rag_doc_kb_document_idx
    ON rag_document_knowledge_bases (document_id, knowledge_base_id);

CREATE INDEX rag_doc_kb_tenant_kb_idx
    ON rag_document_knowledge_bases (tenant_id_hash, knowledge_base_id, assigned_at DESC);
```

### 5.3 既存 schema への追加

`rag_documents` には必須の新規列を追加しない。既存 API と移行の互換性を保つため、所属は関連表だけで表現する。

`rag_search_audit` には `knowledge_base_ids JSON` を追加する。監査ログに名前・説明は保存しない。

```sql
ALTER TABLE rag_search_audit ADD knowledge_base_ids JSON;
```

`rag_ingestion_audit` は文書単位の監査のまま維持する。必要であれば低機密の `knowledge_base_count NUMBER(10)` を追加するが、
Phase 1 では必須にしない。

### 5.4 DEFAULT ナレッジベース

アップロード時にナレッジベースが指定されない場合は、tenant ごとに `DEFAULT` を自動作成し、その文書を所属させる。`DEFAULT` は予約名で、ユーザー作成・改名・アーカイブはできない。

説明は必須のため（#521）、DEFAULT の KB と検索・回答プロファイルには既定の説明を入れる。作成するとき（`_ensure_default_knowledge_base` / `_ensure_default_search_answer_profile`）と、
説明が空の既存の DEFAULT を読み込んだとき（検索・回答プロファイル一覧の `ensure_default_search_answer_profile`・DEFAULT 行きのアップロード）に補い、
migration `20260930_001_default_descriptions` も説明が空の DEFAULT に同じ文言を入れる。利用者が入れた説明は上書きしない。
文言は `DEFAULT_KNOWLEDGE_BASE_DESCRIPTION` / `DEFAULT_SEARCH_ANSWER_PROFILE_DESCRIPTION`（`app/schemas/`）。

理由:

- ナレッジベース未所属の文書が検索対象に混ざる状態を避ける。
- 既存 UI のアップロード体験を壊さず段階移行できる。
- CI / local 開発では tenant header がなくても `__GLOBAL__` 相当の `DEFAULT` 所属で動作する。

### 5.5 既存データ移行

初期移行では次のどちらかを選べるようにする。

1. 安全移行: 全既存文書を `DEFAULT` に所属させる。
2. カテゴリ移行: `category_name` がある文書は category ごとに同名ナレッジベースを作成し、未分類だけ `DEFAULT` へ入れる。

本番移行では安全移行を既定とし、カテゴリ移行は staging で件数・名称衝突を確認してから実行する。

## 6. Backend API

### 6.1 ナレッジベース CRUD

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/api/knowledge-bases?status=ACTIVE&q=&limit=50&offset=0&ids=` | 一覧。文書数・索引済み数・エラー数も返す。`ids`（繰り返し、最大 200）を渡すとその ID の KB だけを返す（status を省くとアーカイブ済みも返す。#302）。 |
| `POST` | `/api/knowledge-bases` | 作成。 |
| `GET` | `/api/knowledge-bases/{knowledge_base_id}` | 詳細。 |
| `PATCH` | `/api/knowledge-bases/{knowledge_base_id}` | 名前、説明、既定検索モードを更新。 |
| `POST` | `/api/knowledge-bases/{knowledge_base_id}/archive` | アーカイブ。文書と chunk は削除しない。 |

- 名前は tenant 内で一意（大文字小文字を区別せず、アーカイブ済みを含む）。作成・改名で重複すると 409 と理由を返す（#282）。
- 変更系（`PATCH` / `archive` / 文書の追加・外す）の応答は、文書数・索引済み数などの集計列を取り直した詳細を返す（#282）。
- アーカイブ済みの KB でも所属文書の一覧（`GET /api/documents?knowledge_base_id=`）は所属を返す。検索対象から外すのは検索の SQL だけ（#282）。
- アーカイブ済みの KB は変更しない。`PATCH`・文書の追加（`POST .../documents`）・外す（`DELETE .../documents/{document_id}`）・項目抽出の定義の保存は 409 を返す（画面の読み取り専用と同じ。#961）。
- **API の互換の変更（#302）**：`POST` / `PATCH` の `adapter_config` は受け付けない。指定すると（`null` や `{}` でも）422
  「adapter_config は指定できません。…」を返し、何も保存しない。以前は `retrieval_config` カラムへ正規化して保存していたが、
  3 層モデルでは取込・検索のどちらでも使わない（`rag/docs/agents/settings-responsibility.md`「KB の legacy adapter/query config は読み取りのみ許容」）。
  応答の `adapter_config`（保存値の読み取り互換）はこれまでどおり返す（表示専用の `effective_adapter_config` はパイプライン図とともに削除した。#2113）。
  旧クライアントは `adapter_config` を送らないように直す（名前・説明・既定検索モードは従来どおり更新できる）。
  なお free-form の `retrieval_config` の書き込みは互換のため残している（未整理。読み取り互換の保存値と同じカラム）。

作成 payload:

```json
{
  "name": "社内規程",
  "description": "就業規則、情報セキュリティ規程、申請手順を含む",
  "default_search_mode": "hybrid"
}
```

KB が持つのは所属（スコープ）と名前・説明だけとする。文書の処理（文書解析・文書分割など）は文書のレシピ、
検索方法（回答の検索と生成）と安全チェックは検索・回答プロファイルの検索・回答設定で扱い、品質評価はグローバル設定だけで扱う(#301。根拠確認・回答スタイルは #595 で削除した)。
旧 `retrieval_config` / `adapter_config`（`ingestion` / `query`）の保存値は後方互換の読み取り対象に留め、runtime では使わない。

> 3 層モデル（`rag/AGENTS.md`「RAG 設定責務」）以降、文書レシピの既定は global から解決し、KB の
> `adapter_config.ingestion` も取込では使わない（保存値の読み取り互換だけ）。

レスポンス summary:

```json
{
  "id": "kb_...",
  "name": "社内規程",
  "description": "就業規則、情報セキュリティ規程、申請手順を含む",
  "status": "ACTIVE",
  "default_search_mode": "hybrid",
  "document_count": 42,
  "indexed_document_count": 38,
  "error_document_count": 2,
  "searchable_chunk_count": 1280,
  "created_at": "2026-06-15T00:00:00Z",
  "updated_at": "2026-06-15T00:00:00Z",
  "archived_at": null
}
```

### 6.2 文書 membership API

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/api/documents?knowledge_base_id={knowledge_base_id}&status=&q=&limit=&offset=` | 対象ナレッジベースの文書一覧（文書一覧 API の絞り込み）。 |
| `POST` | `/api/knowledge-bases/{knowledge_base_id}/documents` | 既存文書を一括追加。 |
| `DELETE` | `/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}` | 文書をナレッジベースから外す。文書自体は削除しない。 |
| `GET` | `/api/documents/{document_id}/knowledge-bases` | 文書の所属ナレッジベース一覧。 |
| `PUT` | `/api/documents/{document_id}/knowledge-bases` | 文書の所属を指定リストに置換。 |

一括追加 payload:

```json
{
  "document_ids": ["doc_1", "doc_2"]
}
```

置換 payload:

```json
{
  "knowledge_base_ids": ["kb_1", "kb_2"]
}
```

制約:

- アーカイブ済みナレッジベースへ新規文書は追加できない。
- 文書が少なくとも 1 つのナレッジベースに所属する状態を原則とする。
- 最後の membership 削除は、代替所属先を指定するか、`DEFAULT` へ自動移動する。
  `DELETE /api/knowledge-bases/{id}/documents/{document_id}` は、利用者の KB 範囲外を含めてほかに所属がなければ
  `DEFAULT` へ所属させてから外す。`DEFAULT` にだけ所属する文書は外せない（409。#282）。

### 6.3 アップロード API

`POST /api/documents/upload` は multipart form に `knowledge_base_ids` を追加する。

```text
file=<UploadFile>
knowledge_base_ids=kb_1
knowledge_base_ids=kb_2
```

指定がない場合は `DEFAULT` へ所属させる。

`UploadResult` には `knowledge_bases` を追加する。

```json
{
  "id": "doc_...",
  "file_name": "security-policy.pdf",
  "status": "UPLOADED",
  "file_size_bytes": 12345,
  "content_sha256": "...",
  "duplicate_of_document_id": null,
  "knowledge_bases": [
    { "id": "kb_1", "name": "社内規程" }
  ]
}
```

### 6.4 文書一覧 API

`GET /api/documents` は `knowledge_base_id` query を追加する。

```text
GET /api/documents?knowledge_base_id=kb_1&status=INDEXED&q=規程&limit=50&offset=0
```

`DocumentSummary` には `knowledge_bases` を追加する。既存 `category_name` は互換性のため残す。

```json
{
  "id": "doc_...",
  "file_name": "security-policy.pdf",
  "status": "INDEXED",
  "category_name": "社内規程",
  "knowledge_bases": [
    { "id": "kb_1", "name": "社内規程" }
  ],
  "uploaded_at": "2026-06-15T00:00:00Z",
  "indexed_at": "2026-06-15T00:10:00Z"
}
```

### 6.5 検索 API

`POST /api/search` はナレッジベース scope を明示的に受け取る。

推奨 payload:

```json
{
  "query": "リモートワーク時の申請手順は？",
  "knowledge_base_ids": ["kb_1"],
  "top_k": 20,
  "rerank_top_n": 5,
  "mode": "hybrid",
  "filters": {
    "content_kind": "text"
  }
}
```

互換性のため、Phase 1 では `filters.knowledge_base_id` も単一 ID として受け付ける。ただし新規 UI は
`knowledge_base_ids` を使う。

検索・回答プロファイルで検索するときは `search_answer_profile_id` を 1 つ渡す（RAG 検索・チャット・MCP の `rag_search` が使う）。
検索・回答プロファイルの参照 KB を検索対象へ展開し、その検索・回答プロファイルの検索・回答設定で回答する。複数の検索・回答プロファイルを受ける
`search_answer_profile_ids` は #635 で削除し、送ると 422 を返す（読み捨てて検索・回答プロファイルの外を検索しないため）。

retrieval SQL の基本条件:

```sql
JOIN rag_document_knowledge_bases dkb
  ON dkb.document_id = d.document_id
JOIN rag_knowledge_bases kb
  ON kb.knowledge_base_id = dkb.knowledge_base_id
WHERE d.status = 'INDEXED'
  AND kb.status = 'ACTIVE'
  AND dkb.knowledge_base_id IN (...)
  AND tenant / access predicates
```

検索レスポンスの citation には、可能な場合だけ `knowledge_bases` または `knowledge_base_ids` を低機密 metadata として含める。
監査ログには ID のみ保存し、名前・説明は保存しない。

項目抽出の値による絞り込み（#549）は `filters.extraction_fields` に条件の JSON 配列（文字列）で渡す。

```json
{
  "filters": {
    "extraction_fields": "[{\"name\":\"金額\",\"value_type\":\"number\",\"op\":\"gte\",\"value\":\"1000000\"},{\"name\":\"契約日\",\"value_type\":\"date\",\"op\":\"gte\",\"value\":\"2025-01-01\"}]"
  }
}
```

- 演算子は `eq`（一致）・`gte`（以上）・`lte`（以下）。`string` / `bool` は `eq` だけ、`number` / `date` は 3 つとも使える。
  範囲は `gte` と `lte` の 2 つの条件で指定し、条件どうしは AND。値は `number` が数値、`date` が `YYYY-MM-DD`、
  `bool` が `true` / `false`。条件は 10 件まで。検証は `app/schemas/search.py` の `ExtractionFieldCondition`。
- 絞り込みは文書単位の `EXISTS` で、文書の採用中の抽出（active な chunk_set が参照する `rag_document_extractions` の行）の
  `extraction_json.fields` を `JSON_TABLE` で行にして、項目名と値を比べる（`app/clients/oracle.py` の
  `_extraction_field_where`）。項目名と値は bind で渡し、SQL に埋めるのは演算子と列の許可リストの値だけ。
- 項目の無い文書、`number` / `date` に変換できない値（例: 「約100万円」）は一致しない（除かれる）。取込は抽出値を
  型に寄せて保存する（`normalize_field_value`。桁区切りを除いた数字・`YYYY-MM-DD`・`true` / `false`）。
- 関数索引は作っていない。実データの件数で実行計画を確かめ、必要なら検討する。
- 画面の候補は `GET /api/search/extraction-fields?search_answer_profile_id=` が返す（選んだ検索・回答プロファイルの参照 KB の定義の和集合）。アーカイブ済みの検索・回答プロファイルは検索と同じく 409（#961）。

### 6.6 評価 API

`/api/evaluation/run`（と job の `/api/evaluation/jobs/run`）の case に `knowledge_base_ids` を追加する。

```json
{
  "case_id": "remote-work-policy",
  "query": "リモートワーク時の申請手順は？",
  "knowledge_base_ids": ["kb_1"],
  "relevant_document_ids": ["doc_1"],
  "expected_keywords": ["申請", "承認"]
}
```

これにより golden set をナレッジベース単位で管理できる。

## 7. 認可・tenant・監査

### 7.1 tenant

- production（`PLATFORM_AUTH_MODE=authenticated`）では client の `X-Tenant-ID` を使わず、tenant なし（単一 tenant）で動かす（#225）。利用できるナレッジベースはログイン中の利用者（MCP ではサービストークンの利用者）のロールの対象範囲で決める（#214）。
- local（`PLATFORM_AUTH_MODE=local`）だけは開発・検証のため、`X-Tenant-ID` がある場合にナレッジベース、membership、文書、chunk を同じ `tenant_id_hash` に限定する。
- raw tenant id は DB / レスポンス / 監査ログへ保存しない。
- tenant header がない local / CI では全体を参照できるが、`DEFAULT` は global 相当として扱う。

### 7.2 access scope

既存 scope に加えて、次の header を追加する。

```text
X-RAG-Allowed-Knowledge-Base-Ids: kb_1,kb_2
```

適用ルール:

- `allowed_document_ids`、`allowed_category_names`、`allowed_knowledge_base_ids` はすべて AND 条件。
- header が存在するが有効値が 0 件の場合は deny-all。
- ナレッジベース一覧は許可された ID のみ返す。
- 文書一覧、詳細、membership 更新、検索 retrieval、評価 retrieval は同じ predicate を使う。
- membership 変更は、対象ナレッジベースと対象文書の両方にアクセスできる場合だけ許可する。

### 7.3 監査

検索監査へ追加する項目:

- `knowledge_base_ids`: 検索 scope に使われた ID の配列。
- `knowledge_base_count`: scope 件数。
- `filter_keys`: `knowledge_base_ids` または `knowledge_base_id` を含める。

ログへ出さない項目:

- query 本文。
- OCR 原文。
- ナレッジベース名・説明。
- raw tenant id / user id。
- 例外 message。

## 8. UI / UX 設計

### 8.1 ナビゲーション

サイドナビの「データ取込」セクションに `ナレッジベース` を追加する。

推奨順:

1. ナレッジベース
2. アップロード
3. 文書インデックス

理由:

- ユーザーは先に検索対象の箱を作り、その後にアップロードする。
- アップロード済み文書の管理もナレッジベース詳細から始められる。
- 既存のサイドナビ構造を保ち、深い階層にしない。

新規 route:

```ts
knowledgeBases: "/knowledge-bases"
knowledgeBaseDetail: "/knowledge-bases/:id"
```

### 8.2 ナレッジベース一覧

目的: 管理者・運用者が検索対象のまとまりと状態を素早く把握する。

表示要素:

- ページヘッダー: `ナレッジベース`
- 主アクション: `作成`
- 検索 input: 名前・説明の部分一致。
- status filter: `すべて / 有効 / アーカイブ済み`
- 一覧 table:
  - 名前
  - 説明
  - 文書数
  - 索引済み
  - エラー
  - 検索対象チャンク
  - 更新日時
  - 操作
- empty / loading / error state は platform の `docs/ux-contracts/messaging.md` に従う。

UI 方針:

- 業務系コンソールなので、カードを並べるよりも dense な table を主表示にする。
- 行クリックで詳細へ遷移する。操作ボタンは編集・アーカイブなど明確な command だけにする。
- 破壊的操作は確認ダイアログを出す。アーカイブは文書削除ではないことを明記する。
- モバイル幅では主要列だけ表示し、その他は詳細画面へ寄せる。

### 8.3 作成・編集

作成は一覧の上のフォーム、編集は詳細の概要でその場の編集にする（#302）。詳細の「編集」（`ObjectActionBar`。
アーカイブ済みには出さない）で概要の名前・説明がフォームに変わり、`保存`（primary）/ `キャンセル` で閉じる。

- 名前と説明は必須（#521）。名前は最大 256 文字、説明は最大 2000 文字（作成と同じ）。空・空白だけは画面が欄の直下に
  `FieldError` を出して送らず、最初の不正な欄へフォーカスする。API も空・空白だけ・`null` の説明を 422 で拒否する
  （更新で説明を省略したときは変更しない）。検索・回答プロファイルも同じ。
- 説明が必須になる前に作った説明の空の KB・検索・回答プロファイルも一覧・詳細で読める。編集して保存するときに説明の入力を求める
  （変更がなくても閉じない）。
- DEFAULT は名前を読み取り専用にし、説明だけを送る。
- 同名（大文字小文字を区別せず、アーカイブ済みを含む）の 409 は、名前の欄の下に理由を出す。その他の失敗はフォームの下の `FormStatus`。
- 変更があるときは離脱ガードで確認する。変更がなければ Esc でも閉じる。
- 構築設定（`adapter_config`）は API でも受け付けない（§6.1）。

以下は当初の設計案（参考）。

フィールド:

- 名前: required、最大 256 文字。
- 説明: optional、最大 2000 文字。
- 既定検索モード: segmented control (`hybrid / vector / keyword`)。
- 構築設定: 折りたたみ領域。文書解析、文書分割、索引構築、品質 gate を設定。
- 検索・回答設定は検索・回答プロファイル側で設定し、KB 作成/編集フォームには出さない。

UX 要件:

- label を必ず表示し、placeholder だけにしない。
- validation error は該当 field の直下に表示する。
- submit 中は primary button を loading / disabled にする。
- 成功時は toast、失敗時は form status と toast を使い分ける。
- Escape / キャンセルで閉じられるが、未保存変更がある場合は確認する。

### 8.4 ナレッジベース詳細

構成:

- breadcrumb: `ナレッジベース / {name}`
- header:
  - 名前
  - status badge
  - 文書数、索引済み、エラー、検索対象チャンク
  - actions: `アップロード`, `文書を追加`, `編集`, `アーカイブ`
- tabs:
  - `文書`
  - `概要`
  - `設定`

`文書` tab:

- 既存の文書一覧 table を再利用する。
- status filter / search / pagination / bulk selection を維持する。
- 一括操作:
  - 取込
  - 所属解除
  - 別ナレッジベースへ追加
- 所属解除は文書削除ではないことを確認ダイアログに明記する。

`概要` tab:

- 最近のアップロード/索引 activity。
- エラー文書の短いリスト。
- 検索対象 chunk 数の推移は将来拡張。Phase 1 は静的 metrics でよい。

`設定` tab:

- 名前、説明、既定検索モード、retrieval config。
- アーカイブ状態の説明。

### 8.4.1 KB を選ぶ UI（#302、#578）

KB の選択は、共有 UI の検索できる選択部品（`SearchableMultiSelect` / `SearchableSelectField`。#578）で作る。
複数選択（検索・回答プロファイルの参照 KB・品質評価・文書詳細の所属先は `KnowledgeBaseScopePicker`、アップロードの登録先）は
「検索欄 ＋ 候補の一覧（開いている間だけ）＋ 選択済みの chip」、文書インデックスの絞り込みは単一選択
（ボタンに選択中の名前を切らずに出し、押すと検索欄と候補の一覧が開く）。

候補の取得は `useKnowledgeBaseChoices`（`frontend/src/lib/queries.ts`）の 1 か所で決める。

- まず有効な KB の先頭 200 件（`KNOWLEDGE_BASE_LOCAL_FILTER_LIMIT` = 一覧 API の `limit` の最大値）を 1 回で取る。
  全件がそこに収まれば、検索は画面側で絞り込む（入力のたびに問い合わせない）。既定の KB を先頭に名前の順（3 製品で共通の
  名前の順 `compareNames`。記号 → 数字（数値の順）→ 英字 → かな → 漢字。#2753）で並べ、文書の最も多い KB に「最多」を付ける。
- 201 件以上なら、サーバー側の検索に切り替える（`GET /api/knowledge-bases?status=ACTIVE&q=`。名前・説明の部分一致）。
  1 回 50 件を取り、複数選択は「さらに表示」、単一選択は一覧の下端までのスクロールか最後の候補からの ↓ で続きを読む。
  全件をページ送りで読み切らない（旧 `useAllKnowledgeBases` は削除した）。候補はサーバーの順（DEFAULT → 更新の新しい順）。
- 検索語は部品の `SearchField` が確定した値（300ms・Enter・IME の変換の確定後）。親で遅延させない（UX 契約 page-archetypes.md の規則 7）。
- 文書のない KB も隠さない（作ったばかりの KB へ登録・所属させるため。右端の「0 文書」で分かる）。

選択済みの KB は `ids` で引き、候補のページに無くても chip（単一選択はボタン）に名前を出す。アーカイブ済みの KB は
「アーカイブ済み」、見つからない ID（存在しない・範囲外）は「見つかりません」を chip に添える（色だけに頼らない）。
文書インデックスで復元した絞り込みの KB が削除・アーカイブ済みなら、その条件だけ「すべて」に戻す。

### 8.4.1a 項目抽出の定義（#548）

項目抽出（「メタデータ/項目抽出」）で取り出す項目の定義は、KB ごとに持てる（`rag_knowledge_bases.extraction_fields`、
`GET` / `PUT /api/knowledge-bases/{id}/extraction-fields`）。KB の詳細の「抽出する項目」で編集する。

- KB に定義が無い（NULL）ときは、全体の既定（「取込・検索・回答設定 › 文書解析」の「解析後の処理」の項目の定義、
  `extraction-fields.json`）を使う。既存の KB はすべて NULL のため、既存環境の抽出は変わらない。
  全体の既定を一度も保存していない（ファイルが無い）環境は、標準の 4 項目（文書の種類・文書タイトル・発行日・作成日・
  発行元・作成部署。`STANDARD_FIELD_DEFINITIONS`）を全体の既定にする（#556）。保存した定義は 0 件でもそのまま使い、
  `GET /api/settings/extraction-fields` の `uses_standard` が「未保存で標準の項目を使っている」を示す。
  `DELETE /api/settings/extraction-fields`（画面の「標準の項目に戻す」）は保存した定義のファイルを消し、標準の項目に戻す。
  `PUT` の `fields: null` で全体の既定に戻す。空の定義（`fields: []`）は「この KB では項目を抽出しない」。
- 取込は、文書が属する有効な KB の定義を使う。**複数の KB に属する文書は、各 KB の定義（無ければ全体の既定）の
  和集合**で抽出する。同じ項目名（大文字小文字を区別しない）は、作成の古い KB の定義（説明・型）を使う。
  所属する有効な KB が無い文書は全体の既定を使う（`resolve_field_definitions`）。
- 定義の検証は全体の既定と同じ（項目名は必須・重複不可・50 項目まで）。アーカイブ済みの KB は変更できない（409）。
- 定義を変えても既存の文書の抽出値は変わらない。次の取込（再解析）から効く（既存文書の再抽出は #550）。
  項目抽出のレイヤーの「作り直しが必要」（#550）は、文書ごとの今の定義（上と同じ解決。
  `layer_fingerprint.current_field_definitions`）と比べる。文書一覧では、層のある文書の所属 KB の定義を
  1 回の問い合わせ（`list_documents_extraction_field_sets`）で読む。

### 8.4.1b 項目抽出の実行（入力の予算・分割・統合・再開。#2720）

項目抽出の実行は文書レシピの工程（`IngestionPipeline._attach_extraction_fields`）が持つ。KB は項目の定義だけを持ち、
検索・回答プロファイルには取込の設定を置かない。分け方と統合の規則は `app/rag/field_extraction_units.py`。

- **入力の予算**: 既定のテキストモデルの「入力の上限（tokens）」（システム設定 › モデル）の
  `RAG_FIELD_EXTRACTION_INPUT_TOKEN_PERCENT`（既定 40%）から、毎回送る文（system の指示・抽出の指示・項目の定義・
  応答の JSON Schema）の見積もりと枠の余地（512）を引いた残りを、1 回の要求の本文の予算にする。token は platform の
  `estimate_input_tokens` で見積もる（tokenizer は持たない）。合成の入力の実測（2026-10-20、openai.gpt-oss-120b）で、
  まれな漢字が続く本文は実際の token が見積もりの約 1.8 倍、ふつうの日本語の文は見積もり以下だったため、残りの 60% を
  出力の枠と見積もりの誤差の余裕にする。項目の定義だけで予算が尽きる（本文の予算が 256 未満）ときは、取込を止めて
  項目の数・説明を減らすよう案内する。本文を黙って切り捨てない。
- **分割**: 解析の成果物の原文の要素（見出し・段落・表）の順に、予算に収まる単位へ詰める。予算を超える
  要素は行 → 文 → 文字の順に分け、Markdown の表は表頭（1・2 行目）を各部分に付け直す。2 つ以上に分けたときは、
  各単位の頭に「[文書の一部 i/n] 見出し: …」を付け、前の単位の末尾（予算の 10% まで）を重ねる（境界をまたぐ項目名と
  値のため）。重ねた範囲は単位の `overlap`、新しく読む範囲は `atoms` に分けて持ち、全要素がちょうど 1 回 `atoms` に
  入る。検索用の chunk・解析の segment とは目的と予算が違うため、境界・件数をそろえない。
- **応答の検証**: 応答の形 `{"fields": [...]}` を JSON Schema（`FIELD_EXTRACTION_RESPONSE_SCHEMA`）で渡し、Pydantic で
  外側を検証してから項目を 1 つずつ読む（定義外・空の値・同じ応答の中の重複は捨てる）。形が合わない応答は「出力の形の
  誤り」として共通の実行器が回数を絞って呼び直す。
- **統合**: 項目の定義は 1 項目 1 値（単値）。型ごとに正規化した値（幅・大文字小文字・空白をそろえる）で同じ値をまとめ、
  根拠（値が書かれた要素。見つからなければ単位）の数が多い値、同じなら文書の前にある値を採る。最後の値・confidence の
  高い値で置き換えない。値には書かれた要素（`source_element_id`）と頁を出典として付ける。違う値の候補が残った項目は
  実行結果の `conflict_fields`（名前だけ）に刻む。
- **実行結果**: `parser_artifacts.field_extraction_outcome` に `status`（completed / no_definitions / no_text）・`field_count`
  と、単位の数（`unit_count`・`succeeded_unit_count`・`reused_unit_count`・`failed_unit_count`・`skipped_unit_count`・
  `resplit_count`）・`candidate_count`・`conflict_fields` を刻む。
- **失敗の分類**: 共通の実行器（`pr_backend_core.bounded_calls`）の分類で分ける。入力の上限の超過（413・context の長さ・
  vLLM 系の `prompt length … exceeds the max_model_len`）は、その単位の予算を半分にして 3 回まで分け直す。分類できない
  400 は入力の上限の超過に読み替えない（「要求の誤り」）。429・一時的な 5xx・接続・時間切れは実行器が待って再試行し、
  使い切ったら間隔をあけて 1 回だけ呼ぶ。設定の誤り・権限・割り当ての使い切りは実行器が取込を止める。単位の失敗は分類
  （入力の上限の超過 / 出力の打ち切り / 応答の形 / 要求の誤り / 安全の拒否 / 一時的な障害 / 分類できない誤り）・HTTP の
  状態・単位の数だけで文言とログ（`field_extraction_unit_failed` / `field_extraction_failed`）を作り、応答の本文・例外の文・
  本文・値は出さない。1 つも成功しないうちに失敗したら残りの単位は呼ばない（同じ誤りをくり返さない）。
- **再開**: 成功した単位の結果を、抽出の成果物の置き場所（`RAG_EXTRACTION_ARTIFACT_PREFIX` の下の
  `<文書>/fields/<hash>.json`。文書の削除で消える）に保存する。名前は方式の版・モデル・項目の定義・指示・応答の形と
  単位の本文の hash で、どれかが変われば使い直さない。1 つでも失敗した単位があれば取込を止め、途中の結果は抽出結果に
  入れない（旧 active の成果物を保つ）。再実行では保存した単位を使い、失敗した単位だけを呼ぶ。
  `RAG_EXTRACTION_ARTIFACT_CACHE_ENABLED=false` なら保存しない。

### 8.4.2 アーカイブした KB と検索・回答プロファイルの参照（#302）

KB をアーカイブしても検索・回答プロファイルの `knowledge_base_ids` からは外さない（検索・回答プロファイルの設定を黙って変えない）。
アーカイブ済み・存在しない KB は検索されないため、検索・回答プロファイルの画面で警告する。

- API：検索・回答プロファイルの詳細は `knowledge_bases`（アーカイブ済みを含み `status` 付き）と `missing_knowledge_base_ids`、
  一覧・詳細は `archived_knowledge_base_count` / `missing_knowledge_base_count` を返す（tenant 内で解決。利用者の KB 範囲では絞らない）。
- 一覧：該当する検索・回答プロファイルがあれば `Banner`（warning）を出し、「参照 KB」列に `StatusBadge`（warning）「要確認」を付ける（アーカイブ済みの検索・回答プロファイルは除く）。
- 編集画面：`Banner` にアーカイブ済みの KB の名前と見つからない KB の ID を並べ、外して保存するよう案内する。すべてが該当するときは
  「結果が 0 件になる」ことも伝える。警告は下書き（選択中の KB）に合わせて更新し、外すと消える。
- アーカイブの確認ダイアログにも「参照している検索・回答プロファイルでも検索されなくなる」ことを書く。

### 8.5 アップロード画面

アップロード前にナレッジベース選択を表示する。

推奨:

- 初期値は最近使ったナレッジベース、なければ `DEFAULT`。
- 単一選択を基本にし、詳細オプションで複数選択を許可する。
- 選択肢には status、文書数、説明を出す。
- アーカイブ済みナレッジベースは選択不可。

アップロード後の `DocumentWorkspace` には所属ナレッジベースの badge と、同一設定をまとめた
ナレッジベース別の構築設定グループを表示する。

### 8.6 文書インデックス

変更点:

- filter に `ナレッジベース` select を追加する。
- table の `カテゴリ` 列は残すが、主列として `ナレッジベース` を追加する。
- 複数所属は badge で最大 2 件まで表示し、超過分は `+N` とする。
- 行内操作に `所属を変更` を追加する。

### 8.7 RAG 検索

検索フォームの上部に `検索対象ナレッジベース` を置く。

要件:

- 既定では最近使ったナレッジベースを選択する。
- 選択なしで全体検索を許可するかは権限に従う。運用 UI では明示的に `すべてのアクセス可能なナレッジベース` と表示する。
- 検索結果の citation にナレッジベース badge を表示する。
- 検索 scope は URL query または state に保持し、戻る操作で復元する。

### 8.8 アクセシビリティとレスポンシブ

UI 実装時の必須確認:

- 375px 幅で横スクロールしない。
- table はモバイルで重要列優先にする。必要なら行詳細展開を使う。
- すべての input/select/dialog/button に label または aria-label を付ける。
- icon-only button は tooltip と aria-label を持つ。
- keyboard だけで作成、編集、所属変更、検索実行ができる。
- focus ring を消さない。
- loading / empty / error / success state を platform の `docs/ux-contracts/messaging.md` に合わせる。
- button size / variant / 配置は platform の `docs/design-system/` と `docs/ux-contracts/buttons.md` に合わせる。

## 9. Backend 実装方針

### 9.1 schema

追加予定:

- `backend/app/schemas/knowledge_base.py`
  - `KnowledgeBaseStatus`
  - `KnowledgeBaseSummary`
  - `KnowledgeBaseDetail`
  - `KnowledgeBaseCreateRequest`
  - `KnowledgeBaseUpdateRequest`
  - `KnowledgeBaseDocumentAssignmentRequest`
  - `KnowledgeBaseRef`

既存 schema 変更:

- `DocumentSummary.knowledge_bases: list[KnowledgeBaseRef]`
- `DocumentDetail.knowledge_bases: list[KnowledgeBaseRef]`
- `UploadResult.knowledge_bases: list[KnowledgeBaseRef]`
- `SearchRequest.knowledge_base_ids: list[str] = []`
- `SearchDiagnostics.knowledge_base_count: int`

### 9.2 OracleClient

追加予定メソッド:

- `create_knowledge_base(...)`
- `list_knowledge_bases(...)`
- `get_knowledge_base(...)`
- `update_knowledge_base(...)`
- `archive_knowledge_base(...)`
- `assign_documents_to_knowledge_base(...)`
- `remove_document_from_knowledge_base(...)`
- `replace_document_knowledge_bases(...)`
- `list_document_knowledge_bases(document_id)`

既存メソッド変更:

- `create_document(..., knowledge_base_ids: list[str] | None = None)`
- `list_documents(..., knowledge_base_id: str | None = None)`
- `count_documents(..., knowledge_base_id: str | None = None)`
- retrieval where builder に `knowledge_base_ids` を追加。

Local store も Oracle adapter と同じ契約で更新し、単体テストが外部 DB なしで通るようにする。

### 9.3 取込 pipeline

取込処理自体は文書単位のまま、抽出と索引を分ける。

- `UPLOADED -> INGESTING -> REVIEW -> INDEXING -> INDEXED` は文書状態。
- `EXTRACT` job は原本読取/parse/OCR/構造抽出まで、`INDEX` job は保存済み抽出から chunk / embedding / Oracle index / KB binding までを担当する。
- 同じ文書が複数ナレッジベースに所属しても、同じ取込設定なら chunk / embedding は共有する。
- ナレッジベース所属変更では再 embedding しない。
- 検索対象になるかどうかは、文書が `INDEXED`、所属ナレッジベースが `ACTIVE`、配信中 chunk_set binding が存在するかで決まる。

## 10. Frontend 実装方針

実装（`frontend/src/components/knowledge-bases/`）:

- `KnowledgeBaseManagementClient.tsx`: 一覧（検索・状態の絞り込み・ページング）と作成フォーム
- `KnowledgeBaseDetailClient.tsx`: 詳細（所属文書の追加・解除、アーカイブ、検索テスト、関係情報）
- `KnowledgeBaseScopePicker.tsx` / `KnowledgeBaseMultiSelect.tsx`: 評価・検索・回答プロファイル・アップロード・文書詳細で使う複数選択と、文書インデックスの絞り込みの選択肢・文言（共有の `SearchableMultiSelect` / `SearchableSelectField`。200 件以下は画面側、超えるとサーバー側の検索。#302 / #578）
- `KnowledgeBaseStatusPill.tsx`: 状態のバッジ（一覧・詳細で共有）
- `KnowledgeBaseSearchTestPanel.tsx`: 詳細の検索テスト
- `KnowledgeBaseGraphView.tsx`: 関係情報の読み取り専用の図（高度な診断。取込パイプラインの図は #2113 で削除）

既存変更:

- `APP_ROUTES` に `knowledgeBases` を追加。
- `NAV_SECTIONS` に `ナレッジベース` を追加。
- `api.ts` に knowledge base 型と API client を追加。
- `queries.ts` に TanStack Query hooks を追加。
- `UploadWorkspace` に `KnowledgeBasePicker` を追加。
- `FileListClient` に knowledge base filter / column / membership action を追加。
- `SearchClient` に search scope picker を追加。

i18n key 例:

```ts
"nav.knowledgeBases": "ナレッジベース"
"knowledgeBases.title": "ナレッジベース"
"knowledgeBases.create": "作成"
"knowledgeBases.edit": "編集"
"knowledgeBases.archive": "アーカイブ"
"knowledgeBases.field.name": "名前"
"knowledgeBases.field.description": "説明"
"knowledgeBases.empty.title": "ナレッジベースがありません"
"knowledgeBases.error.load": "ナレッジベースの取得に失敗しました。"
```

## 11. テスト範囲

### 11.1 Backend pytest

追加・更新するテスト:

- schema validation:
  - name required / max length。
  - `default_search_mode` enum。
  - `knowledge_base_ids` の空値・重複除去。
- API:
  - create/list/get/update/archive。
  - archived KB への document assignment 拒否。
  - upload with knowledge base。
  - upload without knowledge base uses default。
  - list documents by knowledge base。
  - search by knowledge base。
- Oracle adapter:
  - DDL artifact に `rag_knowledge_bases` / `rag_document_knowledge_bases` が含まれる。
  - membership insert/delete。
  - retrieval SQL が membership join と `kb.status = 'ACTIVE'` を含む。
  - tenant predicate と allowed knowledge base predicate が入る。
- local store:
  - multi membership。
  - search scope filtering。
  - deleting document removes membership。
- audit:
  - search audit に `knowledge_base_ids` が入る。
  - name / description は audit に出ない。

### 11.2 Frontend Vitest

追加・更新するテスト:

- `api.listKnowledgeBases` query string。
- `api.createKnowledgeBase` payload。
- `api.uploadDocument` が `knowledge_base_ids` を FormData に入れる。
- `api.search` が `knowledge_base_ids` を送る。
- `KnowledgeBaseBadges` の省略表示。
- `KnowledgeBasePicker` の empty / loading / selected state。

### 11.3 Playwright

UI/UX 変更なので必須。

主要 e2e:

- ナレッジベース一覧を表示し、作成 dialog で新規作成できる。
- 名前 validation error が field 直下に出る。
- ナレッジベース詳細から文書を追加・所属解除できる。
- アップロード時にナレッジベースを選択でき、アップロード後に badge が表示される。
- 文書インデックスでナレッジベース filter が効く。
- RAG 検索で選択したナレッジベース ID が request payload に入る。
- 375px と desktop 幅で、主要操作に横スクロールや重なりがない。
- keyboard 操作で作成 dialog を開き、保存/キャンセルできる。
- loading / empty / error state を確認する。

### 11.4 実行コマンド

実装後に最低限実行する。

```bash
cd backend && uv run pytest && uv run ruff check . && uv run mypy .
cd frontend && npm run lint && npm run build && npm run test -- --run
cd frontend && npm run e2e
```

Playwright 実行時は dev server を起動し、desktop と mobile viewport の両方を確認する。

## 12. 段階実装計画

### Step 1: Backend foundation

- `knowledge_base.py` schema 追加。
- Oracle DDL artifact 追加。
- `OracleClient` に CRUD / membership 追加。
- request context に `allowed_knowledge_base_ids` を追加。
- pytest で DB 契約を固定する。

### Step 2: Document and search integration

- upload API に `knowledge_base_ids` 追加。
- document summary/detail に `knowledge_bases` 追加。
- document list / count に `knowledge_base_id` filter 追加。
- search request / retrieval / diagnostics / audit に `knowledge_base_ids` 追加。
- evaluation case に `knowledge_base_ids` 追加。

### Step 3: Frontend management UI

- route / nav / i18n 追加。
- ナレッジベース一覧・作成/編集 dialog。
- ナレッジベース詳細・文書 membership UI。
- Vitest と Playwright を追加。

### Step 4: Existing flows update

- upload に `KnowledgeBasePicker`。
- file list に filter / column。
- search に scope picker。
- mobile / desktop / keyboard / empty-loading-error state を Playwright で確認。

### Step 5: Migration and ops

- 既存文書の構築 artifact 回填は [oracle-variant-backfill-runbook.md](./oracle-variant-backfill-runbook.md) を正とする。
- `app.rag.variant_backfill_cli` で read-only 検証 SQL と manifest を生成し、staging artifact として保存する。
- 実データの回填はレビュー済み手順で行い、parser / preprocess 差分が再抽出できない文書は `needs_reingest` として分離する。
- staging で category migration option を検証する。
- 監査 table migration と docs/rag-architecture.md / backend README を更新する。

## 13. 受け入れ基準

- ユーザーはナレッジベースを作成し、そこへ文書をアップロードできる。
- 文書は 1 つ以上のナレッジベースに所属する。
- 同じ文書を複数ナレッジベースに所属させても chunk / embedding は複製されない。
- ナレッジベースを選択した検索は、その所属文書の `INDEXED` chunk だけを retrieval 対象にする。
- tenant / document / category / knowledge base の認可 scope はすべて AND 条件で効く。
- アーカイブ済みナレッジベースは新規 upload / assignment / search scope に使えない。
- 監査ログに query 本文、OCR 原文、ナレッジベース名・説明、raw tenant/user id が出ない。
- UI は日本語 i18n、既存 Button / Messaging spec、Playwright 検証を満たす。


項目抽出と章節の要約の派生の要素は、本文と分けて検索用 Chunk にします。親子階層を含むすべての分割方式で同じ扱いです。派生の Chunk に検索で当たると、対応する原文の Chunk を回答の根拠に使います。既存の抽出結果に派生の要素がある文書は Chunk から再処理すると反映します。Docling の番号の復元と、現在の章節規則に基づく要約の生成には抽出からの再処理が必要です。

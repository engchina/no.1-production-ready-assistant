# 検索・回答プロファイルへの更新（#860）

更新日: 2026-10-03（JST）

## 名称と責務

3 製品の先頭ナビは「AI 活用」とする。RAG ではチャットと RAG 検索の入口であり、管理対象とは区別する。

「検索・回答プロファイル（Search Answer Profile）」は、参照する KB、検索・回答の設定、安全チェック、専用の知識（ドメインキーワード、承認済み FAQ、用語・同義語、回答ルール、確認の質問）をまとめる。管理画面は「ナレッジ構築」に置き、チャットと RAG 検索が選択して利用する。KB は文書 membership、加工の設定は文書の処理レシピが持つ。回答の役割・口調を表す persona は持たない。

## 一括更新の手順

この更新は API / MCP の破壊的変更を含む。RAG と呼び出す Agent、外部の連携先を同じ保守時間帯で更新する。

1. RAG / Agent の新規実行・取込を停止し、実行中の処理を完了またはキャンセルする。
2. Oracle の対象 schema と Agent の永続化状態をバックアップする。権限・保存回答・会話・プロファイル ID と件数を控える。
3. RAG と Agent のコードを同じ suite の版へ更新する。
4. RAG の「運用設定 → システムテーブル」で未適用の `20261003_001_search_answer_profiles` を確認する。既存の承認欄を使って「作成・更新」を実行する。CLI では `system_schema_cli initialize --allow-destructive` を使う。**全再作成はデータを削除するため、この更新には使わない。**
5. schema が `ready`、新 migration の checksum が適用済みであること、ID・行数・権限・保存回答・会話が保持されていることを確認する。
6. Agent と外部クライアントを新しい MCP 契約に合わせる。ブラウザを再読み込みし、対象選択、検索、会話、権限を確認して受付を再開する。

Oracle の DDL は auto-commit を伴う。旧名だけが存在する object を改名し、新名だけなら済んだ操作として再開する。新旧両方が存在する場合は `ORA-20060` で停止し、行を統合・上書き・削除しない。衝突した object の由来を確認してから再実行する。途中で止まった DB に未適用の旧 migration もある場合は `PROFILE_RENAME_INCOMPLETE` で停止する。バックアップと ledger を確認して復旧し、旧版のアプリを改名途中の schema へ接続しない。

## 保存値の対応

| 保存先 | 更新する構造 | 保持する値 |
|---|---|---|
| Oracle の 3 表・7 表の列、名前付き索引・制約 | `RAG_SEARCH_ANSWER_PROFILES`、`RAG_SEARCH_ANSWER_PROFILE_KNOWLEDGE`、`RAG_ROLE_SEARCH_ANSWER_PROFILES`、`SEARCH_ANSWER_PROFILE_ID`、`PROFILE_CONFIG` | ID、参照関係、作成時刻、設定 JSON、知識 payload、会話、フィードバック |
| `RAG_ROLE_PERMISSIONS` | `menu.search_answer_profiles` / `rag.search_answer_profiles.manage` へ grant を写し、旧 code の行を除去 | 実効権限。新旧重複は grant を重複作成しない |
| `RAG_ANSWER_RECORDS.DIAGNOSTICS_JSON` | root の `business_view_applied` を `search_answer_profile_applied` へ改名 | ID と他の診断値。新旧 key の併存は停止 |
| RAG の sessionStorage | 選択 ID、一覧状態、対象別 draft の key | raw record、保存時刻、TTL、利用者境界、自由本文。現行 key があれば優先 |
| RAG の localStorage | ナビの `nav.section.rag` を `nav.section.use` へ移す | 開閉状態、テーマ、サイドバー状態 |
| Agent の保存 Skill | MCP 要件の一覧ツール名 | Skill ID、自由な instructions、他の MCP 要件 |
| Agent の tool policy | allow / ask / deny の一覧ツール名 | default mode と判定。接続名の記号置換・64 文字のハッシュも対応 |
| Agent の Run / SDK checkpoint | RAG の構造化 tool call 名と arguments | run / call / trace ID、選択対象、承認、ユーザー本文と他製品の call |

評価の request / result、処理 job、知識 payload は、このエンティティの旧 field を schema として持たないため本文を一括置換しない。文書、質問、回答、プロファイル名、カスタム説明、SDK の message、イベントの履歴と自由本文も置換しない。外部の独自 JSON は自動更新の対象外なので、その連携先で schema を確認する。

過去 54 件の migration SQL は `app/rag/schema_migrations/pre_profile_rename.json` に原文で保存する。名前と checksum を変えず、改名後の補修で必要な過去 DDL だけ実行対象の object 名に読み替える。Agent が以前使っていた退役表 `AGENT_ROLE_BUSINESS_VIEWS` の削除 migration も原文を保つ。これらの旧名は移行境界に限定する。

## 公開契約

- 画面: `/search-answer-profiles`
- REST: `/api/search-answer-profiles`、関連 request / response の `search_answer_profile_id`、ロール範囲の `search_answer_profile_ids` / `allowed_search_answer_profile_ids`
- MCP: `rag_list_search_answer_profiles`、`rag_search` の `search_answer_profile_id`
- 契約の正本: `platform/contracts/mcp/rag-tools.json`

旧 route / tool は提供しない。REST の query / JSON に旧 scope の field があれば 422、MCP の未知の field は引数エラーとして拒否し、読み捨てて対象を拡大しない。SQL の VIEW や NL2SQL の既存 permission alias は変更しない。

JSON の改名は Oracle の [JSON_TRANSFORM RENAME](https://docs.oracle.com/en/database/oracle/oracle-database/26/adjsn/json_transform-operator-rename.html) を使い、root の指定した field に限定する。実 Oracle への更新はこの変更作業では実行していない。既存環境への適用とバックアップの復旧確認は、上の手順で行う。

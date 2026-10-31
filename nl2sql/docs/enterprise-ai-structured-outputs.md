# Enterprise AI の Responses Structured Outputs（Issue #431）

## 通信契約

LLM/VLM の接続先は既存の OCI Enterprise AI を維持し、Responses 互換 endpoint に `text.format = {type: "json_schema", name, schema, strict: true}` を送る。OpenAI のサービスや Chat Completions API への切替ではない。

[OpenAI Responses Structured Outputs 仕様](https://developers.openai.com/api/docs/guides/structured-outputs) に従い、Pydantic の domain 契約から送信用 Schema を作成する。すべての object を `additionalProperties:false` とし、全 property を required にする。nullable 型は null union を維持し、送信 Schema の default は除去する。domain の既定値や外部 API は変更しない。

共通実装は `backend/app/features/nl2sql/structured_outputs.py`。payload template に format の token がなくても `text.format` を設定する。返された JSON 全文を `jsonschema` で検証し、その後各機能の Pydantic・業務・SQL 安全検証へ渡す。JSON 部分の切出し、fence 除去、純粋な自然言語への暗黙 downgrade はしない。JSON の `text` field を client が途中で取り出して object を壊すことも防止する。

## 適用範囲

| 機能 | wire Schema / field |
|---|---|
| SQL → 物理構造、業務論理構造 | `StructureOutput.logical_structure`（Markdown 本文） |
| 業務論理構造 → 自然言語の質問 | `QuestionOutput.question` |
| 論理構造 / 質問 → SQL、Enterprise AI Direct SQL 生成 | `SqlOutput.sql`, `explanation` |
| SQL 詳細解析 | `_SqlAnalysisLlmPayload` |
| COMMENT 候補 | `CommentsOutput.suggestions` |
| COMMENT / ANNOTATIONS SQL 生成 | `SqlOutput` |
| Oracle エラー診断 | `AnalysisOutput.analysis` |
| VIEW の構造・JOIN / WHERE 解析 | `StructureOutput.logical_structure` |
| Ontology の schema naming / text extraction / gleaning | `OntologyBuildExtraction` |
| 質問意図解釈 | `QuestionIntentGraph` の wire Schema |
| SQL 品質評価の LLM judge | `QualityEvaluationJudge` |
| 汎用テキスト応答、接続確認、画像 OCR/VLM | `TextOutput.text`。呼出元には従来どおり文字列を返す |

質問意図の `filters.value` は任意の JSON 型を持つため、wire では `value_json:string` に JSON エンコードし、検証後に元の型へ戻す。`"001"` と `1`、null、配列、object を区別する。外部 API と保存データは従来の `value` のまま。`created_at` はサーバーが生成するため LLM Schema には含めない。

Oracle Select AI は DB 内の生成機構、決定論的 SQL 解析・fallback は非 LLM 処理であり、この HTTP 契約の対象外。OCI Generative AI の embedding/rerank も Responses に置き換えない。別 provider・ベクトル DB は追加しない。

## 失敗・retry

`refusal` と `incomplete` 等の応答状態を JSON Pointer で本文を選ぶ前に検査し、正常な結果として表示しない。これらは同条件の retry で解決を期待できないため自動再試行しない。空応答・Schema 違反は `response_format` の再試行可能エラーになる。

SQL→質問の既存段階 retry は維持する。失敗した段階だけ最大2回再試行し、HTTP retry との二重化を避け、全段階の時間予算を共有する。後段失敗時は完成した構造を保持して警告する。他の機能も既存の retry / 決定論的 fallback を維持し、構造化出力非対応の設定を純粋テキストで成功扱いしない。Schema は形式の契約であり、SQL の意味・権限・安全性を保証するものではない。

## 検証

- `test_structured_outputs.py`: recursive required/closed/nullable、余分な field、型不正、全文検証、`text` 保持、reasoning 除外、refusal/incomplete、template、画像 payload、filter 値の型保持。
- SQL 往復・質問・metadata・Ontology・judge・settings の既存回帰テストを構造化 wire に更新。
- 2026-09-11 の既存 OCI 接続で架空のスキーマのみを使い、Question / SQL / Ontology extraction / QuestionIntent の4 Schema が正常応答・ローカル検証成功（各約4–7秒）。実データや credential を記録していない。
- in-memory の架空スキーマで3段階の SQL→構造→質問が約15秒、`source=oci_enterprise_ai`、警告0件。構造からの SQL 再生成（約6秒）、質問からの専用 SQL 生成（約4秒）、自作の `TEST 123` 画像の VLM 抽出（約2秒）も成功。DB 実行や任意 SQL の意味同値性検証は行っていない。

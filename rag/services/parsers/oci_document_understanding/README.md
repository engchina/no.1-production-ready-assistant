# parser-oci-document-understanding

OCI Document Understanding を呼ぶ parser マイクロサービス(OCI クラウド・薄いプロキシ)。

> **準備中（ロードマップ。#3374）。** 実装は今後の版のために残していますが、まだ使えません。文書解析の設定・処理レシピでは選べず、サービス管理では起動できず、Terraform の stack・`rag/init_script.sh`・`rag/scripts/rag-services.sh` は配備しません（開発で動きを確かめるときだけ `scripts/rag-services.sh run parser-oci-document-understanding` で前面に起動できます）。

- 共有 core `rag_parser_core.oci_document_understanding` を **env 由来 config** で駆動する。
- `POST /parse`(file + content_type)→ OCI Document Understanding の非同期 processor job
  で OCR/表抽出 → `StructuredExtraction`(ParseResponse)で返す。
- `GET /health` は OCI 設定の充足(compartment / namespace / 入力 bucket)で ok/degraded。
- **OCI 認証はメインプロジェクト設定を継承**: systemd の unit が共通 `platform/.env`・`backend/.env` を EnvironmentFile で読み、
  OCI の設定ファイルは実行ユーザーの `~/.oci/config` を使う(#286。個別設定なし)。Terraform の stack では
  `rag_enable_oci_cloud_parsers` で配備する。
- 未設定/SDK 失敗/job 失敗/timeout 時は extraction=None を返し、backend 側で既存
  in-process / ローカルフローへ安全に縮退する。
- job の入力（`<入力 prefix>/<document_id>.<拡張子>`。原本の写し）と出力（`<出力 prefix>/<job_id>/`）は、
  結果を読んだ後（失敗・timeout でも）に消す（#2440）。bucket は private（public access なし）でよいが、
  OCI 認証のユーザーに bucket の object の読み書きと削除の権限が要り、DU のサービスが bucket を読み書き
  できる IAM の policy も要る。

## 環境変数(backend と同じキーを継承)

`RAG_OCI_DOCUMENT_UNDERSTANDING_COMPARTMENT_ID` / `PLATFORM_OCI_COMPARTMENT_ID`、
`RAG_OCI_DOCUMENT_UNDERSTANDING_NAMESPACE` / `PLATFORM_OBJECT_STORAGE_NAMESPACE`、
`RAG_OCI_DOCUMENT_UNDERSTANDING_INPUT_BUCKET` / `PLATFORM_OBJECT_STORAGE_BUCKET`、
`RAG_OCI_DOCUMENT_UNDERSTANDING_OUTPUT_BUCKET` / `_INPUT_PREFIX` / `_OUTPUT_PREFIX` /
`_LANGUAGE` / `_FEATURES` / `_POLL_INTERVAL_SECONDS` / `_TIMEOUT_SECONDS`、
`PLATFORM_OCI_CONFIG_FILE` / `PLATFORM_OCI_CONFIG_PROFILE` / `PLATFORM_OCI_REGION` / `PLATFORM_OBJECT_STORAGE_REGION`。

## ローカル実行

```bash
uv run --directory services/parsers/oci_document_understanding \
  uvicorn app.main:app --port 18028
```

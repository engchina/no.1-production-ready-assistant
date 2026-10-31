# parser-docling

Docling を独立したプロセス(サービスごとの uv の venv)で動かす parser マイクロサービス。

- 出力契約: `rag_parser_core` の `StructuredExtraction`(`POST /parse`)
- readiness: `GET /health`(導入 version を返す)
- docling のバージョンは本サービス単独で upgrade 可能(他 parser / backend に非干渉)

## ローカル実行(開発)

```bash
# repo root から(共有 package の path source を解決するため)
uv run --directory services/parsers/docling \
  uvicorn app.main:app --host 127.0.0.1 --port 18020
```

## 起動(uv の venv + systemd。#286)

サービスごとの uv の venv(`uv sync --locked --no-dev --python 3.12`)で動き、本番・開発とも systemd の unit
(`production-ready-rag-parser-docling.service`)で起動 / 停止する。Docker は使わない(Dockerfile は #356 で削除した)。

```bash
# rag/ で実行する
scripts/rag-services.sh install parser-docling   # venv を作り unit と sudoers を登録して起動
scripts/rag-services.sh run parser-docling       # systemd の無い環境で前面に起動
```

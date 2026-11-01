# RAG のディレクトリ構成

> rag/AGENTS.md から移した詳細（#2276）。rag/AGENTS.md の規則と同じ強さで守る。規則を変えるときはこのファイルを直し、AGENTS.md には規則の要約と案内だけを書く。

## ディレクトリ構成

```text
backend/                  FastAPI アプリ
  app/
    main.py               エントリ(CORS, ルーター, lifespan)
    config.py             設定(pydantic-settings)
    api/routes/           health / documents / search / knowledge_bases /
                          search_answer_profiles / evaluation / settings / services
    clients/              モデルの API（enterprise_ai / genai）/ Oracle / ファイル保存先 / parser の clients
    rag/                  ingestion / parsing / chunking / 回答フロー(answer_engine) /
                          guardrail / evaluation / search answer profile
    schemas/              common / search / knowledge_base / search_answer_profile / settings
  tests/                  pytest
frontend/                 Vite + React Router + TypeScript
  src/App.tsx             React Router ルート定義
  src/components/         layout / search / knowledge-bases / search-answer-profiles / settings
  src/lib/                api / queries / routes / i18n / utils
services/                 parser / preprocess / pipeline(graphrag / vector_index / guardrail / evaluation)などのローカル実行単位
```

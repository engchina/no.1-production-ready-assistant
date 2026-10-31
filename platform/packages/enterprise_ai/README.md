# production-ready-enterprise-ai (`pr_enterprise_ai`)

OCI Enterprise AI（OpenAI 互換の Responses API）を呼ぶときの共通の規則（#3418）。標準 library だけで書き、FastAPI・HTTP の client・DB に依存しない。

| モジュール | 内容 |
|---|---|
| `urls` | 呼ぶ URL の決め方（`enterprise_ai_base_url`・`enterprise_ai_request_url`。`{Endpoint URL}/responses`、path は補わない。#1440） |
| `errors` | HTTP の失敗（401 / 403・404 / 429 / 5xx / モデルが無い）と、応答が使えない失敗（時間切れ・接続・出力の上限で途中で終わる・形式・拒否）の分類、確かめる所の案内、応答の本文の秘密を伏せた抜粋（#1684・#1715） |

- 使う側: 3 製品の共通のシステム設定（`pr_system_settings`。`pr_system_settings.enterprise_ai_errors` と `pr_system_settings.model` は同じ名前をここから出す）と、製品横断の評価（`suite/evaluation` の LLM による判定）。
- 依存の向き: この package は製品・suite・ほかの platform の package を import しない。

## テスト

```bash
uv sync --dev
uv run pytest -n auto
uv run ruff format --check . && uv run ruff check . && uv run mypy src
```

分類と案内の詳しい振る舞いは `pr_system_settings` の `tests/test_enterprise_ai_errors.py`・`tests/test_model_endpoint_url.py` が、同じ名前を通して確かめる。

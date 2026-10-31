"""OCI Enterprise AI の呼び方の共通の規則（標準 library だけ。#3418）。

- `urls`: 呼ぶ URL の決め方（`{Endpoint URL}/responses`。path は補わない。#1440）。
- `errors`: HTTP の失敗・応答が使えない失敗の分類と、確かめる所の案内（#1684・#1715）。

3 製品の共通のシステム設定（`pr_system_settings`）と、製品横断の評価（`suite/evaluation` の LLM に
よる判定）が使う。FastAPI・HTTP の client・DB に依存しない。
"""

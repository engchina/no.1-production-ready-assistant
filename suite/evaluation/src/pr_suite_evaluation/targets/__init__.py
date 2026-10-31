"""Target の adapter（製品の公開の契約を呼び、観測を返す）。

- `rag_mcp`: RAG の検索・回答を公開の RAG MCP（`rag_search`）で呼ぶ（#3417）。
- `agent_mcp`: 業務 Agent を公開の Agent MCP で呼ぶ。
- `agent_legacy`: Agent の旧形式の回答の扱い（回答の節・outcome の無い Run の推定）。
"""

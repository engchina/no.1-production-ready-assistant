# Agent の主要ディレクトリ

> agent/AGENTS.md から移した詳細（#2276）。agent/AGENTS.md の規則と同じ強さで守る。規則を変えるときはこのファイルを直し、AGENTS.md には規則の要約と案内だけを書く。

## 主要ディレクトリ

```text
backend/app/features/agent/
  builtin_runtime.py        組み込み Runtime（OpenAI Agents SDK + モデルの API）
  runtime_dispatcher.py     claim/lease dispatcher（本番の別プロセス）
  runtime.py                共通 Run/Event/Artifact/Audit と legacy history
  skills.py                 Skill registry と組み込みのスキル（repo 直下の skills/ を読む。#1816）
  skills_loader.py          SKILL.md と references/ の読み込み
  router.py                 REST / SSE / WS
backend/app/security/       共通認証の上の Agent の権限・対象範囲・権限管理 API
backend/app/system_schema.py               システムテーブル（AGENT_* の DDL・migration・状態）
backend/app/cli/agent_system_schema.py     システムテーブルの status / initialize / recreate の CLI
backend/app/cli/agent_test_resources.py    合成資料の業務 Agent を管理 API で作成・公開する CLI（定義は examples/test-pdfs-ja/agents.json。#3545）
frontend/src/
  App.tsx                   route（画面は React.lazy で route ごとに読む）
  pages/<領域>/<画面>.tsx    agents / runs / approvals / audit / skills / settings など
  pages/shared/             2 画面以上で使う小さな部品・helper
  lib/api.ts, lib/i18n.ts, lib/routes.ts
docs/agent-control-plane-design.md
```

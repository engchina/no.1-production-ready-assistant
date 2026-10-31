# frontend — production-ready RAG UI

Vite + React Router + TypeScript + Tailwind v4 + shadcn/ui + TanStack Query + Zustand。

UI/UX 構造は本リポジトリの AGENTS.md と `frontend/src` を正本として管理する。

## セットアップ

```bash
npm ci
BACKEND_URL=http://localhost:8000 npm run dev   # http://localhost:3000
```

`/api/*` は **`BACKEND_URL` を明示したときだけ** その URL へプロキシされる（既定の接続先は持たない）。
`BACKEND_URL` を渡さずに `npm run dev` すると hermetic モードになり、`/api/*` は proxy されず 404 を返す
（起動時に警告を 1 行表示する）。Vite の config は `.env.local` を読まないため、`BACKEND_URL` はシェルの環境変数で渡す。
`scripts/start-all.sh` / `scripts/start-frontend.sh` は `BACKEND_URL` を明示して起動する。
サーバ状態は TanStack Query、UI 永続状態（例: サイドバー折りたたみ）は Zustand store で管理する。

## 開発コマンド

```bash
npm run lint
npm run typecheck
npm run build
```

## 本番配信

本番（`rag/init_script.sh`）は `package-lock.json` を前提に `npm ci` で再現可能に依存解決して build し、
host の Nginx が Vite の `dist/` を配信して `/api/*` を backend へリバースプロキシする（Docker は使わない。#286 / #356）。

## 構成

```
src/
  main.tsx             Vite エントリ
  App.tsx              React Router ルート定義
  globals.css          Tailwind v4 / shadcn/ui theme tokens
  components/
    layout/Sidebar     サイドナビ
    StatusBadge        ファイル状態バッジ
  lib/                 api / queries / Zustand ui-store / routes / i18n(ja) / utils
```

> 画面の設計・実装は AGENTS.md の指示に従い `ui-ux-pro-max` skill を使う。

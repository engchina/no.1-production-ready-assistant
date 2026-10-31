import { StrictMode, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider, createBrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ConfirmProvider, Toaster } from "@production-ready/ui";
import { TraceDetailsLabelsProvider } from "@production-ready/ui";
import { loadStylesAfterFirstRender } from "@production-ready/ui";
import {
  UnsavedChangesBlocker,
  useConfirmNavigationKey,
} from "@production-ready/system-settings";

import { App } from "./App";
import { ROUTER_BASENAME } from "@/lib/base-path";
import { installBrowserErrorGuards } from "@/lib/browser-error-guards";
import { t } from "@/lib/i18n";
import { AuthProvider } from "@/features/security/AuthProvider";
import { NL2SQL_TRACE_DETAILS_LABELS } from "@/features/nl2sql/chatProgress";
// フォント実体をビルドへ同梱し、Google Fonts / CDN に依存せず同一 origin から配信する。
// Noto Sans JP は可変書体（1 つの face が太さ 100〜900 を持つ。書体名は Noto Sans JP Variable）。@font-face を slice の数に
// 抑える（静的な書体の slice × 太さの @font-face は描画を止めた。#3328・#3333）。
import "@fontsource-variable/noto-sans-jp/wght.css";
// Noto Sans SC / TC / HK は主の CSS に入れず、fonts-cjk.css を最初の描画の後に読む（末尾の loadStylesAfterFirstRender。#3305）。
import "@fontsource/roboto/400.css";
import "@fontsource/roboto/500.css";
import "@fontsource/roboto/600.css";
import "@fontsource/roboto/700.css";
import "@fontsource/google-sans-code/400.css";
import "@fontsource/google-sans-code/500.css";
import "@fontsource/google-sans-code/600.css";
import "@fontsource/google-sans-code/700.css";
// globals.css が tailwindcss + 共有 tokens.css + @source を取り込む（単一エントリ）。
import "./globals.css";

// 永続化テーマを描画前に適用（FOUC 回避）＋ store/OS 変更を購読。
installBrowserErrorGuards();

const root = document.getElementById("root");
if (!root) {
  throw new Error("root element が見つかりません。");
}

const queryClient = new QueryClient();

/** 共有 ConfirmProvider に NL2SQL の文言を注入し、ルート遷移で開いている確認をキャンセルする。 */
function AppConfirmProvider({ children }: { children: ReactNode }) {
  const navigationKey = useConfirmNavigationKey();
  return (
    <ConfirmProvider
      labels={{ confirm: t("common.confirm"), cancel: t("common.cancel") }}
      navigationKey={navigationKey}
    >
      {children}
    </ConfirmProvider>
  );
}

/** 既存のルート定義（App の <Routes>）はそのまま使い、全体を data router の 1 つの splat route に載せる。 */
function RootLayout() {
  return (
    <AuthProvider>
      <AppConfirmProvider>
        {/* ブラウザの戻る/進むの blocker はアプリで 1 つ。各画面のガードはここへ未保存を登録する（#586）。 */}
        <UnsavedChangesBlocker />
        {/* 「トレースの詳細」の文言と段の名前は全画面で同じ（#3219）。 */}
        <TraceDetailsLabelsProvider labels={NL2SQL_TRACE_DETAILS_LABELS}>
          <App />
        </TraceDetailsLabelsProvider>
        <Toaster
          dismissLabel={t("common.dismiss")}
          regionLabel={t("common.notifications")}
        />
      </AppConfirmProvider>
    </AuthProvider>
  );
}

// data router にすると、共有の離脱ガードがブラウザの戻る/進むも確認できる（useBlocker。#138）。
// basename は配信の前置き（`/nl2sql/` で配信するときは `/nl2sql`。`/` なら undefined で今までどおり。#1316）。
const router = createBrowserRouter([{ path: "*", element: <RootLayout /> }], {
  basename: ROUTER_BASENAME,
});

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>
);

// Noto Sans JP に無い字を補う Noto Sans SC・TC・HK の @font-face は別の CSS の chunk にし、最初の描画の後に読む（#3305）。
loadStylesAfterFirstRender(root, () => import("./fonts-cjk.css"), { serviceName: "production-ready-nl2sql" });

import { StrictMode, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider, createBrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import {
  ConfirmProvider,
  Toaster,
  TraceDetailsLabelsProvider,
  createTraceDetailsLabels,
  initTheme,
  loadStylesAfterFirstRender,
} from "@production-ready/ui";
import {
  UnsavedChangesBlocker,
  useConfirmNavigationKey,
} from "@production-ready/system-settings";

import { App } from "./App";
import { AuthProvider } from "@/components/security/AuthProvider";
import { ROUTER_BASENAME } from "@/lib/base-path";
// globals.css が tailwindcss + 共有 tokens.css + @source を取り込む（単一エントリ）。
import "./globals.css";
// 書体は @fontsource の woff2 を同一 origin で配信する（外部 CDN に依存しない）。
import "./fonts.css";
import { t } from "@/lib/i18n";
import { useUiStore } from "@/lib/ui-store";

// 永続化テーマを描画前に適用（FOUC 回避）＋ store / OS 設定の変更を購読する（#95）。
initTheme(useUiStore);

const root = document.getElementById("root");
if (!root) {
  throw new Error("root element が見つかりません。");
}

const queryClient = new QueryClient();

const TRACE_DETAILS_LABELS = createTraceDetailsLabels(t, { product: "agent" });

/**
 * 共有 ConfirmProvider に Agent の文言を渡し、ルートが変わったら開いている確認をキャンセルする
 * （messaging.md §3.5 の `navigationKey`。NL2SQL と同じ形。#802）。
 */
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

// 既存のルート定義（App の <Routes>）はそのまま、全体を data router の 1 つの splat route に載せる。
// data router にすると、共有の離脱ガードがブラウザの戻る/進むも確認できる（useBlocker。#138）。
const router = createBrowserRouter([
  {
    path: "*",
    element: (
      <AppConfirmProvider>
        {/* ブラウザの戻る/進むの blocker はアプリで 1 つ。各画面のガードはここへ未保存を登録する（#586）。 */}
        <UnsavedChangesBlocker />
        {/* 「トレースの詳細」の文言は全画面で同じ（#3219）。Agent の span は段（`pr.stage`）を持たないので、共通の名前だけを使う。 */}
        <TraceDetailsLabelsProvider labels={TRACE_DETAILS_LABELS}>
          <App />
        </TraceDetailsLabelsProvider>
        <Toaster dismissLabel={t("common.dismiss")} regionLabel={t("common.notifications")} />
      </AppConfirmProvider>
    ),
  },
  // 配信の基点（`/agent/` で配備したとき。#1316）。ローカルの開発・e2e（`/`）では undefined（今までどおり）。
], { basename: ROUTER_BASENAME });

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      {/* 共通の認証（Cookie セッション。ローカルはログインなしの全権限の利用者。#215）。 */}
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>
  </StrictMode>
);

// Noto Sans JP に無い字を補う Noto Sans SC・TC・HK の @font-face は別の CSS の chunk にし、最初の描画の後に読む（#3305）。
loadStylesAfterFirstRender(root, () => import("./fonts-cjk.css"), { serviceName: "production-ready-agent" });

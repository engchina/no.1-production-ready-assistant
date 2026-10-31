import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider, createBrowserRouter } from "react-router-dom";
import { initTheme, loadStylesAfterFirstRender } from "@production-ready/ui";
import { UnsavedChangesBlocker } from "@production-ready/system-settings";

import { App } from "./App";
import { Providers } from "@/components/providers";
import { ROUTER_BASENAME } from "@/lib/base-path";
import "@/fonts.css";
import "@/globals.css";
import { useUiStore } from "@/lib/ui-store";

// 永続化テーマを描画前に適用（FOUC 回避）＋ store / OS 設定の変更を購読する（#95）。
initTheme(useUiStore);

const root = document.getElementById("root");
if (!root) {
  throw new Error("root element が見つかりません。");
}

// 既存のルート定義（App の <Routes>）はそのまま、全体を data router の 1 つの splat route に載せる。
// data router にすると、共有の離脱ガードがブラウザの戻る/進むも確認できる（useBlocker。#138）。
// blocker はアプリで 1 つ（UnsavedChangesBlocker）にし、各画面のガードはそこへ未保存を登録する（#586）。
// `/rag/` の下で配信する build では basename を付ける（#1316）。`/` の build は undefined で今までどおり。
const router = createBrowserRouter(
  [
    {
      path: "*",
      element: (
        <Providers>
          <UnsavedChangesBlocker />
          <App />
        </Providers>
      ),
    },
  ],
  { basename: ROUTER_BASENAME }
);

createRoot(root).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>
);

// Noto Sans JP に無い字を補う Noto Sans SC・TC・HK の @font-face は別の CSS の chunk にし、最初の描画の後に読む（#3305）。
loadStylesAfterFirstRender(root, () => import("@/fonts-cjk.css"), { serviceName: "production-ready-rag" });

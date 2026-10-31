// @vitest-environment happy-dom
import { ConfirmProvider, useConfirm } from "@production-ready/ui";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { createMemoryRouter, Outlet, RouterProvider, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { useConfirmNavigationKey } from "../src";

// #833: 画面が作業状態を URL に書き戻すだけの置き換え（同じパスの REPLACE）では、開いている確認を閉じない。
// 戻る / 進む・リンクの遷移・パスの変わる置き換えでは、従来どおりキャンセルする（messaging.md §3.5）。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;
let confirm: ReturnType<typeof useConfirm>;
let navigate: ReturnType<typeof useNavigate>;

function Layout() {
  return (
    <ConfirmProvider navigationKey={useConfirmNavigationKey()}>
      <Probe />
      <Outlet />
    </ConfirmProvider>
  );
}

function Probe() {
  confirm = useConfirm();
  navigate = useNavigate();
  return null;
}

function renderRouter() {
  const router = createMemoryRouter(
    [
      {
        element: <Layout />,
        children: [
          { path: "/documents/:id", element: null },
          { path: "/documents", element: null },
        ],
      },
    ],
    { initialEntries: ["/documents", "/documents/doc-1"], initialIndex: 1 }
  );
  act(() => root.render(<RouterProvider router={router} />));
  return router;
}

function openConfirm() {
  let settled: boolean | null = null;
  act(() => {
    void confirm({ title: "ファイル準備を再実行しますか?" }).then((value) => {
      settled = value;
    });
  });
  return () => settled;
}

const dialog = () => document.querySelector('[role="alertdialog"]');

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe("useConfirmNavigationKey", () => {
  it("同じパスで検索条件だけを置き換えても、開いている確認は閉じない", async () => {
    renderRouter();
    const result = openConfirm();
    expect(dialog()).not.toBeNull();

    await act(() => navigate("/documents/doc-1?recipe=recipe-1", { replace: true }));

    expect(dialog()).not.toBeNull();
    expect(result()).toBeNull();
  });

  it("パスが変わる置き換えでは、開いている確認をキャンセルする", async () => {
    renderRouter();
    const result = openConfirm();

    await act(() => navigate("/documents/doc-2", { replace: true }));

    expect(dialog()).toBeNull();
    expect(result()).toBe(false);
  });

  it("戻る（POP）では、同じパスへの移動でも開いている確認をキャンセルする", async () => {
    const router = renderRouter();
    await act(() => navigate("/documents/doc-1?recipe=recipe-2"));
    const result = openConfirm();

    await act(() => router.navigate(-1));

    expect(router.state.location.search).toBe("");
    expect(dialog()).toBeNull();
    expect(result()).toBe(false);
  });

  it("置き換えの後の遷移（PUSH）でも、開いている確認をキャンセルする", async () => {
    renderRouter();
    await act(() => navigate("/documents/doc-1?recipe=recipe-1", { replace: true }));
    const result = openConfirm();

    await act(() => navigate("/documents/doc-1?recipe=recipe-2"));

    expect(dialog()).toBeNull();
    expect(result()).toBe(false);
  });
});

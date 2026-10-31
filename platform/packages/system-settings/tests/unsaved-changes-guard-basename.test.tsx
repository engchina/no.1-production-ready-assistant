// @vitest-environment happy-dom
// @vitest-environment-options {"url": "http://localhost/rag/editor"}
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { createMemoryRouter, Link, RouterProvider } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { UnsavedChangesBlocker, useUnsavedChangesGuard } from "../src/guards/useUnsavedChangesGuard";

// #1316: 製品を /rag/ などの prefix（router の basename）で配信すると、<a> の href は basename を含む。
// 未保存の確認の後に navigate() へそのまま渡すと /rag/rag/list に移動するため、basename を外して渡す。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;

function Editor() {
  useUnsavedChangesGuard(true, async () => true);
  return <Link to="/list">一覧へ</Link>;
}

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe("useUnsavedChangesGuard と router の basename", () => {
  it("確認の後、basename を二重に付けずに移動する", async () => {
    const router = createMemoryRouter(
      [
        {
          path: "*",
          element: (
            <>
              <UnsavedChangesBlocker />
              <Editor />
            </>
          ),
        },
      ],
      { basename: "/rag", initialEntries: ["/rag/editor"] }
    );
    act(() => root.render(<RouterProvider router={router} />));
    const link = host.querySelector("a");
    expect(link?.getAttribute("href")).toBe("/rag/list");

    await act(async () => {
      link?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }));
      await Promise.resolve();
      await Promise.resolve();
    });

    // data router の location は basename を含む（/rag/rag/list にならない）。
    expect(router.state.location.pathname).toBe("/rag/list");
  });
});

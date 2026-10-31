// @vitest-environment happy-dom
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  confirmUnsavedChanges,
  UnsavedChangesBlocker,
  useUnsavedChangesGuard,
} from "../src/guards/useUnsavedChangesGuard";

// #586: 1 画面に離脱の確認が複数あると、各 hook が useBlocker を呼び、React Router は最後に登録した
// blocker だけで判定していた（先に登録したフォームが未保存でも、ブラウザの戻るで確認が出なかった）。
// blocker はアプリで 1 つ（UnsavedChangesBlocker）にし、未保存のガードを集めて判定する。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;

function Form({ dirty, confirm }: { dirty: boolean; confirm: () => Promise<boolean> }) {
  useUnsavedChangesGuard(dirty, confirm);
  return null;
}

function Editor({
  first,
  second,
}: {
  first: { dirty: boolean; confirm: () => Promise<boolean> };
  second: { dirty: boolean; confirm: () => Promise<boolean> };
}) {
  return (
    <>
      <Form {...first} />
      <Form {...second} />
    </>
  );
}

function renderRouter(element: ReactNode) {
  const router = createMemoryRouter(
    [
      {
        path: "*",
        element: (
          <>
            <UnsavedChangesBlocker />
            {element}
          </>
        ),
      },
    ],
    { initialEntries: ["/list", "/editor"], initialIndex: 1 }
  );
  act(() => root.render(<RouterProvider router={router} />));
  return router;
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.restoreAllMocks();
});

describe("UnsavedChangesBlocker", () => {
  it("先に登録したフォームだけが未保存でも、戻るで確認し、キャンセルで留まる", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const firstConfirm = vi.fn(async () => false);
    const secondConfirm = vi.fn(async () => true);
    const router = renderRouter(
      <Editor
        first={{ dirty: true, confirm: firstConfirm }}
        second={{ dirty: false, confirm: secondConfirm }}
      />
    );

    await act(async () => {
      await router.navigate(-1);
    });
    await flush();

    expect(firstConfirm).toHaveBeenCalledTimes(1);
    expect(secondConfirm).not.toHaveBeenCalled();
    expect(router.state.location.pathname).toBe("/editor");
    expect(warn.mock.calls.flat().join("\n")).not.toContain("only supports one blocker");
  });

  it("確定すると戻る。未保存が無ければ確認せずに戻る", async () => {
    const confirm = vi.fn(async () => true);
    const router = renderRouter(
      <Editor first={{ dirty: false, confirm }} second={{ dirty: true, confirm }} />
    );
    await act(async () => {
      await router.navigate(-1);
    });
    await flush();
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(router.state.location.pathname).toBe("/list");

    const clean = vi.fn(async () => true);
    const cleanRouter = renderRouter(
      <Editor first={{ dirty: false, confirm: clean }} second={{ dirty: false, confirm: clean }} />
    );
    await act(async () => {
      await cleanRouter.navigate(-1);
    });
    await flush();
    expect(clean).not.toHaveBeenCalled();
    expect(cleanRouter.state.location.pathname).toBe("/list");
  });

  it("confirmUnsavedChanges は未保存のガードの確認を 1 回だけ通し、無ければ true", async () => {
    const first = vi.fn(async () => false);
    const second = vi.fn(async () => true);
    renderRouter(<Editor first={{ dirty: true, confirm: first }} second={{ dirty: true, confirm: second }} />);
    await expect(confirmUnsavedChanges()).resolves.toBe(false);
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).not.toHaveBeenCalled();

    act(() => root.render(null));
    await expect(confirmUnsavedChanges()).resolves.toBe(true);
  });
});

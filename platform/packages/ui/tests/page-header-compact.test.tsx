// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PageHeader, type PageHeaderAction } from "../src";
import { contextualMenuLabel } from "../src/components/ui/floating-menu";

// #582: 狭い画面（lg 未満）の PageHeader の折りたたみと、「その他の操作」の読み上げ名。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const noop = () => undefined;
const back: PageHeaderAction = { id: "back", kind: "secondary", label: "一覧へ戻る", onClick: noop };
const reload: PageHeaderAction = { id: "reload", kind: "utility", label: "最新情報を取得", onClick: noop };
const save: PageHeaderAction = { id: "save", kind: "primary", label: "保存", onClick: noop };

describe("contextualMenuLabel", () => {
  it("見えている文言を先頭に置き、対象を括弧で足す（対象が無ければ文言だけ）", () => {
    expect(contextualMenuLabel("その他の操作", "ページ操作")).toBe("その他の操作（ページ操作）");
    expect(contextualMenuLabel("その他の操作", "  ")).toBe("その他の操作");
    expect(contextualMenuLabel("その他の操作")).toBe("その他の操作");
  });
});

describe("PageHeader（lg 未満）", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: true,
      media: query,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }));
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });

  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
  });

  const buttonTexts = () => Array.from(host.querySelectorAll("button")).map((button) => button.textContent);

  it("メニューに入るのが 1 つだけなら畳まず、「一覧へ戻る」を 1 タップで押せる", () => {
    act(() => root.render(<PageHeader title="検索・回答プロファイル" actions={[back, save]} />));
    expect(host.querySelector('[data-testid="page-actions-more"]')).toBeNull();
    expect(buttonTexts()).toEqual(["一覧へ戻る", "保存"]);
  });

  it("2 つ以上なら畳み、メニューのボタンの読み上げ名に対象（actionsLabel）を足す", () => {
    act(() =>
      root.render(<PageHeader title="検索・回答プロファイル" actions={[back, reload, save]} actionsLabel="検索・回答プロファイルの操作" />)
    );
    const trigger = host.querySelector('[data-testid="page-actions-more"]') as HTMLButtonElement;
    expect(trigger.textContent).toBe("その他の操作");
    expect(trigger.getAttribute("aria-label")).toBe("その他の操作（検索・回答プロファイルの操作）");
    expect(buttonTexts()).toEqual(["その他の操作", "保存"]);
  });

  it("actionsLabel を渡さなければ既定の「ページ操作」を足す", () => {
    act(() => root.render(<PageHeader title="検索・回答プロファイル" actions={[back, reload, save]} />));
    const trigger = host.querySelector('[data-testid="page-actions-more"]') as HTMLButtonElement;
    expect(trigger.getAttribute("aria-label")).toBe("その他の操作（ページ操作）");
  });
});

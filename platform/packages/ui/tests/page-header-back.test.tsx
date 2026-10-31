// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PageHeader, type PageHeaderAction } from "../src";

// #618: 詳細・作成・編集の画面の「一覧へ戻る」は左上、保存は右端の primary、破棄はその左。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const noop = () => undefined;
const discard: PageHeaderAction = { id: "discard", kind: "secondary", label: "変更を破棄", onClick: noop };
const save: PageHeaderAction = { id: "save", kind: "primary", label: "保存", onClick: noop };

describe("PageHeader の back（一覧へ戻る）", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: false,
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

  it("タイトルの上の左端に、矢印付きのボタンで出す（右の操作の列には入れない）", () => {
    const onBack = vi.fn();
    act(() =>
      root.render(
        <PageHeader
          title="経理ビュー"
          back={{ label: "一覧へ戻る", onClick: onBack, testId: "editor-back" }}
          actions={[save, discard]}
        />
      )
    );
    const backButton = host.querySelector<HTMLButtonElement>('[data-page-header-back]')!;
    expect(backButton.textContent).toBe("一覧へ戻る");
    expect(backButton.getAttribute("data-testid")).toBe("editor-back");
    expect(backButton.querySelector("svg")).not.toBeNull();
    // 見出しより前（Tab の順の先頭）、操作の列の外。
    const heading = host.querySelector("h1")!;
    expect(backButton.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(host.querySelector('[data-page-header-actions]')!.contains(backButton)).toBe(false);
    act(() => backButton.click());
    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it("操作の列は 破棄 → 保存 の順で、保存（primary）が右端", () => {
    act(() => root.render(<PageHeader title="経理ビュー" back={{ label: "一覧へ戻る", onClick: noop }} actions={[save, discard]} />));
    const labels = Array.from(host.querySelectorAll('[data-page-header-actions] button')).map((b) => b.textContent);
    expect(labels).toEqual(["変更を破棄", "保存"]);
  });

  it("読み上げ名に戻り先を足せる。disabled も渡せる", () => {
    act(() =>
      root.render(
        <PageHeader title="x" back={{ label: "一覧へ戻る", ariaLabel: "検索・回答プロファイルの一覧へ戻る", disabled: true, onClick: noop }} />
      )
    );
    const backButton = host.querySelector<HTMLButtonElement>('[data-page-header-back]')!;
    expect(backButton.getAttribute("aria-label")).toBe("検索・回答プロファイルの一覧へ戻る");
    expect(backButton.disabled).toBe(true);
  });
});

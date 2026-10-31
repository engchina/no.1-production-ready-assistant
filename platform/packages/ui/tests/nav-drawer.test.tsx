// @vitest-environment happy-dom
import { FileText, Settings } from "lucide-react";
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "../src/components/app-shell/AppShell";
import { closesNavDrawer, focusableIn, navDrawerKeyAction } from "../src/components/app-shell/nav-drawer";
import { Sidebar, SidebarAccountFooter } from "../src/components/app-shell/Sidebar";
import type { NavLinkComponent, NavSection } from "../src/navigation/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const sections: NavSection[] = [
  {
    key: "main",
    title: "業務",
    items: [
      { href: "/documents", label: "文書一覧", icon: FileText },
      { href: "/settings", label: "設定", icon: Settings },
    ],
  },
];

const Link: NavLinkComponent = ({ to, children, ...rest }) => (
  <a href={to} {...rest} onClick={(event) => event.preventDefault()}>
    {children}
  </a>
);

const labels = {
  aria: "サイドナビゲーション",
  expand: "サイドバーを展開",
  collapse: "サイドバーを折りたたむ",
  sectionContainsActive: "現在のページを含む",
  sectionToggleExpand: (section: string) => `${section}を展開`,
  sectionToggleCollapse: (section: string) => `${section}を折りたたむ`,
};

function Shell({ path = "/documents", collapsed = true }: { path?: string; collapsed?: boolean }) {
  return (
    <AppShell
      sidebar={
        <Sidebar
          sections={sections}
          currentPath={path}
          title={{ line1: "Production Ready", line2: "RAG", full: "Production Ready RAG" }}
          collapsed={collapsed}
          onToggleCollapsed={() => undefined}
          collapsedSections={{}}
          onSetSectionCollapsed={() => undefined}
          linkComponent={Link}
          labels={labels}
          footer={<SidebarAccountFooter name="山田" collapsed={collapsed} onLogout={() => undefined} />}
        />
      }
    >
      <h1>文書一覧</h1>
    </AppShell>
  );
}

let container: HTMLDivElement;
let root: Root;

function mockViewport(narrow: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: narrow,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
}

function render(node: ReactNode) {
  act(() => root.render(node));
}

const $ = <T extends Element = HTMLElement>(selector: string) => container.querySelector<T>(selector);
const trigger = () => $<HTMLButtonElement>('[data-testid="nav-drawer-trigger"]')!;
const drawer = () => $<HTMLDivElement>('[data-testid="nav-drawer"]')!;
const key = (target: Element, init: KeyboardEventInit) =>
  act(() => {
    target.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init }));
  });
const click = (target: Element) =>
  act(() => {
    (target as HTMLElement).click();
  });

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

describe("AppShell のナビのドロワー（md 未満、#367）", () => {
  beforeEach(() => mockViewport(true));

  it("閉じている間はサイドバーをドロワーの中に隠し、上端のバーに「メニュー」ボタンと製品名を出す", () => {
    render(<Shell />);
    expect(trigger().getAttribute("aria-label")).toBe("メニュー");
    expect(trigger().getAttribute("aria-expanded")).toBe("false");
    expect(trigger().getAttribute("aria-controls")).toBe(drawer().id);
    expect(drawer().getAttribute("role")).toBe("dialog");
    expect(drawer().getAttribute("aria-modal")).toBe("true");
    expect(drawer().hasAttribute("inert")).toBe(true);
    expect(drawer().querySelector("aside")).not.toBeNull();
    expect($('[data-testid="nav-drawer-bar"]')?.textContent).toContain("RAG");
    expect($("main")?.hasAttribute("inert")).toBe(false);
  });

  it("開くとドロワーの閉じるボタンへフォーカスし、背面を inert にする。折りたたみの選好（collapsed）があっても展開して描く", () => {
    render(<Shell collapsed />);
    click(trigger());
    expect(trigger().getAttribute("aria-expanded")).toBe("true");
    expect(drawer().hasAttribute("inert")).toBe(false);
    expect(document.activeElement?.getAttribute("data-testid")).toBe("nav-drawer-close");
    // 見える名前は共有の Tooltip（#372）。HTML の title は使わない。
    expect(document.activeElement?.hasAttribute("title")).toBe(false);
    expect($("main")?.hasAttribute("inert")).toBe(true);
    expect($(".pr-skip-link")?.hasAttribute("inert")).toBe(true);
    expect(drawer().querySelector("aside")?.getAttribute("data-state")).toBe("expanded");
    // フッターも展開（利用者名が出る）
    expect(drawer().textContent).toContain("山田");
  });

  it("Escape で閉じ、「メニュー」ボタンへフォーカスを戻す", () => {
    render(<Shell />);
    click(trigger());
    key(document.activeElement!, { key: "Escape" });
    expect(trigger().getAttribute("aria-expanded")).toBe("false");
    expect(drawer().hasAttribute("inert")).toBe(true);
    expect(document.activeElement).toBe(trigger());
  });

  it("閉じるボタン・scrim のタップで閉じる", () => {
    render(<Shell />);
    click(trigger());
    click($('[data-testid="nav-drawer-close"]')!);
    expect(trigger().getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(trigger());

    click(trigger());
    click($('[data-testid="nav-drawer-scrim"]')!);
    expect(trigger().getAttribute("aria-expanded")).toBe("false");
  });

  it("ナビのリンクを選ぶと閉じる（今のページのリンクでも閉じる）", () => {
    render(<Shell />);
    click(trigger());
    click(drawer().querySelector('a[href="/documents"]')!);
    expect(trigger().getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(trigger());
  });

  it("画面が移ったら（リンク以外の操作でも）閉じる", () => {
    render(<Shell path="/documents" />);
    click(trigger());
    render(<Shell path="/settings" />);
    expect(trigger().getAttribute("aria-expanded")).toBe("false");
  });

  it("Tab / Shift+Tab はドロワーの端で反対の端へ回る（フォーカスの閉じ込め）", () => {
    render(<Shell />);
    click(trigger());
    const close = $('[data-testid="nav-drawer-close"]')!;
    expect(document.activeElement).toBe(close);
    key(close, { key: "Tab", shiftKey: true });
    const last = document.activeElement!;
    expect(last).not.toBe(close);
    expect(drawer().contains(last)).toBe(true);
    key(last, { key: "Tab" });
    expect(document.activeElement).toBe(close);
  });
});

describe("AppShell（md 以上）は従来どおり", () => {
  beforeEach(() => mockViewport(false));

  it("メニューボタンとドロワーを出さず、サイドバーを本文の左に置き、折りたたみの選好に従う", () => {
    render(<Shell collapsed />);
    expect($('[data-testid="nav-drawer-trigger"]')).toBeNull();
    expect($('[data-testid="nav-drawer"]')).toBeNull();
    const aside = container.querySelector("aside")!;
    expect(aside.parentElement?.querySelector(":scope > main")).not.toBeNull();
    expect(aside.getAttribute("data-state")).toBe("collapsed");
    expect(aside.querySelector('button[aria-label="サイドバーを展開"]')).not.toBeNull();
  });
});

describe("AppShell の本文へスキップ", () => {
  beforeEach(() => mockViewport(false));

  it("押すと URL に #pr-main を付けず（既定の移動を止め）、本文の <main> へフォーカスを移す", () => {
    render(<Shell />);
    const link = $<HTMLAnchorElement>(".pr-skip-link")!;
    const main = $<HTMLElement>("main#pr-main")!;
    expect(link.getAttribute("href")).toBe("#pr-main");
    expect(main.getAttribute("tabindex")).toBe("-1");
    const event = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 });
    act(() => {
      link.dispatchEvent(event);
    });
    expect(event.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(main);
  });

  it("修飾キー付きのクリック（新しいタブで開く等）はブラウザに任せる", () => {
    render(<Shell />);
    const link = $<HTMLAnchorElement>(".pr-skip-link")!;
    const event = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0, ctrlKey: true });
    // happy-dom がリンクの既定の移動をしないよう、document で止める前に defaultPrevented を読む。
    let prevented: boolean | undefined;
    const listener = (current: Event) => {
      prevented = current.defaultPrevented;
      current.preventDefault();
    };
    document.addEventListener("click", listener);
    act(() => {
      link.dispatchEvent(event);
    });
    document.removeEventListener("click", listener);
    expect(prevented).toBe(false);
  });
});

describe("navDrawerKeyAction / closesNavDrawer", () => {
  it("Escape は閉じる。Tab は末尾で先頭へ、Shift+Tab は先頭で末尾へ回し、途中は既定の移動に任せる", () => {
    expect(navDrawerKeyAction("Escape", false, 2, 5)).toEqual({ type: "close" });
    expect(navDrawerKeyAction("Tab", false, 4, 5)).toEqual({ type: "focus", index: 0 });
    expect(navDrawerKeyAction("Tab", true, 0, 5)).toEqual({ type: "focus", index: 4 });
    expect(navDrawerKeyAction("Tab", false, -1, 5)).toEqual({ type: "focus", index: 0 });
    expect(navDrawerKeyAction("Tab", false, 1, 5)).toBeNull();
    expect(navDrawerKeyAction("Enter", false, 1, 5)).toBeNull();
    expect(navDrawerKeyAction("Tab", false, -1, 0)).toBeNull();
  });

  it("Tab が止まる要素に開閉の見出し（summary）を含め、閉じた開閉の中身・inert の中は除く（#2512）", () => {
    const root = document.createElement("div");
    root.innerHTML = `
      <button data-id="close">閉じる</button>
      <details open data-id="open">
        <summary data-id="open-summary">開いた見出し</summary>
        <div tabindex="0" data-id="open-region">中身</div>
      </details>
      <details data-id="closed">
        <summary data-id="closed-summary">閉じた見出し</summary>
        <button data-id="closed-button">中の操作</button>
        <details open><summary data-id="nested-summary">入れ子</summary></details>
      </details>
      <div inert><button data-id="inert-button">inert</button></div>
      <div tabindex="-1" data-id="skip">-1</div>`;
    expect(focusableIn(root).map((element) => element.dataset.id)).toEqual([
      "close",
      "open-summary",
      "open-region",
      "closed-summary",
    ]);
  });

  it("リンクの中のクリックだけが閉じる対象になる", () => {
    const link = document.createElement("a");
    link.href = "/documents";
    const icon = document.createElement("span");
    link.appendChild(icon);
    expect(closesNavDrawer(icon)).toBe(true);
    expect(closesNavDrawer(document.createElement("button"))).toBe(false);
    expect(closesNavDrawer(null)).toBe(false);
  });
});

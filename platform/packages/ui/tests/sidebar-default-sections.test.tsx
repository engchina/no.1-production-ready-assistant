// @vitest-environment happy-dom
import { FileText, MessagesSquare, Settings, Sparkles } from "lucide-react";
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Sidebar } from "../src/components/app-shell/Sidebar";
import type { NavLinkComponent, NavSection } from "../src/navigation/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// 業務の入口（use）だけを展開し、ほかは既定で閉じるナビ（#1563）。
const sections: NavSection[] = [
  {
    key: "use",
    title: "AI 活用",
    items: [
      { href: "/chat", label: "チャット", icon: MessagesSquare },
      { href: "/search", label: "検索", icon: Sparkles },
    ],
  },
  { key: "build", title: "構築", defaultCollapsed: true, items: [{ href: "/documents", label: "文書一覧", icon: FileText }] },
  { key: "settings", title: "システム設定", defaultCollapsed: true, items: [{ href: "/settings", label: "設定", icon: Settings }] },
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
  sectionToggleExpand: (section: string) => `${section} を展開`,
  sectionToggleCollapse: (section: string) => `${section} を折りたたむ`,
};

/** ui-store の setSectionCollapsed と同じく、保存値を書き換える最小の親。 */
function Harness({
  path,
  saved = {},
  onSave,
}: {
  path: string;
  saved?: Record<string, boolean>;
  onSave?: (next: Record<string, boolean>) => void;
}) {
  const [collapsedSections, setCollapsedSections] = useState(saved);
  return (
    <Sidebar
      sections={sections}
      currentPath={path}
      title={{ line1: "Production Ready", line2: "RAG", full: "Production Ready RAG" }}
      collapsed={false}
      onToggleCollapsed={() => undefined}
      collapsedSections={collapsedSections}
      onSetSectionCollapsed={(key, collapsed) =>
        setCollapsedSections((current) => {
          // 今のページのセクションを開く effect は値が変わらなければ同じオブジェクトを返す（再描画を繰り返さない）。
          if (current[key] === collapsed) return current;
          const next = { ...current, [key]: collapsed };
          onSave?.(next);
          return next;
        })
      }
      linkComponent={Link}
      labels={labels}
    />
  );
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: false,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

const toggle = (title: string) =>
  [...container.querySelectorAll<HTMLButtonElement>("button[aria-expanded]")].find((button) =>
    button.getAttribute("aria-label")?.startsWith(`${title} を`)
  )!;

const expanded = (title: string) => toggle(title).getAttribute("aria-expanded") === "true";

// #1563
describe("Sidebar のセクションの既定の開閉", () => {
  it("保存値が無ければ defaultCollapsed の無いセクションだけを展開する", () => {
    act(() => root.render(<Harness path="/chat" />));
    expect(expanded("AI 活用")).toBe(true);
    expect(expanded("構築")).toBe(false);
    expect(expanded("システム設定")).toBe(false);
    expect(container.querySelector("#nav-section-settings")?.hasAttribute("inert")).toBe(true);
  });

  it("今のページを含むセクションは既定で閉じていても開く", () => {
    act(() => root.render(<Harness path="/settings" />));
    expect(expanded("システム設定")).toBe(true);
    expect(expanded("構築")).toBe(false);
  });

  it("保存値は既定より優先し、見出しで開閉した結果を保存する", () => {
    const saves: Record<string, boolean>[] = [];
    act(() =>
      root.render(<Harness path="/chat" saved={{ use: true, build: false }} onSave={(next) => saves.push(next)} />)
    );
    // 今のページ（/chat）を含む use は開く（既存の振る舞い）。build は保存値の「展開」を保つ。
    expect(expanded("AI 活用")).toBe(true);
    expect(expanded("構築")).toBe(true);
    expect(expanded("システム設定")).toBe(false);

    // 既定で閉じたセクションを一度押すと開き、false を保存する（既定を知らないトグルの true にしない）。
    act(() => toggle("システム設定").click());
    expect(expanded("システム設定")).toBe(true);
    expect(saves.at(-1)).toMatchObject({ settings: false });

    act(() => toggle("システム設定").click());
    expect(expanded("システム設定")).toBe(false);
    expect(saves.at(-1)).toMatchObject({ settings: true });
  });
});

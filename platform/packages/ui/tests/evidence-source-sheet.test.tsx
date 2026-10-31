// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EvidenceSourceSheet, useEvidenceSource, type EvidenceListItem } from "../src";

// #2360: 回答の根拠を右のサイドパネルで開く共通の部品（RAG のチャット・RAG 検索・Agent のチャットで同じ）。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

const ITEMS: EvidenceListItem[] = [
  {
    key: "doc-a:1",
    fileName: "文書 A.pdf",
    text: "合成の抜粋 1",
    usedInAnswer: true,
    answerUse: "confirmed",
    routeBadges: ["Evidence"],
    score: 0.81,
    rerankScore: 0.42,
    chips: [{ id: "page", label: "ページ", value: "p.3" }],
  },
  {
    key: "doc-b:2",
    fileName: "文書 B.pdf",
    text: "合成の抜粋 2",
    usedInAnswer: null,
    answerUse: null,
    score: 0.5,
    rerankScore: null,
  },
];

function Harness({
  preview = false,
  kind,
  notice,
}: {
  preview?: boolean;
  kind?: "answer" | "search";
  notice?: { title: string; body?: string };
}) {
  const source = useEvidenceSource<number>();
  return (
    <>
      {ITEMS.map((item, index) => (
        <button key={item.key} type="button" data-testid={`open-${index}`} onClick={() => source.open(index)}>
          {item.fileName}
        </button>
      ))}
      {source.mounted && source.shown != null ? (
        <EvidenceSourceSheet
          open={source.selected !== null}
          onClose={source.close}
          item={ITEMS[source.shown]}
          index={source.shown}
          kind={kind}
          notice={notice}
        >
          {preview ? <div data-testid="preview">原文</div> : null}
        </EvidenceSourceSheet>
      ) : null}
    </>
  );
}

const $ = <T extends Element = HTMLElement>(selector: string) => document.querySelector<T>(selector);
const sheet = () => $<HTMLDivElement>('[data-testid="evidence-source-sheet"]');
const click = (target: Element) =>
  act(() => {
    (target as HTMLElement).click();
  });

beforeEach(() => {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: true,
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

describe("EvidenceSourceSheet", () => {
  it("根拠を取得する前と取得失敗でも子の状態をパネル内に表示する", () => {
    act(() => root.render(<EvidenceSourceSheet open onClose={() => undefined} item={null} index={0}><p role="status">取得中</p></EvidenceSourceSheet>));
    expect(sheet()?.textContent).toContain("取得中");
    expect(sheet()?.querySelector('[data-testid="evidence-source-summary"]')).toBeNull();
    act(() => root.render(<EvidenceSourceSheet open onClose={() => undefined} item={null} index={0}><p role="alert">閲覧できません</p></EvidenceSourceSheet>));
    expect(sheet()?.textContent).toContain("閲覧できません");
    expect(sheet()?.querySelector('[data-testid="evidence-source-preview"]')).toBeNull();
  });

  it("一度開くまでは描かず、開くと右の wide のパネルに要約（番号・文書名・引用の印・経路・チップ・スコア）を出す（操作の行は持たない。#2462）", () => {
    act(() => root.render(<Harness />));
    expect(sheet()).toBeNull();
    click($('[data-testid="open-0"]')!);
    const panel = sheet()!;
    expect(panel.getAttribute("role")).toBe("dialog");
    expect(panel.getAttribute("data-state")).toBe("open");
    // 読むためのパネルなので、開いたらパネルそのものへフォーカスする（閉じるボタンの Tooltip を開かない）。
    expect(document.activeElement).toBe(panel);
    expect(panel.className).toContain("right-0");
    expect(panel.className).toContain("sm:w-[64rem]");
    const titleId = panel.getAttribute("aria-labelledby")!;
    expect(document.getElementById(titleId)?.textContent).toBe("回答の根拠");
    const summary = panel.querySelector('[data-testid="evidence-source-summary"]')!;
    expect(summary.getAttribute("aria-label")).toBe("根拠の要約");
    expect(summary.textContent).toContain("1");
    expect(summary.querySelector('[data-testid="evidence-file-name"]')?.textContent).toBe("文書 A.pdf");
    expect(summary.textContent).toContain("回答で引用");
    expect(summary.textContent).toContain("Evidence");
    expect(summary.textContent).toContain("p.3");
    expect(summary.textContent).toContain("0.810");
    expect(summary.querySelector("button, a")).toBeNull();
    // プレビューが無ければ抜粋は全文で、開閉を出さない。
    expect(summary.querySelector('[data-testid="citation-text"]')?.textContent).toBe("合成の抜粋 1");
    expect(summary.querySelector('[data-testid="evidence-excerpt-details"]')).toBeNull();
    expect(panel.querySelector('[data-testid="evidence-source-preview"]')).toBeNull();
  });

  it("案内を渡すと要約の上に info の Banner で出す（位置の決まらない根拠の行。#2710）", () => {
    act(() => root.render(<Harness preview notice={{ title: "位置は確定していません", body: "候補 2 件" }} />));
    click($('[data-testid="open-0"]')!);
    const panel = sheet()!;
    const notice = panel.querySelector('[data-testid="evidence-source-notice"]')!;
    expect(notice.querySelector('[data-surface-tint="info"]')).not.toBeNull();
    expect(notice.textContent).toContain("位置は確定していません");
    expect(notice.textContent).toContain("候補 2 件");
    const summary = panel.querySelector('[data-testid="evidence-source-summary"]')!;
    // 案内は要約より前（読み上げの順も先）。
    expect(notice.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    act(() => root.render(<Harness preview />));
    expect(sheet()!.querySelector('[data-testid="evidence-source-notice"]')).toBeNull();
  });

  it("プレビューを渡すと抜粋と頁・章節のチップを既定で閉じた開閉に入れ、要約の下にプレビューの領域を置く（#2502）", () => {
    act(() => root.render(<Harness preview />));
    click($('[data-testid="open-0"]')!);
    const panel = sheet()!;
    const summary = panel.querySelector('[data-testid="evidence-source-summary"]')!;
    // 見出しの行（文書名・引用の印・経路）とスコアは開閉の外で出したまま。
    expect(summary.querySelector('[data-testid="evidence-file-name"]')?.textContent).toBe("文書 A.pdf");
    expect(summary.querySelector('[data-testid="evidence-route-badges"]')).not.toBeNull();
    expect(summary.querySelector('[data-testid="citation-score-panel"]')?.textContent).toContain("0.810");
    const details = summary.querySelector<HTMLDetailsElement>('[data-testid="evidence-excerpt-details"]')!;
    expect(details.open).toBe(false);
    expect(details.querySelector('[data-testid="evidence-excerpt-toggle"]')?.textContent).toBe("抜粋と頁・章節");
    expect(details.querySelector('[data-testid="citation-text"]')?.textContent).toBe("合成の抜粋 1");
    expect(details.querySelector('[data-testid="evidence-chips"]')?.textContent).toContain("p.3");
    click(details.querySelector('[data-testid="evidence-excerpt-toggle"]')!);
    expect(details.open).toBe(true);
    const preview = panel.querySelector('[data-testid="evidence-source-preview"]')!;
    expect(preview.getAttribute("aria-label")).toBe("原文のプレビュー");
    expect(preview.querySelector('[data-testid="preview"]')).not.toBeNull();
  });

  it("開いた抜粋は高さに上限のあるスクロールの領域に入れ、プレビューの下限は画面の高さに合わせる（#2512）", () => {
    act(() => root.render(<Harness preview />));
    click($('[data-testid="open-0"]')!);
    const panel = sheet()!;
    const details = panel.querySelector<HTMLDetailsElement>('[data-testid="evidence-excerpt-details"]')!;
    click(details.querySelector('[data-testid="evidence-excerpt-toggle"]')!);
    const scroll = details.querySelector<HTMLElement>('[data-testid="evidence-excerpt-scroll"]')!;
    expect(scroll.getAttribute("role")).toBe("region");
    expect(scroll.getAttribute("aria-label")).toBe("抜粋と頁・章節");
    expect(scroll.className).toContain("max-h-[18dvh]");
    expect(scroll.className).toContain("sm:max-h-[min(20rem,28dvh)]");
    expect(scroll.className).toContain("overflow-y-auto");
    expect(scroll.querySelector('[data-testid="citation-text"]')?.textContent).toBe("合成の抜粋 1");
    expect(scroll.querySelector('[data-testid="evidence-chips"]')?.textContent).toContain("p.3");
    // あふれていなければ Tab で止めず、フェードも出さない（あふれたときの動きは e2e で確かめる）。
    expect(scroll.getAttribute("data-overflowing")).toBe("false");
    expect(scroll.hasAttribute("tabindex")).toBe(false);
    expect(scroll.querySelector('[data-testid="evidence-excerpt-fade"]')?.className).toContain("opacity-0");
    const preview = panel.querySelector<HTMLElement>('[data-testid="evidence-source-preview"]')!;
    expect(preview.className).toContain("min-h-[min(24rem,45dvh)]");
    expect(preview.className).toContain("sm:min-h-[min(32rem,55dvh)]");
  });

  it("抜粋を開いたまま閉じても、開き直す・別の根拠を開くと閉じた状態から出す（#2502）", () => {
    act(() => root.render(<Harness preview />));
    click($('[data-testid="open-0"]')!);
    const details = () => sheet()!.querySelector<HTMLDetailsElement>('[data-testid="evidence-excerpt-details"]')!;
    click(details().querySelector('[data-testid="evidence-excerpt-toggle"]')!);
    expect(details().open).toBe(true);
    click($('[data-testid="open-1"]')!);
    expect(sheet()!.querySelector('[data-testid="evidence-file-name"]')?.textContent).toBe("文書 B.pdf");
    expect(details().open).toBe(false);
    click(details().querySelector('[data-testid="evidence-excerpt-toggle"]')!);
    act(() => {
      sheet()!.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: "Escape" }));
    });
    expect(sheet()!.getAttribute("data-state")).toBe("closed");
    click($('[data-testid="open-1"]')!);
    expect(sheet()!.getAttribute("data-state")).toBe("open");
    expect(details().open).toBe(false);
  });

  it("回答を作らない検索の箇所は「検索で見つかった箇所」。閉じた後も前の根拠を出したまま隠し、開いた操作へフォーカスを戻す", () => {
    act(() => root.render(<Harness kind="search" />));
    const opener = $<HTMLButtonElement>('[data-testid="open-1"]')!;
    opener.focus();
    click(opener);
    const panel = sheet()!;
    expect(document.getElementById(panel.getAttribute("aria-labelledby")!)?.textContent).toBe("検索で見つかった箇所");
    act(() => {
      panel.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: "Escape" }));
    });
    expect(sheet()!.getAttribute("data-state")).toBe("closed");
    expect(sheet()!.textContent).toContain("文書 B.pdf");
    expect(document.activeElement).toBe(opener);
  });
});

// @vitest-environment happy-dom
import { Layers } from "lucide-react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  AnswerEvidence,
  EvidenceCard,
  EvidenceList,
  EvidenceWithheldNote,
  evidenceCountLabel,
  evidenceLabels,
  evidenceScoreMeterPercent,
  formatEvidenceScore,
  type EvidenceListItem,
} from "../src";

// #2328: 回答の根拠の共有の部品（RAG のチャット・RAG 検索・Agent のチャットで同じ見出しとカード）。

function item(overrides: Partial<EvidenceListItem> = {}): EvidenceListItem {
  return {
    key: "doc-a:set:1",
    fileName: "文書 A.pdf",
    text: "合成の本文の抜粋です。",
    usedInAnswer: false,
    answerUse: null,
    score: 0.5,
    rerankScore: null,
    ...overrides,
  };
}

function html(node: React.ReactNode): HTMLElement {
  const host = document.createElement("div");
  host.innerHTML = renderToStaticMarkup(node);
  return host;
}

describe("evidenceCountLabel", () => {
  it("AI に渡した件数と、回答で引用・該当確認済み・参考の内訳を 0 件を省いて並べる", () => {
    const items = [
      item({ usedInAnswer: true, answerUse: "confirmed" }),
      item({ usedInAnswer: true, answerUse: "reference" }),
      item({ usedInAnswer: true, answerUse: null }),
      item(),
    ];
    expect(evidenceCountLabel(items)).toBe("AI に渡した 4 件 ／ 回答で引用 3 件 ／ うち該当確認済み 1 件 ／ 参考 1 件");
  });

  it("引用したかが分からない（検索だけ）なら件数だけ、引用が 0 件なら内訳を出さない", () => {
    expect(evidenceCountLabel([item({ usedInAnswer: null })])).toBe("AI に渡した 1 件");
    expect(evidenceCountLabel([item(), item()])).toBe("AI に渡した 2 件");
  });

  it("まとまりだけを参照した箇所は「回答で引用」に数えず、「まとまりで参照」に分ける（#2710）", () => {
    const items = [
      item({ usedInAnswer: true, answerUse: "confirmed" }),
      item({ usedAsContext: true }),
      item({ usedAsContext: true }),
      // 引用した箇所は、まとまりの印があっても「回答で引用」だけに数える。
      item({ usedInAnswer: true, answerUse: null, usedAsContext: true }),
    ];
    expect(evidenceCountLabel(items)).toBe(
      "AI に渡した 4 件 ／ 回答で引用 2 件 ／ うち該当確認済み 1 件 ／ まとまりで参照 2 件",
    );
  });

  it("文言は上書きできる", () => {
    expect(evidenceCountLabel([item()], evidenceLabels({ count: (n) => `${n} items` }))).toBe("1 items");
  });
});

describe("EvidenceCard", () => {
  it("番号・文書名・引用のバッジ・経路・抜粋・チップ・補足・原典・スコアを出す", () => {
    const host = html(
      <ul>
        <EvidenceCard
          index={1}
          item={item({
            usedInAnswer: true,
            answerUse: "confirmed",
            superseded: true,
            routeBadges: ["Vector #1", "Evidence"],
            rerankScore: 0.42,
            chips: [{ id: "page", label: "ページ", value: "p.3" }],
            tags: [
              { id: "recipe", value: "レシピ 1", icon: Layers },
              { id: "category", value: "分類 A", tone: "info" },
            ],
            sourceUrl: "https://example.com/a",
          })}
          actions={<button type="button">根拠を表示</button>}
        />
      </ul>,
    );
    const main = host.querySelector('[data-testid="citation-main"]')!;
    expect(main.textContent).toContain("2");
    expect(main.textContent).toContain("文書 A.pdf");
    expect(main.textContent).toContain("回答で引用");
    expect(main.textContent).toContain("旧版");
    expect(host.querySelector('[data-testid="evidence-route-badges"]')?.textContent).toBe("Vector #1Evidence");
    expect(host.querySelector('[data-testid="citation-text"]')?.textContent).toBe("合成の本文の抜粋です。");
    expect(host.querySelector("dt")?.textContent).toBe("ページ");
    expect(host.querySelector("dd")?.textContent).toBe("p.3");
    expect(host.textContent).toContain("レシピ 1");
    expect(host.querySelector('[data-surface-tint="info"]')?.textContent).toBe("分類 A");
    expect(host.querySelector("a")?.getAttribute("href")).toBe("https://example.com/a");
    expect(host.querySelector('[role="meter"]')?.getAttribute("aria-valuetext")).toBe("0.420");
    expect(host.querySelector("button")?.textContent).toBe("根拠を表示");
  });

  it("まとまりだけを参照した箇所は「回答で引用」ではなく「まとまりで参照」のバッジを出す（#2710）", () => {
    const context = html(<ul><EvidenceCard index={0} item={item({ usedAsContext: true })} /></ul>);
    const main = context.querySelector('[data-testid="citation-main"]')!;
    expect(main.textContent).toContain("まとまりで参照");
    expect(main.textContent).not.toContain("回答で引用");
    const cited = html(<ul><EvidenceCard index={0} item={item({ usedInAnswer: true, usedAsContext: true })} /></ul>);
    expect(cited.textContent).toContain("回答で引用");
    expect(cited.textContent).not.toContain("まとまりで参照");
  });

  it("参考の記載は「参考」、引用していなければバッジを出さず、Rerank が無ければ未実行と出す", () => {
    const reference = html(<ul><EvidenceCard index={0} item={item({ usedInAnswer: true, answerUse: "reference" })} /></ul>);
    expect(reference.textContent).toContain("参考");
    expect(reference.textContent).not.toContain("回答で引用");
    const plain = html(<ul><EvidenceCard index={0} item={item({ score: null })} /></ul>);
    expect(plain.textContent).not.toContain("回答で引用");
    expect(plain.textContent).toContain("Rerank 未実行");
    expect(plain.textContent).toContain("—");
    expect(plain.querySelector('[role="meter"]')).toBeNull();
  });

  it("長いチップは行の幅で止めて省略し、全文を title と読み上げに残す（#3422）", () => {
    const longPath = Array.from({ length: 12 }, (_, index) => `第 ${index + 1} 章`).join(" > ");
    const card = html(
      <ul>
        <EvidenceCard index={0} item={item({ chips: [{ id: "section_path", label: "章節", value: longPath }] })} />
      </ul>
    );
    // 行（flex-wrap）の item の dl は、縮められない（min-width: auto）と文字の全幅まで伸びる。幅の確認は e2e（RAG・Agent）。
    const chips = card.querySelector('[data-testid="evidence-chips"]')!;
    expect(chips.classList).toContain("min-w-0");
    expect(chips.classList).toContain("max-w-full");
    const value = chips.querySelector("dd")!;
    expect(value.classList).toContain("truncate");
    expect(value.getAttribute("title")).toBe(longPath);
    expect(value.textContent).toBe(longPath);
    expect(chips.querySelector("dt")!.textContent).toBe("章節");
  });
});

describe("AnswerEvidence", () => {
  it("根拠が無ければ何も出さず、あれば件数の見出しの折りたたみ（既定は閉じる）にカードを入れる", () => {
    expect(renderToStaticMarkup(<AnswerEvidence items={[]} />)).toBe("");
    const host = html(<AnswerEvidence items={[item({ usedInAnswer: true }), item({ key: "b" })]} />);
    const details = host.querySelector("details")!;
    expect(details.getAttribute("data-testid")).toBe("answer-citations");
    expect(details.hasAttribute("open")).toBe(false);
    expect(host.querySelector("summary")?.textContent).toContain("AI に渡した 2 件 ／ 回答で引用 1 件");
    expect(host.querySelectorAll('[data-testid="evidence-card"]')).toHaveLength(2);
  });

  it("権限の無い根拠は数と理由だけを出し、AI に渡した件数に含める（#3252）", () => {
    const host = html(<AnswerEvidence items={[item({ usedInAnswer: true })]} withheldCount={2} />);
    expect(host.querySelector("summary")?.textContent).toContain(
      "AI に渡した 3 件 ／ 回答で引用 1 件 ／ 権限の無い根拠 2 件",
    );
    const note = host.querySelector('[data-testid="evidence-withheld"]');
    expect(note?.textContent).toContain("権限の無い根拠 2 件");
    expect(note?.textContent).toContain("抜粋・資料名・原文は表示しません");
    expect(host.querySelectorAll('[data-testid="evidence-card"]')).toHaveLength(1);
    // 見られる根拠が無くても、権限の無い根拠があれば折りたたみと数の行だけを出す。
    const onlyWithheld = html(<AnswerEvidence items={[]} withheldCount={1} />);
    expect(onlyWithheld.querySelector("summary")?.textContent).toContain("AI に渡した 1 件 ／ 権限の無い根拠 1 件");
    expect(onlyWithheld.querySelectorAll('[data-testid="evidence-card"]')).toHaveLength(0);
    expect(renderToStaticMarkup(<EvidenceWithheldNote count={0} />)).toBe("");
    expect(evidenceCountLabel([item()], undefined, 0)).toBe("AI に渡した 1 件");
  });

  it("スコアは小数 3 桁、メーターは 0〜100%", () => {
    expect(formatEvidenceScore(0.12345)).toBe("0.123");
    expect(formatEvidenceScore(null)).toBe("—");
    expect(evidenceScoreMeterPercent(0.5)).toBe(50);
    expect(evidenceScoreMeterPercent(2)).toBe(100);
    expect(evidenceScoreMeterPercent(-1)).toBe(0);
  });
});

describe("EvidenceList", () => {
  it("見方の切り替え（タブ）を持たず、一覧は一定の高さでスクロールする（#2388）", () => {
    const host = html(<EvidenceList items={[item(), item({ key: "b" })]} />);
    expect(host.querySelector('[role="tablist"]')).toBeNull();
    expect(host.querySelector("ul")?.className).toContain("bounded-scroll-area-lg");
    expect(host.querySelectorAll('[data-testid="evidence-card"]')).toHaveLength(2);
  });

  it("onOpenSource を渡すと、各カードの操作の行の先頭に同じ名前の「根拠を表示」を出す（3 製品で同じ）", () => {
    const host = html(
      <EvidenceList
        items={[item(), item({ key: "b", fileName: "文書 B.pdf" })]}
        idPrefix="t"
        onOpenSource={() => undefined}
        renderActions={() => <span data-testid="extra">評価</span>}
      />,
    );
    const buttons = Array.from(host.querySelectorAll("button"));
    expect(buttons.map((button) => button.textContent)).toEqual(["根拠を表示", "根拠を表示"]);
    expect(buttons[1].getAttribute("aria-label")).toBe("文書 B.pdf の根拠を表示");
    expect(buttons[1].getAttribute("aria-haspopup")).toBe("dialog");
    expect(buttons[0].getAttribute("data-testid")).toBe("t-source-0");
    expect(host.querySelectorAll('[data-testid="extra"]')).toHaveLength(2);
  });

  it("開けない画面（onOpenSource なし）ではボタンを出さない", () => {
    const host = html(<EvidenceList items={[item()]} />);
    expect(host.querySelector("button")).toBeNull();
  });
});

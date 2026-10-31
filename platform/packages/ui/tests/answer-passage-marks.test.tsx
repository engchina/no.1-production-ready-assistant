// @vitest-environment happy-dom
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  AnswerPassageBadges,
  AnswerSections,
  answerPassageCounts,
  answerPassageQuote,
  answerPassageKey,
  hasAnswerPassageRecords,
  matchAnswerPassageMarks,
  parseAnswerPassageRecords,
  type AnswerSectionsBlock,
} from "../src";

// #2738: 回答の段落の印（未確認・要注意）・外した段落・回答全体の注意（RAG・Agent 共通）。
// 事例は platform/contracts/answer-passages/passage-marks.json（RAG・Agent の backend のテストと同じ）。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface ContractExample {
  id: string;
  published: string;
  passage_marks: unknown[];
  removed_passages: unknown[];
  answer_warnings: unknown[];
}

// happy-dom の URL は file: を扱わないため、パスで読む。
const CONTRACT_PATH = resolve(dirname(fileURLToPath(import.meta.url)), "../../../contracts/answer-passages/passage-marks.json");
const contract = JSON.parse(readFileSync(CONTRACT_PATH, "utf8")) as { states: string[]; reason_codes: Record<string, { kind: string; state?: string; label: string }>; examples: ContractExample[] };

function example(id: string): ContractExample {
  const found = contract.examples.find((item) => item.id === id);
  if (!found) throw new Error(`契約の事例 ${id} がありません`);
  return found;
}

/** 本文を空行で段落に分けた blocks（製品の組み立てを簡単にしたもの）。 */
function paragraphs(text: string): AnswerSectionsBlock[] {
  return text.split(/\n{2,}/).map((part, index) => ({ kind: "paragraph", key: `p${index}`, content: part }));
}

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe("parseAnswerPassageRecords", () => {
  it("3 つの項目がどれも無い記録（過去の回答）は null", () => {
    expect(parseAnswerPassageRecords(null)).toBeNull();
    expect(parseAnswerPassageRecords({ status: "completed", withheld: { claims: 2 } })).toBeNull();
    expect(hasAnswerPassageRecords(null)).toBe(false);
  });

  it("契約の事例を読み、件数を数える（state は契約の値、知らない値は warning）", () => {
    for (const item of contract.examples) {
      const records = parseAnswerPassageRecords(item)!;
      expect(records).not.toBeNull();
      expect(records.marks).toHaveLength(item.passage_marks.length);
      expect(records.removed).toHaveLength(item.removed_passages.length);
      expect(records.warnings).toHaveLength(item.answer_warnings.length);
      for (const mark of records.marks) expect(contract.states).toContain(mark.state);
    }
    expect(answerPassageCounts(parseAnswerPassageRecords(example("keep-unverified-remove-contradicted")))).toEqual({
      unverified: 0,
      warning: 1,
      removed: 1,
    });
    expect(answerPassageCounts(parseAnswerPassageRecords(example("guide-error-keeps-body")))).toEqual({
      unverified: 1,
      warning: 0,
      removed: 0,
    });
    const odd = parseAnswerPassageRecords({ passage_marks: [{ text: "文。", state: "other", reasons: [{ code: "x" }] }] })!;
    expect(odd.marks[0]).toEqual({ text: "文。", state: "warning", reasons: [{ code: "x", label: "x", detail: "" }] });
    expect(odd.removed).toEqual([]);
  });

  it("契約の理由の語彙は、段落の印・外した段落・回答の注意のどれかに分かれる", () => {
    for (const [code, entry] of Object.entries(contract.reason_codes)) {
      expect(["passage", "removed", "answer"], code).toContain(entry.kind);
      if (entry.kind === "passage") expect(contract.states, code).toContain(entry.state);
      expect(entry.label.length, code).toBeGreaterThan(0);
    }
  });
});

describe("matchAnswerPassageMarks", () => {
  it("箇条書きの印・強調・空白の差を無視し、同じ段落 → 含む段落の順に当てる", () => {
    expect(answerPassageKey("1. **対象** を 開きます【証拠 1】。")).toBe(answerPassageKey("対象を開きます【証拠 1】。"));
    expect(answerPassageKey("・項目の説明\n根拠：文書 A p.3")).toBe("項目の説明");
    const { groups, targets } = matchAnswerPassageMarks(
      ["要約の段落です。", "1. 手順の 1 つ目です。続けて確認します。", "はい。"],
      [
        { text: "続けて確認します。", state: "warning", reasons: [{ code: "item_check", label: "検査", detail: "" }] },
        { text: "手順の 1 つ目です。", state: "unverified", reasons: [{ code: "unsupported", label: "根拠に無い", detail: "" }] },
        { text: "見当たらない段落。", state: "unverified", reasons: [] },
      ]
    );
    expect(targets).toEqual([1, 1, -1]);
    // 1 つの段落に複数の印が当たれば、unverified を優先して理由を並べる。
    expect(groups[1]?.state).toBe("unverified");
    expect(groups[1]?.reasons.map((reason) => reason.code)).toEqual(["item_check", "unsupported"]);
    expect(groups[0]).toBeNull();
    expect(groups[2]).toBeNull();
  });
});

describe("AnswerSections の段落の印", () => {
  it("契約の事例: 印の段落に線と印を付け、押すと理由を開き、外した段落と確認が必要な点の節を出す", () => {
    const item = example("keep-unverified-remove-contradicted");
    const passages = parseAnswerPassageRecords(item);
    act(() => root.render(<AnswerSections blocks={paragraphs(item.published)} passages={passages} />));
    const marked = Array.from(host.querySelectorAll('[data-testid="answer-passage-mark"]'));
    expect(marked).toHaveLength(1);
    expect(marked[0].getAttribute("data-passage-state")).toBe("warning");
    expect(marked[0].textContent).toContain("単価は 254・253・300 のいずれかです");
    expect(marked[0].className).toContain("border-l-2");
    const toggle = marked[0].querySelector('[data-testid="answer-passage-mark-toggle"]') as HTMLButtonElement;
    expect(toggle.textContent).toBe("要注意の理由");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(marked[0].querySelector('[data-testid="answer-passage-mark-reasons"]')).toBeNull();
    act(() => toggle.click());
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    const reasons = marked[0].querySelector('[data-testid="answer-passage-mark-reasons"]')!;
    expect(reasons.id).toBe(toggle.getAttribute("aria-controls"));
    expect(reasons.textContent).toContain("根拠の表の対応と違う可能性");
    expect(reasons.textContent).toContain("監査の理由が、根拠の表と違う対応（値『253』を『項目甲』の値としています）を述べています。");

    const removed = host.querySelector('[data-testid="answer-removed-passages"]')!;
    expect(removed.querySelector("h4")?.textContent).toBe("根拠と矛盾し除いた内容");
    expect(removed.querySelector('[data-testid="answer-removed-passage"]')?.textContent).toBe(
      "「変更は翌月から反映されます。」根拠と矛盾：根拠では当月から反映されると記載されています。"
    );
    const marks = host.querySelector('[data-testid="answer-marked-passages"]')!;
    expect(marks.querySelector("h4")?.textContent).toBe("確認が必要な点");
    expect(host.querySelector('[data-testid="answer-passage-warnings"]')).toBeNull();
    // 外した段落・確認が必要な点の後に、今までの「資料から確認できなかった事項」の節は作らない（製品が渡さない）。
    expect(host.textContent).not.toContain("資料から確認できなかった事項");
  });

  it("確認が必要な点の項目を押すと、本文の段落へ移ってフォーカスする", () => {
    const item = example("guide-error-keeps-body");
    const scrollIntoView = vi.fn();
    (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView = scrollIntoView;
    act(() => root.render(<AnswerSections blocks={paragraphs(item.published)} passages={parseAnswerPassageRecords(item)} />));
    const link = host.querySelector('[data-testid="answer-marked-passage-link"]') as HTMLButtonElement;
    expect(link.textContent).toBe("「登録後は自動で通知されます。」");
    expect(link.getAttribute("aria-label")).toBe("「登録後は自動で通知されます。」（本文の段落へ移動）");
    act(() => link.click());
    const marked = host.querySelector('[data-testid="answer-passage-mark"]') as HTMLElement;
    expect(marked.getAttribute("data-passage-state")).toBe("unverified");
    expect(document.activeElement).toBe(marked);
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    // 回答全体の注意は本文の上の warning の Banner。
    const warnings = host.querySelector('[data-testid="answer-passage-warnings"]')!;
    expect(warnings.textContent).toContain("業務ガイドの手順の確認で問題が見つかった");
    expect(warnings.textContent).toContain("必須の手順『終了日の入力』の説明がありません。");
    expect(host.querySelector('[data-testid="answer-text"]')?.firstElementChild).toBe(warnings);
  });

  it("理由の無い印は状態の説明を出し、項目（箇条書き）にも印を付ける。当たらない印は押せない引用で出す", () => {
    act(() =>
      root.render(
        <AnswerSections
          blocks={[
            {
              kind: "section",
              key: "s",
              title: "手順",
              ordered: true,
              entries: [
                { key: "a", content: "画面を開きます。", passageText: "1. 画面を開きます。" },
                { key: "b", content: "保存します。" },
              ],
            },
          ]}
          passages={{
            marks: [
              { text: "1. 画面を開きます。", state: "unverified", reasons: [] },
              { text: "本文に無い段落。", state: "warning", reasons: [{ code: "superseded", label: "旧版の資料に基づく", detail: "" }] },
            ],
            removed: [],
            warnings: [],
          }}
        />
      )
    );
    const marked = host.querySelectorAll('[data-testid="answer-passage-mark"]');
    expect(marked).toHaveLength(1);
    expect(marked[0].closest("li")?.textContent).toContain("画面を開きます。");
    act(() => (marked[0].querySelector("button") as HTMLButtonElement).click());
    expect(marked[0].textContent).toContain("この段落は根拠で確かめられていません。");
    const items = host.querySelectorAll('[data-testid="answer-marked-passage"]');
    expect(items).toHaveLength(2);
    expect(items[1].querySelector("button")).toBeNull();
    expect(items[1].textContent).toContain("要注意：旧版の資料に基づく");
  });

  it("同じ理由名は一覧で一度だけ表示し、詳細は両方保持する", () => {
    act(() => root.render(<AnswerSections blocks={paragraphs("段落。")}
      passages={{marks:[{text:"段落。",state:"warning",reasons:[
        {code:"application_unaudited",label:"条件への当てはめを確かめていない",detail:"前提 A"},
        {code:"application_unaudited",label:"条件への当てはめを確かめていない",detail:"前提 B"},
      ]}],removed:[],warnings:[]}} />));
    const summary = host.querySelector('[data-testid="answer-marked-passage"]');
    expect(summary?.textContent?.match(/条件への当てはめを確かめていない/g)).toHaveLength(1);
    const button=host.querySelector('[data-testid="answer-passage-mark"] button') as HTMLButtonElement;
    act(()=>button.click());
    expect(host.textContent).toContain("前提 A");
    expect(host.textContent).toContain("前提 B");
  });

  it("記録が無い（過去の回答）なら印・節・注意を出さない", () => {
    act(() => root.render(<AnswerSections blocks={paragraphs("段落 1。\n\n段落 2。")} passages={null} />));
    expect(host.querySelector('[data-testid="answer-passage-mark"]')).toBeNull();
    expect(host.querySelectorAll('[data-testid="answer-section"]')).toHaveLength(0);
  });
});

describe("answerPassageQuote", () => {
  it("箇条書き・番号の印・強調・根拠の ID を除き、長い文は切る", () => {
    expect(answerPassageQuote("- **承認**は部署が行います【証拠 ID: 0f3a…:7】。")).toBe("「承認は部署が行います。」");
    expect(answerPassageQuote("・項目の説明")).toBe("「項目の説明」");
    expect(answerPassageQuote("1. 手順の 1 つ目【証拠 1】")).toBe("「手順の 1 つ目」");
    expect(answerPassageQuote("あ".repeat(90))).toBe(`「${"あ".repeat(80)}…」`);
  });
});

describe("AnswerPassageBadges", () => {
  it("未確認・要注意・矛盾で除外の件数のバッジを出し、0 件は出さない", () => {
    act(() =>
      root.render(
        <AnswerPassageBadges
          records={{
            marks: [
              { text: "a", state: "unverified", reasons: [] },
              { text: "b", state: "unverified", reasons: [] },
            ],
            removed: [{ text: "c", reasons: [] }],
            warnings: [],
          }}
        />
      )
    );
    expect(host.querySelector('[data-testid="answer-passage-badges-unverified"]')?.textContent).toBe("未確認 2 件");
    expect(host.querySelector('[data-testid="answer-passage-badges-warning"]')).toBeNull();
    expect(host.querySelector('[data-testid="answer-passage-badges-removed"]')?.textContent).toBe("矛盾で除外 1 件");
    act(() => root.render(<AnswerPassageBadges records={{ marks: [], removed: [], warnings: [] }} />));
    expect(host.querySelector('[data-testid="answer-passage-badges"]')).toBeNull();
  });
});


describe("可視内容のない段落の記録", () => {
  const empty = ["\u200b", "\u200c\u200d\u2060\ufeff", "**\u200b**", "- \u200b", "```\n\u200b\n```", "[\u200b](https://example.invalid)", "---"];
  const valid = ["保存", "0", "-1", "A", "中", "👍", "👩‍💻", "**保存**", "[保存](https://example.invalid)", "A#1", "2*3", "_"];
  it("不可視・書式だけの印を一覧と件数から除き、原文と短い有効文字を保つ", () => {
    const raw = { passage_marks: [...empty, ...valid].map((text) => ({ text, state: "unverified", reasons: [] })), removed_passages: empty.map((text) => ({ text, reasons: [] })) };
    const before = JSON.stringify(raw);
    const records = parseAnswerPassageRecords(raw)!;
    expect(records.marks.map((row) => row.text)).toEqual(valid);
    expect(records.removed).toEqual([]);
    expect(answerPassageCounts(records)).toEqual({ unverified: valid.length, warning: 0, removed: 0 });
    expect(JSON.stringify(raw)).toBe(before);
    expect(answerPassageQuote("[保存](https://example.invalid)")).toBe("「保存」");
  });
  it("直接入力にも同じ可視性を適用し、空リンクと空節とバッジを作らない", () => {
    const records = { marks: empty.map((text) => ({ text, state: "unverified" as const, reasons: [] })), removed: empty.map((text) => ({ text, reasons: [] })), warnings: [] };
    expect(answerPassageCounts(records)).toEqual({ unverified: 0, warning: 0, removed: 0 });
    expect(hasAnswerPassageRecords(records)).toBe(false);
    act(() => root.render(<><AnswerSections blocks={paragraphs("本文は保存します。")} passages={records} /><AnswerPassageBadges records={records} /></>));
    expect(host.textContent).toContain("本文は保存します。");
    expect(host.querySelector('[data-testid="answer-marked-passages"]')).toBeNull();
    expect(host.querySelector('[data-testid="answer-removed-passages"]')).toBeNull();
    expect(host.querySelector('[data-testid="answer-marked-passage-link"]')).toBeNull();
    expect(host.querySelector('[data-status-variant]')).toBeNull();
  });
});

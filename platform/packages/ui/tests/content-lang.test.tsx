// @vitest-environment happy-dom
import { act, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  AnswerPlainText,
  AnswerSections,
  ChatTurn,
  ChatUserMessage,
  CONTENT_LANG_SCAN_LIMIT,
  detectContentLang,
  DocumentPreview,
  EvidenceCard,
  ExcerptContent,
  MarkdownText,
  normalizeContentLang,
  resolveContentLang,
  type AnswerPassageRecords,
  type EvidenceListItem,
} from "../src";
import { SIMPLIFIED_ONLY_HAN, TRADITIONAL_ONLY_HAN } from "../src/lib/han-script-sets";

// 合成の例文（業務の内容ではない）。字形の違いを確かめるための一般的な文だけを使う。
const JA = "これはテスト用の例文で、字形の違いを確かめます。";
const ZH_HANS = "这是一个用于测试的示例句子，我们说明字形的区别。";
const ZH_HANT = "這是一個用於測試的範例句子，我們說明字形的區別。";

describe("detectContentLang", () => {
  it("かなが 2 割以上なら日本語", () => {
    expect(detectContentLang(JA)).toBe("ja");
    expect(detectContentLang("テスト")).toBe("ja");
  });

  it("簡体字だけ・繁体字だけの字で簡体と繁体を分ける", () => {
    expect(detectContentLang(ZH_HANS)).toBe("zh-Hans");
    expect(detectContentLang(ZH_HANT)).toBe("zh-Hant");
    expect(detectContentLang("这个怎么用？")).toBe("zh-Hans");
  });

  it("中国語の文に混ざる外来語のカタカナは日本語にしない", () => {
    expect(detectContentLang("我们用テスト这个工具进行测试和说明它们的区别")).toBe("zh-Hans");
  });

  it("かながある日本語の文の旧字 1 字は中国語にしない", () => {
    const traditional = TRADITIONAL_ONLY_HAN[0];
    expect(detectContentLang(`東京都内の${traditional}検査結果報告書類一覧表確認作業`)).toBe("ja");
  });

  it("漢字だけで印の字が無い・漢字が無い・簡体と繁体が拮抗するなら決めない", () => {
    expect(detectContentLang("東京都港区")).toBeUndefined();
    expect(detectContentLang("Hello, world 123")).toBeUndefined();
    expect(detectContentLang("")).toBeUndefined();
    expect(detectContentLang(null)).toBeUndefined();
    expect(detectContentLang(`${SIMPLIFIED_ONLY_HAN[0]}${TRADITIONAL_ONLY_HAN[0]}`)).toBeUndefined();
  });

  it("簡体字だけ・繁体字だけの字の集合は日本語の字（常用漢字の例）と重ならない", () => {
    const japanese = "国学会来東為対個骨直写説読";
    for (const char of japanese) {
      expect(SIMPLIFIED_ONLY_HAN.includes(char), char).toBe(false);
      expect(TRADITIONAL_ONLY_HAN.includes(char), char).toBe(false);
    }
    for (const char of SIMPLIFIED_ONLY_HAN) expect(TRADITIONAL_ONLY_HAN.includes(char), char).toBe(false);
  });

  it(`先頭の ${CONTENT_LANG_SCAN_LIMIT} 字だけを読む`, () => {
    expect(detectContentLang(`${"a".repeat(CONTENT_LANG_SCAN_LIMIT)}${ZH_HANS}`)).toBeUndefined();
  });
});

describe("normalizeContentLang / resolveContentLang", () => {
  it("取込の言語の値を :lang() の token の形にそろえる", () => {
    expect(normalizeContentLang("zh-CN")).toBe("zh-Hans");
    expect(normalizeContentLang("zh_SG")).toBe("zh-Hans");
    expect(normalizeContentLang("zh-TW")).toBe("zh-Hant");
    expect(normalizeContentLang("zh-Hant-HK")).toBe("zh-HK");
    expect(normalizeContentLang("zh-HK")).toBe("zh-HK");
    expect(normalizeContentLang("zh-hans")).toBe("zh-Hans");
    expect(normalizeContentLang("ja-JP")).toBe("ja");
    expect(normalizeContentLang("en")).toBe("en");
    expect(normalizeContentLang("zh")).toBeUndefined();
  });

  it("値があればそれを使い、null なら付けず、渡さなければ本文から判定する", () => {
    expect(resolveContentLang("zh-TW", JA)).toBe("zh-Hant");
    expect(resolveContentLang(null, ZH_HANS)).toBeUndefined();
    expect(resolveContentLang(undefined, ZH_HANS)).toBe("zh-Hans");
    expect(resolveContentLang("zh", ZH_HANT)).toBe("zh-Hant");
    expect(resolveContentLang("zh", JA)).toBe("zh");
    expect(resolveContentLang("  ", ZH_HANS)).toBe("zh-Hans");
  });
});

const evidence = (overrides: Partial<EvidenceListItem> = {}): EvidenceListItem => ({
  key: "e1",
  fileName: "文書 A",
  text: ZH_HANS,
  usedInAnswer: null,
  answerUse: null,
  score: null,
  rerankScore: null,
  ...overrides,
});

function html(node: ReactNode): HTMLElement {
  const host = document.createElement("div");
  host.innerHTML = renderToStaticMarkup(node);
  return host;
}

const langOf = (host: HTMLElement, selector: string) => host.querySelector(selector)?.getAttribute("lang") ?? null;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("共有の部品は利用者の内容に lang を付ける", () => {
  it("MarkdownText・ExcerptContent は本文から判定し、lang の値・null を優先する", () => {
    expect(langOf(html(<MarkdownText text={ZH_HANS} />), "[data-markdown-text]")).toBe("zh-Hans");
    expect(langOf(html(<MarkdownText text={ZH_HANS} lang={null} />), "[data-markdown-text]")).toBeNull();
    expect(langOf(html(<MarkdownText text={JA} lang="zh-TW" />), "[data-markdown-text]")).toBe("zh-Hant");
    expect(langOf(html(<ExcerptContent text={ZH_HANT} testId="excerpt" />), '[data-testid="excerpt"] p')).toBe("zh-Hant");
  });

  it("判定できない本文には lang を付けない（画面の言語のまま）", () => {
    expect(langOf(html(<MarkdownText text="東京都港区" />), "[data-markdown-text]")).toBeNull();
  });

  it("質問の吹き出し（ChatUserMessage・ChatTurn）", () => {
    expect(langOf(html(<ChatUserMessage>{ZH_HANS}</ChatUserMessage>), "[lang]")).toBe("zh-Hans");
    expect(langOf(html(<ChatTurn question={JA} />), "[lang]")).toBe("ja");
    expect(langOf(html(<ChatTurn question={<span>{ZH_HANS}</span>} />), "[lang]")).toBeNull();
    expect(langOf(html(<ChatTurn question={<span>{ZH_HANS}</span>} questionLang="zh-Hant" />), "[lang]")).toBe("zh-Hant");
  });

  it("回答の本文（AnswerSections）は段落・項目の文から判定する", () => {
    const host = html(
      <AnswerSections
        blocks={[
          { kind: "paragraph", key: "p", content: "要点" },
          { kind: "section", key: "s", title: "要点", entries: [{ key: "e", content: <b>x</b>, passageText: ZH_HANS }] },
        ]}
      />
    );
    // 段落・項目の文に付ける（入れ物には付けない。#3349）。
    expect(langOf(host, '[data-testid="answer-text"]')).toBeNull();
    expect(langOf(host, '[data-testid="answer-text"] > p')).toBe("zh-Hans");
    expect(langOf(host, "li span")).toBe("zh-Hans");
  });

  it("節に分けない回答の本文（AnswerPlainText）", () => {
    expect(langOf(html(<AnswerPlainText text={ZH_HANT} testId="plain" />), '[data-testid="plain"]')).toBe("zh-Hant");
    expect(langOf(html(<AnswerPlainText text={ZH_HANT} lang={null} testId="plain" />), '[data-testid="plain"]')).toBeNull();
  });

  it("根拠の抜粋（EvidenceCard）は item.lang か本文から判定する", () => {
    const card = (item: EvidenceListItem) =>
      html(
        <ul>
          <EvidenceCard item={item} index={0} />
        </ul>
      );
    expect(langOf(card(evidence()), '[data-testid="citation-text"]')).toBe("zh-Hans");
    expect(langOf(card(evidence({ lang: "zh-HK" })), '[data-testid="citation-text"]')).toBe("zh-HK");
    expect(langOf(card(evidence({ text: JA })), '[data-testid="citation-text"]')).toBe("ja");
  });

  it("文書のプレビューの本文（DocumentPreview の text）", async () => {
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    await act(async () => {
      root.render(<DocumentPreview kind="text" fileName="a.txt" loadText={async () => ZH_HANT} textKey="a" />);
    });
    expect(langOf(host, '[data-testid="preview-text-body"]')).toBe("zh-Hant");
    // 領域（読み上げの名前を持つ要素）には付けない（#3349）。
    expect(langOf(host, '[data-testid="preview-text"]')).toBeNull();
    await act(async () => root.unmount());
    host.remove();
  });
});

/** 要素から最も近い `lang`（無ければ null。画面の言語 `<html lang>` を継承する）。 */
const nearestLang = (element: Element | null | undefined) => element?.closest("[lang]")?.getAttribute("lang") ?? null;

function byText(host: HTMLElement, text: string): Element {
  const found = [...host.querySelectorAll("*")].filter((element) => element.textContent === text);
  // 同じ文字を持つ要素のうち、最も内側の要素。
  const inner = found.find((element) => ![...element.children].some((child) => child.textContent === text));
  if (!inner) throw new Error(`見つかりません: ${text}`);
  return inner;
}

describe("部品・製品の文言は内容の lang を継承しない（#3349）", () => {
  const passages: AnswerPassageRecords = {
    marks: [{ text: ZH_HANS, state: "unverified", reasons: [{ code: "unsupported", label: "合成の理由", detail: "" }] }],
    removed: [{ text: ZH_HANT, reasons: [{ code: "contradicted", label: "合成の除いた理由", detail: "" }] }],
    warnings: [],
  };

  it("AnswerSections の見出し・根拠の行・ボタン・extra・決まった文言は画面の言語、内容は回答の言語", () => {
    const entries = Array.from({ length: 12 }, (_, index) => ({
      key: `e${index}`,
      content: `${ZH_HANS}${index}`,
      citations: [{ key: `c${index}`, label: `根拠：合成の資料${index} p.1` }],
      extra: index === 0 ? <span>章節の原文を開く</span> : undefined,
    }));
    const host = html(
      <AnswerSections
        blocks={[
          { kind: "paragraph", key: "p", content: ZH_HANS, citations: [{ key: "c", label: "根拠：合成の資料 p.2" }] },
          { kind: "paragraph", key: "fixed", content: "資料の記載を以下に示します。", lang: null },
          { kind: "content", key: "t", content: <div>{ZH_HANT}</div> },
          {
            kind: "section",
            key: "s",
            title: "資料により確認できた事項",
            collapsible: true,
            entries: [{ key: "note", content: "以下は合成の説明です。", muted: true, lang: null }, ...entries],
          },
          { kind: "section", key: "zh", title: "这些示例说明了什么", entries: [{ key: "z", content: ZH_HANS }] },
        ]}
        passages={passages}
      />
    );
    // 内容は回答の言語。
    expect(nearestLang(byText(host, ZH_HANS))).toBe("zh-Hans");
    expect(nearestLang(byText(host, `${ZH_HANS}0`))).toBe("zh-Hans");
    expect(nearestLang(byText(host, ZH_HANT))).toBe("zh-Hans");
    // 部品・製品の文言は lang を継承しない（画面の言語）。
    for (const text of [
      "根拠：合成の資料 p.2",
      "根拠：合成の資料0 p.1",
      "資料により確認できた事項",
      "資料の記載を以下に示します。",
      "以下は合成の説明です。",
      "章節の原文を開く",
      "すべて表示（全 12 件）",
      "確認が必要な点",
      "根拠と矛盾し除いた内容",
      "合成の除いた理由",
    ]) {
      // 付けないか、見出しの文字から判定した日本語（画面の言語と同じ）。
      expect([null, "ja"], text).toContain(nearestLang(byText(host, text)));
    }
    // 回答の見出しは見出しの文字の言語。
    expect(nearestLang(byText(host, "这些示例说明了什么"))).toBe("zh-Hans");
    // 印の付いた段落の引用・外した段落の引用は回答の言語、印のボタンは画面の言語。
    const marked = host.querySelector('[data-testid="answer-marked-passage"]');
    expect(nearestLang(marked?.querySelector("[lang]"))).toBe("zh-Hans");
    expect(nearestLang(host.querySelector('[data-testid="answer-passage-mark-toggle"]'))).toBeNull();
    expect(nearestLang(host.querySelector('[data-testid="answer-removed-passage"] [lang]'))).toBe("zh-Hans");
  });

  it("AnswerPlainText の後ろに置くもの（生成中のカーソルなど）は画面の言語", () => {
    const host = html(
      <AnswerPlainText text={ZH_HANS} testId="plain">
        <span>生成中</span>
      </AnswerPlainText>
    );
    expect(nearestLang(byText(host, ZH_HANS))).toBe("zh-Hans");
    expect(nearestLang(byText(host, "生成中"))).toBeNull();
  });

  it("MarkdownText のリンクの読み上げの補足は画面の言語", () => {
    document.documentElement.lang = "ja";
    const host = html(<MarkdownText text={`${ZH_HANS} [示例](https://example.com/)`} />);
    expect(nearestLang(host.querySelector("a"))).toBe("zh-Hans");
    expect(nearestLang(byText(host, "（新しいタブで開きます）"))).toBe("ja");
  });

  it("ExcerptContent の表のセルは内容の言語、表の読み上げの名前は画面の言語", () => {
    const table = `<table><tr><th>列一</th><th>列二</th></tr><tr><td>这是值</td><td>说明</td></tr></table>`;
    const host = html(<ExcerptContent text={`${ZH_HANS}
${table}`} tableLabel="抜粋の表" testId="excerpt" />);
    expect(nearestLang(host.querySelector('[data-testid="excerpt"] p'))).toBe("zh-Hans");
    expect(nearestLang(byText(host, "这是值"))).toBe("zh-Hans");
    expect(nearestLang(byText(host, "列一"))).toBe("zh-Hans");
    expect(nearestLang(host.querySelector('[aria-label="抜粋の表"]'))).toBeNull();
  });

  it("EvidenceCard の抜粋は内容の言語、ファイル名などの項目は画面の言語", () => {
    const host = html(
      <ul>
        <EvidenceCard item={evidence({ fileName: "合成の資料.txt" })} index={0} />
      </ul>
    );
    expect(nearestLang(host.querySelector('[data-testid="citation-text"]'))).toBe("zh-Hans");
    expect(nearestLang(byText(host, "合成の資料.txt"))).toBeNull();
  });

  it("ChatTurn の送った人・失敗の文言は質問の言語を継承しない", () => {
    const host = html(<ChatTurn question={ZH_HANS} questionStatus="failed" failedLabel="送信に失敗しました" />);
    expect(nearestLang(byText(host, ZH_HANS))).toBe("zh-Hans");
    expect(nearestLang(byText(host, "送信に失敗しました"))).toBeNull();
  });
});

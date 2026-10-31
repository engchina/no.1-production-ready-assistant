import { readFileSync } from "node:fs";

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  MessageText,
  normalizeMessageText,
  segmentMessageText,
} from "../src/components/ui/message-text";

describe("MessageText", () => {
  it("改行・タブ・連続空白を prose 向けに正規化する", () => {
    expect(normalizeMessageText("  abc。ghn\n\txyz  ")).toBe("abc。ghn xyz");
  });

  it("日本語・中国語・英語の強い句読点で分節する", () => {
    expect(segmentMessageText("最新です。変更はありません！続行します？注意；完了。"))
      .toEqual([
        { text: "最新です。", separatorBefore: "" },
        { text: "変更はありません！", separatorBefore: "" },
        { text: "続行します？", separatorBefore: "" },
        { text: "注意；", separatorBefore: "" },
        { text: "完了。", separatorBefore: "" },
      ]);
    expect(segmentMessageText("Saved. No changes! Continue?", "en")).toEqual([
      { text: "Saved.", separatorBefore: "" },
      { text: "No changes!", separatorBefore: " " },
      { text: "Continue?", separatorBefore: " " },
    ]);
  });

  it.each(["「設定」を開いてください。", "「\u2060設定」を開いてください。", "（設定）を開いてください。"])("開き括弧を次の文と同じ箱にする: %s", (next) => {
    expect(segmentMessageText("保存しました。" + next).map((s) => s.text)).toEqual(["保存しました。", next]);
  });

  it("閉じ括弧は前の文に残す", () => {
    expect(segmentMessageText("「保存しました。」次へ。").map((s) => s.text)).toEqual(["「保存しました。」", "次へ。"]);
  });

  it.each(["「", "保存しました。「", "「\u2060"])("開き括弧で終わっても文字を失わない: %s", (text) => {
    expect(segmentMessageText(text).map((segment) => segment.text).join("")).toBe(text);
  });

  it("小数・バージョン・URL 内の句点を分割しない", () => {
    const text = "Version 1.2 is current. See https://example.com/docs. Done.";
    expect(segmentMessageText(text, "en").map((segment) => segment.text)).toEqual([
      "Version 1.2 is current.",
      "See https://example.com/docs.",
      "Done.",
    ]);
  });

  it("連続句読点と単独の長文を空セグメントなしで保持する", () => {
    expect(segmentMessageText("本当！？次です。").map((segment) => segment.text)).toEqual([
      "本当！？",
      "次です。",
    ]);
    expect(segmentMessageText("a".repeat(300))).toEqual([
      { text: "a".repeat(300), separatorBefore: "" },
    ]);
    expect(segmentMessageText(" \n\t ")).toEqual([]);
  });

  it("原文を欠落させず文ごとの inline box を描画する", () => {
    const html = renderToStaticMarkup(
      <MessageText text={"システムテーブルは最新です。変更はありません。"} />
    );
    expect(html).toContain("data-message-text");
    expect(html.match(/data-message-sentence/g)).toHaveLength(2);
    expect(html).toContain("システムテーブルは最新です。</span><span");
    expect(html).toContain("変更はありません。");
  });

  it("半角の文末は空白か末尾が続くときだけ区切り、記号の長い連続でも時間内に終わる（#1646）", () => {
    expect(segmentMessageText("v3.14 を使う!! 次へ。").map((segment) => segment.text)).toEqual([
      "v3.14 を使う!!",
      "次へ。",
    ]);
    // 以前の正規表現は `!` の連続の各位置から読み直して O(n²) になった（20 万文字で数十秒）。
    const started = performance.now();
    const segments = segmentMessageText(`${"!".repeat(200_000)}a`);
    expect(performance.now() - started).toBeLessThan(1000);
    expect(segments.map((segment) => segment.text).join("")).toBe(`${"!".repeat(200_000)}a`);
  });

  it("日本語を文節で折り返す指定（.pr-message-text）を本文に付ける（#899）", () => {
    const html = renderToStaticMarkup(<MessageText text={"Oracle Profile の反映が完了しました。"} />);
    expect(html).toMatch(/class="pr-message-text[^"]*"[^>]*data-message-text/);
  });
});

// #899: 短い 1 文でも語の途中（「完了しま / した。」）で折り返さないよう、メッセージの部品の本文は文節で折り返す。
describe(".pr-message-text", () => {
  const read = (path: string) => readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8");

  it("文節での折り返しと、未対応のブラウザ・入りきらない文節の劣化を共有の CSS で決める", () => {
    const css = read("styles/tokens/base.css");
    const rule = css.match(/\.pr-message-text \{([^}]*)\}/)?.[1] ?? "";
    // 未対応のブラウザは auto-phrase を捨てて直前の normal に残る（宣言の順が要る）。
    expect(rule).toMatch(/word-break: normal;\s*word-break: auto-phrase;/);
    expect(rule).toContain("overflow-wrap: anywhere;");
    expect(rule).toContain("text-wrap: pretty;");
    // 本文（body）全体には入れない（表・チップなどの狭い面で文節がはみ出すため）。
    expect(css).toMatch(/body \{\s*line-break: strict;\s*word-break: normal;\s*overflow-wrap: normal;\s*\}/);
  });

  it.each([
    "components/ui/toast.tsx",
    "components/ui/banner.tsx",
    "components/ui/form-status.tsx",
    "components/feedback/processing-state.tsx",
    "components/feedback/blocked-page-notice.tsx",
  ])("%s の本文の面に付ける", (path) => {
    expect(read(path)).toContain("pr-message-text");
  });
});

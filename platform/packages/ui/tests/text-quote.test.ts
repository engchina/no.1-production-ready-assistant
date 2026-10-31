import { describe, expect, it } from "vitest";

import { findTextQuote } from "../src";

// #2512: テキストのプレビューで、引用（根拠の抜粋）の位置を探す。内容はすべて合成のデータ。

const slice = (text: string, quote: string) => {
  const range = findTextQuote(text, quote);
  return range ? text.slice(range.start, range.end) : null;
};

describe("findTextQuote", () => {
  it("空白・改行・記号・全角半角・大文字小文字の違いを無視して全体を見つける", () => {
    const text = "前置き。\n合成の 項目Ａ は、\n値 12 を持つ。\n後書き。";
    expect(slice(text, "合成の項目aは、値１２を持つ。")).toBe("合成の 項目Ａ は、\n値 12 を持つ");
  });

  it("全体が無ければ、抜粋の行が本文の中で近くに順に並ぶまとまりを強調する（JSON の値だけの抜粋など）", () => {
    const text = [
      "{",
      '  "items": [',
      '    { "title": "合成の見出し X", "body": "合成の本文の一行目です。" },',
      '    { "title": "合成の見出し Y", "body": "合成の本文の二行目です。" },',
      '    { "title": "合成の見出し Z", "body": "別の本文です。" }',
      "  ]",
      "}",
    ].join("\n");
    const quote = "合成の見出し X\n合成の本文の一行目です。\n合成の見出し Y\n合成の本文の二行目です。";
    const found = slice(text, quote)!;
    expect(found.startsWith("合成の見出し X")).toBe(true);
    expect(found.endsWith("合成の本文の二行目です")).toBe(true);
    expect(found).not.toContain("見出し Z");
  });

  it("最初の行が本文の別の所にも当たるときは、多くの行が並ぶまとまりを選ぶ", () => {
    const text = "共通の見出し\n関係の無い段落。\n".repeat(3) + "共通の見出し\n探している一行目。\n探している二行目。\n";
    const range = findTextQuote(text, "共通の見出し\n探している一行目。\n探している二行目。")!;
    expect(text.slice(range.start, range.end)).toBe("共通の見出し\n探している一行目。\n探している二行目");
  });

  it("見つからない・抜粋が短すぎる・空なら null", () => {
    expect(findTextQuote("合成の本文です。", "まったく別の文章です。")).toBeNull();
    expect(findTextQuote("合成の本文です。", "本文")).toBeNull();
    expect(findTextQuote("合成の本文です。", "")).toBeNull();
    expect(findTextQuote("", "合成の本文です。")).toBeNull();
    expect(findTextQuote("合成の本文です。", null)).toBeNull();
  });
});

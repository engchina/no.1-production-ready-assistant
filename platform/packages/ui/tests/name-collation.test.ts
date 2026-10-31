import { describe, expect, it } from "vitest";
import { compareNames } from "../src/lib/name-collation";

describe("名前の並び（#2753）", () => {
  it("記号 → 数字 → ラテン文字 → かな → 漢字 の順に並べる", () => {
    const names = ["漢字", "あいう", "Beta", "10_b", "#tag", "_tmp", "alpha", "カキク"];
    expect([...names].sort((a, b) => compareNames(a, b))).toEqual([
      "_tmp",
      "#tag",
      "10_b",
      "alpha",
      "Beta",
      "あいう",
      "カキク",
      "漢字",
    ]);
  });

  it("数を数値として比べる", () => {
    const names = ["item_10", "item_2", "item_1", "20_a", "3_a", "100_a"];
    expect([...names].sort((a, b) => compareNames(a, b))).toEqual([
      "3_a",
      "20_a",
      "100_a",
      "item_1",
      "item_2",
      "item_10",
    ]);
  });

  it("大文字 / 小文字・全角 / 半角・ひらがな / カタカナを同じ文字として並べる", () => {
    expect([...["b", "A", "c"]].sort((a, b) => compareNames(a, b))).toEqual(["A", "b", "c"]);
    expect(compareNames("ＡＢＣ", "abd")).toBeLessThan(0);
    expect(compareNames("あい", "アウ")).toBeLessThan(0);
  });

  it("同じ文字でも表記が違えば順を決め、同じ入力は毎回同じ並びにする", () => {
    expect(compareNames("abc", "ABC")).not.toBe(0);
    expect(compareNames("abc", "ABC")).toBe(-compareNames("ABC", "abc"));
    expect(compareNames("same", "same")).toBe(0);
  });

  it("desc なら逆順にする", () => {
    expect([...["a", "c", "b"]].sort((a, b) => compareNames(a, b, "desc"))).toEqual(["c", "b", "a"]);
  });
});

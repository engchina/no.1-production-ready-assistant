import { describe, expect, it } from "vitest";

import { labelForMessage, numberFieldError, parseJsonField, requiredSelectError, requiredTextError } from "./field-validation";

// 送信前の検証の規則と文言の型（UX 契約 messaging.md §3.2.1。#540 / #541）。

describe("labelForMessage", () => {
  it("英数字・閉じ括弧で終わるラベルだけ、助詞の前に空白を入れる", () => {
    expect(labelForMessage("Workspace root")).toBe("Workspace root ");
    expect(labelForMessage("上限 (MB)")).toBe("上限 (MB) ");
    expect(labelForMessage("名前")).toBe("名前");
  });
});

describe("requiredTextError / requiredSelectError", () => {
  it("空白だけの入力は未入力として扱う", () => {
    expect(requiredTextError("  ", "名前")).toBe("名前を入力してください。");
    expect(requiredTextError(" a ", "名前")).toBeNull();
  });

  it("選ぶ欄は「選択してください」の文言", () => {
    expect(requiredSelectError("", "モデル")).toBe("モデルを選択してください。");
    expect(requiredSelectError("m1", "モデル")).toBeNull();
  });
});

describe("numberFieldError", () => {
  // 空の数値を 0 で保存しない（#540）。
  it("空は 0 として扱わず未入力にする", () => {
    expect(numberFieldError("", { label: "上限", min: 0 })).toBe("上限を入力してください。");
  });

  it("範囲の内側は null、境界は含む", () => {
    expect(numberFieldError("1", { label: "回数", integer: true, min: 1, max: 10 })).toBeNull();
    expect(numberFieldError("10", { label: "回数", integer: true, min: 1, max: 10 })).toBeNull();
  });

  it("範囲の外・整数でない値は、規則に合わせた文言を返す", () => {
    expect(numberFieldError("11", { label: "回数", integer: true, min: 1, max: 10 })).toBe(
      "回数は 1 以上 10 以下の整数を入力してください。"
    );
    expect(numberFieldError("1.5", { label: "回数", integer: true, min: 1 })).toBe("回数は 1 以上の整数を入力してください。");
    expect(numberFieldError("abc", { label: "温度" })).toBe("温度は数値で入力してください。");
    expect(numberFieldError("-1", { label: "温度", min: 0, max: 2 })).toBe("温度は 0 以上 2 以下の数値を入力してください。");
  });

  it("exclusiveMin は下限を含まない（0 より大きい）", () => {
    expect(numberFieldError("0", { label: "予算", min: 0, exclusiveMin: true })).toBe("予算は 0 より大きい数値を入力してください。");
    expect(numberFieldError("0.01", { label: "予算", min: 0, exclusiveMin: true })).toBeNull();
    expect(numberFieldError("6", { label: "予算", min: 0, exclusiveMin: true, max: 5 })).toBe(
      "予算は 0 より大きく 5 以下の数値を入力してください。"
    );
  });

  it("Infinity は数値として受け付けない", () => {
    expect(numberFieldError("Infinity", { label: "温度" })).toBe("温度は数値で入力してください。");
  });
});

describe("parseJsonField", () => {
  it("空は required のときだけエラー、任意なら値なし", () => {
    expect(parseJsonField(" ", "引数", { required: true })).toEqual({ ok: false, error: "引数を入力してください。" });
    expect(parseJsonField("", "引数")).toEqual({ ok: true, value: undefined });
  });

  it("解析できない JSON・期待した形でない JSON はエラー", () => {
    expect(parseJsonField("{", "引数")).toEqual({ ok: false, error: "引数は有効な JSON で入力してください。" });
    expect(parseJsonField("{}", "一覧", { expect: "array" })).toEqual({ ok: false, error: "一覧は JSON の配列で入力してください。" });
    expect(parseJsonField("[]", "設定", { expect: "object" })).toEqual({
      ok: false,
      error: "設定は JSON のオブジェクトで入力してください。",
    });
    expect(parseJsonField("null", "設定", { expect: "object" })).toEqual({
      ok: false,
      error: "設定は JSON のオブジェクトで入力してください。",
    });
  });

  it("正しい JSON は解析した値を返す", () => {
    expect(parseJsonField<{ a: number }>('{"a":1}', "設定", { expect: "object" })).toEqual({ ok: true, value: { a: 1 } });
  });
});

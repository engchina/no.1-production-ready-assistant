import { describe, expect, it } from "vitest";

import { firstInvalidFieldId, requiredTextError } from "./required-fields";

describe("必須の入力欄（検索・回答プロファイル・ナレッジベースの名前と説明）", () => {
  it("空・空白だけ（全角空白・改行を含む）は未入力として文言を返す", () => {
    expect(requiredTextError("", "説明を入力してください。")).toBe("説明を入力してください。");
    expect(requiredTextError(" 　\n\t", "説明を入力してください。")).toBe("説明を入力してください。");
  });

  it("文字があれば null を返す", () => {
    expect(requiredTextError(" 経理規程 ", "説明を入力してください。")).toBeNull();
  });

  it("最初にエラーのある欄の ID を欄の並び順で返す", () => {
    expect(
      firstInvalidFieldId([
        ["name", null],
        ["description", "説明を入力してください。"],
        ["scope", "1 件以上選んでください。"],
      ])
    ).toBe("description");
    expect(
      firstInvalidFieldId([
        ["name", undefined],
        ["description", null],
      ])
    ).toBeNull();
  });
});

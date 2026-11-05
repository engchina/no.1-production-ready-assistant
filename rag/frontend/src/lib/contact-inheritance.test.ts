import { describe, expect, it } from "vitest";

import { contactInheritancePlaceholder } from "@/components/search-answer-profiles/ContactInheritanceHelper";
import { profileEffectiveContactText } from "@/components/search-answer-profiles/ContactGuidanceFieldset";

import { normalizeContact, profileContactInheritance } from "./contact-inheritance";

const SYSTEM = { enabled: true, contact: "窓口 A" };
const DISABLED = { enabled: false, contact: "" };

// #2683: 窓口の案内の継承（システム設定 → 検索・回答プロファイル）と有効な窓口。
describe("検索・回答プロファイルの窓口の上書き", () => {
  it("空ならシステム設定の窓口を継承し、placeholder と有効な窓口に出す", () => {
    const inheritance = profileContactInheritance("", SYSTEM);
    expect(inheritance.effective).toEqual({ source: "system", contact: "窓口 A" });
    expect(contactInheritancePlaceholder(inheritance)).toBe("未設定（窓口 A を継承）");
    expect(profileEffectiveContactText(inheritance)).toBe("有効な窓口: 窓口 A（システム設定の窓口を継承）");
    expect(inheritance.sameAs).toBeNull();
  });

  it("上書きがあれば上書きが有効な窓口になる", () => {
    const inheritance = profileContactInheritance("窓口 B", SYSTEM);
    expect(profileEffectiveContactText(inheritance)).toBe("有効な窓口: 窓口 B（このプロファイルの上書き）");
    expect(inheritance.sameAs).toBeNull();
  });

  it("システム設定と同じ値なら、空にすると継承する案内を出す（空白の違いは同じとみなす）", () => {
    expect(profileContactInheritance(" 窓口  A ", SYSTEM).sameAs).toBe("system");
  });

  it("回答の注意書きが無効なら、上書きがあっても窓口の案内は出ない", () => {
    const inheritance = profileContactInheritance("窓口 B", DISABLED);
    expect(inheritance.effective).toEqual({ source: "none", contact: "" });
    expect(contactInheritancePlaceholder(inheritance)).toBe("未設定（窓口の案内なし）");
    expect(profileEffectiveContactText(inheritance)).toBe(
      "有効な窓口: なし（システム設定で回答の注意書きが無効のため、窓口の案内を出しません）"
    );
  });

  it("システム設定を読めるまでは placeholder と有効な窓口を出さない", () => {
    const inheritance = profileContactInheritance("", undefined);
    expect(contactInheritancePlaceholder(inheritance)).toBeUndefined();
    expect(profileEffectiveContactText(inheritance)).toBeNull();
  });
});

describe("normalizeContact", () => {
  it("改行・連続の空白を 1 つの空白にそろえる", () => {
    expect(normalizeContact(" 窓口\n A ")).toBe("窓口 A");
    expect(normalizeContact(null)).toBe("");
  });
});

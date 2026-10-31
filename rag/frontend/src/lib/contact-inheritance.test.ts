import { describe, expect, it } from "vitest";

import { contactInheritancePlaceholder } from "@/components/search-answer-profiles/ContactInheritanceHelper";
import { profileEffectiveContactText } from "@/components/search-answer-profiles/ContactGuidanceFieldset";
import { guideEffectiveContactText } from "@/components/search-answer-profiles/SupportGuideHandoffContactField";

import { guideContactInheritance, normalizeContact, profileContactInheritance } from "./contact-inheritance";

const SYSTEM = { enabled: true, contact: "窓口 A" };
const DISABLED = { enabled: false, contact: "" };

// #2683: 窓口の案内の継承（システム設定 → 検索・回答プロファイル → 業務ガイド）と有効な窓口。
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

describe("業務ガイドの引き継ぎ先", () => {
  it("空ならプロファイルの窓口の上書きを継承する", () => {
    const inheritance = guideContactInheritance("", "窓口 B", SYSTEM);
    expect(contactInheritancePlaceholder(inheritance)).toBe("未設定（窓口 B を継承）");
    expect(guideEffectiveContactText(inheritance)).toBe(
      "引き継ぎのときの窓口: 窓口 B（プロファイルの窓口の上書きを継承）。回答の本文は「担当の窓口」と案内し、回答の注意書きでこの窓口を案内します。"
    );
  });

  it("プロファイルの上書きも空ならシステム設定の窓口を継承する", () => {
    const inheritance = guideContactInheritance("", "", SYSTEM);
    expect(inheritance.inherited).toEqual({ source: "system", contact: "窓口 A" });
    expect(guideEffectiveContactText(inheritance)).toContain("窓口 A（システム設定の窓口を継承）");
  });

  it("ガイドの窓口があれば本文で案内し、注意書きには窓口を出さない", () => {
    const inheritance = guideContactInheritance("窓口 C", "窓口 B", SYSTEM);
    expect(guideEffectiveContactText(inheritance)).toBe(
      "引き継ぎのときの窓口: 窓口 C（この業務ガイド）。回答の本文でこの窓口を案内し、回答の注意書きには窓口を出しません。"
    );
    expect(inheritance.sameAs).toBeNull();
  });

  it("継承する窓口と同じ値なら、その出どころを示す", () => {
    expect(guideContactInheritance("窓口 B", "窓口 B", SYSTEM).sameAs).toBe("profile");
    expect(guideContactInheritance("窓口 A", "", SYSTEM).sameAs).toBe("system");
    // プロファイルの上書きがあれば、システム設定と同じ値でも重複ではない（上書きを打ち消している）。
    expect(guideContactInheritance("窓口 A", "窓口 B", SYSTEM).sameAs).toBeNull();
  });

  it("回答の注意書きが無効でも、ガイドの窓口は本文で案内する", () => {
    expect(guideContactInheritance("窓口 C", "窓口 B", DISABLED).effective).toEqual({
      source: "guide",
      contact: "窓口 C",
    });
    const empty = guideContactInheritance("", "窓口 B", DISABLED);
    expect(empty.effective).toEqual({ source: "none", contact: "" });
    expect(guideEffectiveContactText(empty)).toBe(
      "引き継ぎのときの窓口: なし。回答の本文は「担当の窓口」と案内します（システム設定で回答の注意書きが無効のため、窓口の案内を出しません）。"
    );
  });
});

describe("normalizeContact", () => {
  it("改行・連続の空白を 1 つの空白にそろえる", () => {
    expect(normalizeContact(" 窓口\n A ")).toBe("窓口 A");
    expect(normalizeContact(null)).toBe("");
  });
});

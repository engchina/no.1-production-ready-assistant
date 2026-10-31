import { describe, expect, it } from "vitest";

import { isImeComposing, isSubmitEnter } from "./keyboard";

describe("isSubmitEnter", () => {
  it("通常の Enter は送信する", () => {
    expect(isSubmitEnter({ key: "Enter", keyCode: 13, nativeEvent: { isComposing: false } })).toBe(
      true
    );
  });

  it("IME の変換を確定する Enter では送信しない", () => {
    // Chrome / Firefox: 変換中の keydown は isComposing=true。
    expect(isSubmitEnter({ key: "Enter", keyCode: 229, nativeEvent: { isComposing: true } })).toBe(
      false
    );
    // Safari: 確定の Enter は compositionend の後に isComposing=false・keyCode=229 で届く。
    expect(isSubmitEnter({ key: "Enter", keyCode: 229, nativeEvent: { isComposing: false } })).toBe(
      false
    );
    expect(isImeComposing({ key: "Enter", isComposing: true })).toBe(true);
  });

  it("Enter 以外のキーでは送信しない", () => {
    expect(isSubmitEnter({ key: "a", keyCode: 65 })).toBe(false);
  });
});

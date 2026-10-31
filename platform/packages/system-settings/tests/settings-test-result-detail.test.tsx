// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";
import { SettingsTestResultPanel } from "../src/oci/SettingsTestResultPanel";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("設定テストのエラー本文", () => {
  it.each([null, "", "   "])("本文が %j なら空の詳細を出さない", (rawError) => {
    const host = document.createElement("div");
    const root = createRoot(host);
    try {
      act(() => root.render(<SettingsTestResultPanel tone="danger" message="失敗しました。" rawError={rawError} />));
      expect(host.textContent).not.toContain("エラーの詳細");
      expect(host.querySelector("details")).toBeNull();
    } finally { act(() => root.unmount()); }
  });

  it("伏せた本文は HTML として実行せず文字として詳細に出す", () => {
    const host = document.createElement("div");
    const root = createRoot(host);
    const rawError = 'HTTP 400: output token limit exceeded\n<secret> <script>alert(1)</script>';
    try {
      act(() => root.render(<SettingsTestResultPanel tone="danger" message="失敗しました。" errorType="HTTPStatusError" rawError={rawError} />));
      expect(host.textContent).toContain("HTTPStatusError");
      expect(host.textContent).toContain(rawError);
      expect(host.querySelector("script")).toBeNull();
      expect(host.querySelector("details")?.open).toBe(true);
    } finally { act(() => root.unmount()); }
  });
});

// @vitest-environment happy-dom
import { FileKey } from "lucide-react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PreconditionBanner } from "../src";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe("PreconditionBanner（#1688）", () => {
  it("warning の Banner（role=status）に、何が足りないかと次の行動を出し、id で操作に結べる", () => {
    const html = renderToStaticMarkup(
      <PreconditionBanner
        id="save-precondition"
        testId="precondition"
        title="Select AI Credential が作成されていません"
        message="保存には Select AI Credential が必要です。"
      />,
    );
    expect(html).toContain('id="save-precondition"');
    expect(html).toContain('data-testid="precondition"');
    expect(html).toContain('role="status"');
    expect(html).toContain('data-surface-tint="warning"');
    expect(html).toContain("Select AI Credential が作成されていません");
    // 導線も再試行も渡さなければ操作の行を描かない（閉じる × も付けない）。
    expect(html).not.toContain("<a ");
    expect(html).not.toContain("<button");
  });

  it("導線（ButtonLink・移動先のアイコン・ArrowRight）→ 再試行の順に並べる", () => {
    const retry = vi.fn();
    act(() =>
      root.render(
        <PreconditionBanner
          title="Select AI Credential が作成されていません"
          message="本文"
          settingsLink={{
            to: "/settings/select-ai-credential",
            label: "Select AI Credential を開く",
            icon: FileKey,
            testId: "open-settings",
          }}
          onRetry={retry}
          retryTestId="retry"
        />,
      ),
    );
    const controls = [...host.querySelectorAll("a, button")];
    expect(controls.map((element) => element.textContent)).toEqual([
      "Select AI Credential を開く",
      "再試行",
    ]);
    const link = host.querySelector('[data-testid="open-settings"]');
    expect(link?.getAttribute("href")).toBe("/settings/select-ai-credential");
    // 先頭アイコンと方向のアイコン（ArrowRight）の 2 つ。
    expect(link?.querySelectorAll("svg")).toHaveLength(2);
    act(() => (host.querySelector('[data-testid="retry"]') as HTMLButtonElement).click());
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("再試行の処理中は押したボタンだけを loading にする", () => {
    act(() =>
      root.render(
        <PreconditionBanner
          title="t"
          message="m"
          onRetry={() => undefined}
          retrying
          retryLabel="もう一度確かめる"
          retryTestId="retry"
        />,
      ),
    );
    const button = host.querySelector('[data-testid="retry"]');
    expect(button?.getAttribute("aria-busy")).toBe("true");
    expect(button?.textContent).toBe("もう一度確かめる");
  });
});

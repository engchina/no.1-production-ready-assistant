// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  FeedbackControls,
  isSameFeedback,
  type FeedbackControlsLabels,
  type FeedbackControlsProps,
  type FeedbackControlsSubmission,
} from "../src";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const labels: FeedbackControlsLabels = {
  question: "この回答は役に立ちましたか？",
  helpful: "役に立った",
  notHelpful: "役に立たなかった",
  savedInline: "保存済み・変更できます",
  reasonLegend: "役に立たなかった理由",
  commentLabel: "コメント",
  commentCount: (count, max) => `${count}/${max}文字`,
  correctedAnswerLabel: "修正した回答",
  save: "フィードバックを保存",
  cancel: "キャンセル",
  retry: "再試行",
  saveError: "保存できませんでした。",
};

type Reason = "incorrect" | "incomplete";
const reasons = [
  { value: "incorrect", label: "内容が間違っている" },
  { value: "incomplete", label: "情報が足りない" },
] as const;

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

function render(props: Partial<FeedbackControlsProps<Reason>> = {}) {
  const onSubmit = props.onSubmit ?? vi.fn(async () => undefined);
  act(() =>
    root.render(
      <FeedbackControls<Reason>
        value={null}
        reasons={reasons}
        labels={labels}
        data-testid="feedback"
        {...props}
        onSubmit={onSubmit}
      />
    )
  );
  return { onSubmit };
}

function button(name: string): HTMLButtonElement {
  const found = Array.from(host.querySelectorAll<HTMLButtonElement>("button")).find(
    (item) => item.getAttribute("aria-label") === name || item.textContent?.trim() === name
  );
  if (!found) throw new Error(`button not found: ${name}`);
  return found;
}

async function click(target: HTMLElement) {
  await act(async () => {
    target.click();
  });
}

async function type(target: HTMLTextAreaElement, value: string) {
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    setter?.call(target, value);
    target.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

describe("FeedbackControls（#805）", () => {
  it("問いとボタンの組を出し、保存済みでなければ「保存済み」を出さない", () => {
    render();
    const group = host.querySelector("[role=group]");
    expect(group?.getAttribute("aria-label")).toBe(labels.question);
    expect(button(labels.helpful).getAttribute("aria-pressed")).toBe("false");
    expect(host.textContent).not.toContain(labels.savedInline);
    expect(host.querySelector("[data-testid=feedback]")?.className).toContain("border-t");
  });

  it("「役に立った」はすぐ保存し、理由・コメントを持たない", async () => {
    const { onSubmit } = render();
    await click(button(labels.helpful));
    expect(onSubmit).toHaveBeenCalledWith({
      rating: "helpful",
      reason: null,
      comment: null,
      correctedAnswer: null,
    } satisfies FeedbackControlsSubmission<Reason>);
  });

  it("保存済みの評価は押された状態と「保存済み」で出し、同じ評価は送り直さない", async () => {
    const { onSubmit } = render({ value: { rating: "helpful", comment: "" } });
    expect(button(labels.helpful).getAttribute("aria-pressed")).toBe("true");
    expect(button(labels.helpful).className).toContain("text-success-fg");
    expect(host.textContent).toContain(labels.savedInline);
    await click(button(labels.helpful));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("「役に立たなかった」は理由（必須）を選んでから、整えたコメントと一緒に保存し、閉じる", async () => {
    const { onSubmit } = render({ commentId: "fb-comment" });
    await click(button(labels.notHelpful));
    expect(button(labels.notHelpful).getAttribute("aria-expanded")).toBe("true");
    expect(host.querySelector("legend")?.textContent).toContain("必須");
    expect(button(labels.save).disabled).toBe(true);
    await click(button("情報が足りない"));
    const comment = host.querySelector<HTMLTextAreaElement>("#fb-comment")!;
    await type(comment, "  手順が抜けている  ");
    expect(host.textContent).toContain("12/1000文字");
    await click(button(labels.save));
    expect(onSubmit).toHaveBeenCalledWith({
      rating: "not_helpful",
      reason: "incomplete",
      comment: "手順が抜けている",
      correctedAnswer: null,
    });
    expect(host.querySelector("fieldset")).toBeNull();
  });

  it("修正した回答の欄は correctedAnswer のときだけ出す", async () => {
    render();
    await click(button(labels.notHelpful));
    expect(host.textContent).not.toContain(labels.correctedAnswerLabel);
    render({ correctedAnswer: true, correctedAnswerId: "fb-corrected" });
    expect(host.querySelector("#fb-corrected")).not.toBeNull();
  });

  it("失敗は role=alert の文言と「再試行」を出し、再試行は同じ内容を送る", async () => {
    const onSubmit = vi
      .fn<(submission: FeedbackControlsSubmission<Reason>) => Promise<unknown>>()
      .mockRejectedValueOnce(new Error("サーバーに接続できません。"))
      .mockResolvedValueOnce(undefined);
    render({ onSubmit, getErrorMessage: (error) => (error instanceof Error ? error.message : null) });
    await click(button(labels.helpful));
    const alert = host.querySelector("[role=alert]");
    expect(alert?.textContent).toContain("サーバーに接続できません。");
    await click(button(labels.retry));
    expect(onSubmit).toHaveBeenCalledTimes(2);
    expect(onSubmit.mock.calls[1]?.[0]).toEqual(onSubmit.mock.calls[0]?.[0]);
    expect(host.querySelector("[role=alert]")).toBeNull();
  });

  it("getErrorMessage が文言を返さなければ既定の文言を出す", async () => {
    render({ onSubmit: vi.fn(async () => Promise.reject(new Error(""))) });
    await click(button(labels.helpful));
    expect(host.querySelector("[role=alert]")?.textContent).toContain(labels.saveError);
  });

  it("保存中は押したボタンだけが loading になり、ほかは押せない", async () => {
    let resolve: () => void = () => undefined;
    const onSubmit = vi.fn(() => new Promise<void>((done) => (resolve = done)));
    render({ onSubmit });
    await click(button(labels.helpful));
    expect(button(labels.helpful).getAttribute("aria-busy")).toBe("true");
    expect(button(labels.notHelpful).disabled).toBe(true);
    expect(button(labels.notHelpful).getAttribute("aria-busy")).toBeNull();
    await act(async () => resolve());
    expect(button(labels.notHelpful).disabled).toBe(false);
  });

  it("compact は問いを読み上げだけにし、保存済みの表示と区切り線を出さない", () => {
    render({ compact: true, value: { rating: "not_helpful", reason: "incorrect" } });
    expect(host.querySelector(".sr-only")?.textContent).toBe(labels.question);
    expect(host.textContent).not.toContain(labels.savedInline);
    expect(host.querySelector("[data-testid=feedback]")?.className).not.toContain("border-t");
  });

  it("readOnly は押せず、保存済みの理由とコメントを文で出す", async () => {
    const { onSubmit } = render({
      readOnly: true,
      value: { rating: "not_helpful", reason: "incorrect", comment: "数字が違う" },
    });
    expect(button(labels.notHelpful).disabled).toBe(true);
    expect(button(labels.notHelpful).getAttribute("aria-pressed")).toBe("true");
    expect(host.textContent).toContain("内容が間違っている");
    expect(host.textContent).toContain("数字が違う");
    expect(host.textContent).not.toContain(labels.savedInline);
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

describe("isSameFeedback", () => {
  it("コメント・修正した回答は空白を除いて比べ、空文字と null を同じとみなす", () => {
    const submission = { rating: "not_helpful", reason: "incorrect", comment: null, correctedAnswer: null } as const;
    expect(isSameFeedback({ rating: "not_helpful", reason: "incorrect", comment: "  " }, submission)).toBe(true);
    expect(isSameFeedback({ rating: "not_helpful", reason: "incomplete" }, submission)).toBe(false);
    expect(isSameFeedback(null, submission)).toBe(false);
  });
});

// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  ChatAnswer,
  ChatPendingTurn,
  ChatTurn,
  createOptimisticChatMessage,
  withOptimisticChatStatus,
} from "../src";

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

describe("ChatTurn / ChatAnswer", () => {
  it("1 往復は article に質問の吹き出しと回答の入れ物を並べる", () => {
    act(() =>
      root.render(
        <ChatTurn question="売上は？" testId="turn">
          <ChatAnswer live id="message-1" testId="answer">
            <p>回答</p>
          </ChatAnswer>
        </ChatTurn>
      )
    );
    const turn = host.querySelector('[data-testid="turn"]')!;
    expect(turn.tagName).toBe("ARTICLE");
    expect(turn.querySelector('[data-status="sent"]')!.textContent).toBe("売上は？");
    const answer = host.querySelector('[data-testid="answer"]')!;
    expect(answer.id).toBe("message-1");
    expect(answer.getAttribute("aria-live")).toBe("polite");
    expect(answer.className).toContain("border-border");
    expect(answer.textContent).toBe("回答");
  });

  it("live を渡さない回答の入れ物は読み上げの領域を持たない", () => {
    act(() => root.render(<ChatAnswer testId="answer">回答</ChatAnswer>));
    expect(host.querySelector('[data-testid="answer"]')!.hasAttribute("aria-live")).toBe(false);
  });
});

describe("ChatPendingTurn", () => {
  const sending = createOptimisticChatMessage("在庫は？", 1000);

  it("送信中は質問と、回答の入れ物の中の処理の段階を出す", () => {
    act(() =>
      root.render(
        <ChatPendingTurn
          message={sending}
          failedLabel="送信できませんでした"
          progress={<p data-testid="progress">質問を送信しています</p>}
          failure={<p data-testid="failure">失敗</p>}
          testId="pending"
        />
      )
    );
    expect(host.querySelector('[data-status="sending"]')!.textContent).toBe("在庫は？");
    expect(host.querySelector('[data-testid="progress"]')!.closest("div.border-border")).not.toBeNull();
    expect(host.querySelector('[data-testid="failure"]')).toBeNull();
  });

  it("送れなかったら質問を残したまま、状態の文と原因・再送信を出す", () => {
    act(() =>
      root.render(
        <ChatPendingTurn
          message={withOptimisticChatStatus(sending, "failed")}
          failedLabel="送信できませんでした"
          progress={<p data-testid="progress">質問を送信しています</p>}
          failure={<p data-testid="failure">失敗</p>}
        />
      )
    );
    const question = host.querySelector('[data-status="failed"]')!;
    expect(question.textContent).toContain("在庫は？");
    expect(question.textContent).toContain("送信できませんでした");
    expect(host.querySelector('[data-testid="failure"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="progress"]')).toBeNull();
  });
});

describe("ChatTurn の送った人（#1928）", () => {
  it("質問の吹き出しと、吹き出しの代わりの利用者の側の表示の上に送った人を出す", () => {
    act(() => {
      root.render(
        <>
          <ChatTurn question="質問" questionSender="管理者 が送信" testId="bubble" />
          <ChatTurn userContent={<p>答えた内容</p>} questionSender="管理者 が送信" testId="record" />
          <ChatTurn question="持ち主の質問" testId="owner" />
        </>
      );
    });
    for (const id of ["bubble", "record"]) {
      const turn = host.querySelector(`[data-testid="${id}"]`);
      const sender = turn?.querySelector("[data-chat-sender]");
      expect(sender?.textContent).toBe("管理者 が送信");
    }
    expect(host.querySelector('[data-testid="owner"] [data-chat-sender]')).toBeNull();
  });
});

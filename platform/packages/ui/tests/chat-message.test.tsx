import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  ChatUserMessage,
  createOptimisticChatMessage,
  withOptimisticChatStatus,
} from "../src";

describe("createOptimisticChatMessage（#907）", () => {
  it("送った本文を送信中の仮のメッセージにし、画面の中だけで一意な ID を付ける", () => {
    const first = createOptimisticChatMessage("経費の上限は？", 1_000);
    const second = createOptimisticChatMessage("経費の上限は？", 1_000);
    expect(first).toMatchObject({ content: "経費の上限は？", status: "sending", sentAtMs: 1_000 });
    expect(first.localId).toMatch(/^local-/);
    expect(second.localId).not.toBe(first.localId);
  });

  it("失敗・停止では送信の時刻を保ち、再送信では数え直す", () => {
    const message = createOptimisticChatMessage("質問", 1_000);
    const failed = withOptimisticChatStatus(message, "failed", 5_000);
    expect(failed).toMatchObject({ localId: message.localId, status: "failed", sentAtMs: 1_000 });
    expect(withOptimisticChatStatus(failed, "stopped", 6_000).sentAtMs).toBe(1_000);
    expect(withOptimisticChatStatus(failed, "sending", 9_000)).toMatchObject({
      localId: message.localId,
      status: "sending",
      sentAtMs: 9_000,
    });
  });
});

describe("ChatUserMessage（#907）", () => {
  it("送信中も送信後と同じ吹き出しで出し、状態は data-status に出す", () => {
    const html = renderToStaticMarkup(
      <ChatUserMessage status="sending" testId="bubble">
        {"1 行目\n2 行目"}
      </ChatUserMessage>
    );
    expect(html).toContain('data-status="sending"');
    expect(html).toContain('data-testid="bubble"');
    expect(html).toContain("whitespace-pre-wrap");
    expect(html).toContain("1 行目\n2 行目");
    expect(html).not.toContain("text-danger-fg");
  });

  it("送信できなかったときは吹き出しを残し、下にアイコン付きの状態の文を出す", () => {
    const html = renderToStaticMarkup(
      <ChatUserMessage status="failed" failedLabel="送信できませんでした">
        質問
      </ChatUserMessage>
    );
    expect(html).toContain("質問");
    expect(html).toContain("送信できませんでした");
    expect(html).toContain("text-danger-fg");
    expect(html).toContain('aria-hidden="true"');
  });

  it("持ち主以外が送った質問は、吹き出しの上に送った人を文で出す（#1928）", () => {
    const html = renderToStaticMarkup(
      <ChatUserMessage senderLabel="システム管理者 が送信">質問</ChatUserMessage>
    );
    expect(html).toContain("data-chat-sender");
    expect(html).toContain("システム管理者 が送信");
    expect(html.indexOf("システム管理者 が送信")).toBeLessThan(html.indexOf("質問"));
    expect(renderToStaticMarkup(<ChatUserMessage>質問</ChatUserMessage>)).not.toContain(
      "data-chat-sender"
    );
  });
});

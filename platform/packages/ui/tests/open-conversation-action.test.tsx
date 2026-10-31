import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { OpenConversationAction } from "../src";

describe("OpenConversationAction（#2343）", () => {
  it("会話を開ける実行だけ、共通の文言と読み上げ名のリンクで出す", () => {
    const html = renderToStaticMarkup(<OpenConversationAction href="/chat?conversation_id=c1" testId="open" />);
    expect(html).toContain('href="/chat?conversation_id=c1"');
    expect(html).toContain("会話を開く");
    expect(html).toContain('aria-label="この実行の会話をチャットで開く"');
    expect(html).toContain('data-testid="open"');
  });

  it("開く元の会話が無い実行（チャットでない・見られない）は何も描かない", () => {
    expect(renderToStaticMarkup(<OpenConversationAction href={null} />)).toBe("");
    expect(renderToStaticMarkup(<OpenConversationAction href="" />)).toBe("");
  });
});

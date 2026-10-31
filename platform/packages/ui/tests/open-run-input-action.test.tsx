import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { OpenConversationAction, OpenRunInputAction } from "../src";

const LABELS = { label: "画面 A で開く", ariaLabel: "この実行の質問と条件を画面 A に入れて開く" };

describe("OpenRunInputAction（#3309）", () => {
  it("本人の実行だけ、製品が渡す文言と読み上げ名のリンクで出す", () => {
    const html = renderToStaticMarkup(
      <OpenRunInputAction href="/search?question=q1" labels={LABELS} testId="open-input" />
    );
    expect(html).toContain('href="/search?question=q1"');
    expect(html).toContain("画面 A で開く");
    expect(html).toContain('aria-label="この実行の質問と条件を画面 A に入れて開く"');
    expect(html).toContain('data-testid="open-input"');
  });

  it("開けない実行（本人でない・画面の権限が無い）は何も描かない", () => {
    expect(renderToStaticMarkup(<OpenRunInputAction href={null} labels={LABELS} />)).toBe("");
    expect(renderToStaticMarkup(<OpenRunInputAction href="" labels={LABELS} />)).toBe("");
  });

  it("「会話を開く」と同じボタンの見た目で、アイコンだけが違う", () => {
    const input = renderToStaticMarkup(<OpenRunInputAction href="/a" labels={LABELS} />);
    const conversation = renderToStaticMarkup(<OpenConversationAction href="/a" />);
    const classOf = (html: string) => /<a[^>]*class="([^"]*)"/.exec(html)?.[1];
    expect(classOf(input)).toBe(classOf(conversation));
    expect(input).toContain("lucide-square-pen");
    expect(conversation).not.toContain("lucide-square-pen");
  });
});

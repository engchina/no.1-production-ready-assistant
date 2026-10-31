import { Database } from "lucide-react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { BlockedPageNotice } from "../src";

describe("BlockedPageNotice", () => {
  it("見出しの id でカードに名前を付け、本文・補足・操作・区切りの補足を順に出す", () => {
    const html = renderToStaticMarkup(
      <BlockedPageNotice
        title="データベースを起動してください"
        titleId="database-unavailable-title"
        icon={Database}
        message="接続先を確認してください。"
        details={<p>診断コード: missing</p>}
        actions={<button type="button">再試行</button>}
        footer="設定ページは引き続き利用できます。"
        testId="notice"
      />
    );
    expect(html).toContain('data-testid="notice"');
    expect(html).toMatch(/<section[^>]*aria-labelledby="database-unavailable-title"/);
    expect(html).toMatch(/<h1 id="database-unavailable-title"[^>]*>データベースを起動してください<\/h1>/);
    const order = ["接続先を確認", "診断コード: missing", ">再試行<", "設定ページは引き続き"].map((text) =>
      html.indexOf(text)
    );
    expect(order.every((index) => index >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
  });

  it("アイコンは装飾として読み上げず、トーンの色はトークンのユーティリティで付ける", () => {
    const warning = renderToStaticMarkup(<BlockedPageNotice title="t" titleId="t" icon={Database} />);
    expect(warning).toMatch(/class="[^"]*bg-warning-subtle text-warning-fg[^"]*" aria-hidden="true"/);
    const info = renderToStaticMarkup(<BlockedPageNotice title="t" titleId="t" icon={Database} tone="info" />);
    expect(info).toContain("bg-info-subtle text-info-fg");
    expect(warning).not.toMatch(/#[0-9a-f]{3,6}\b/i);
  });

  it("本文・補足・操作・区切りの補足は、渡したものだけを描く（空の面を出さない）", () => {
    const html = renderToStaticMarkup(<BlockedPageNotice title="t" titleId="t" icon={Database} />);
    expect(html).not.toMatch(/<p[ >]/);
    expect(html).not.toContain("border-t");
  });
});

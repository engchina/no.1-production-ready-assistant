import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { LockKeyhole } from "lucide-react";
import { describe, expect, it } from "vitest";

import { ButtonLink } from "../src";

function FakeRouterLink({
  to,
  className,
  children,
  ...props
}: {
  to: string;
  className?: string;
  children: ReactNode;
  "aria-label"?: string;
  "data-testid"?: string;
}) {
  return (
    <a href={`#router:${to}`} className={className} data-router-link="" {...props}>
      {children}
    </a>
  );
}

describe("ButtonLink（#800）", () => {
  it("Button と同じ見た目のリンクを描き、アイコンは icon から 16px で aria-hidden にする", () => {
    const html = renderToStaticMarkup(
      <ButtonLink to="/security/permissions?role=r1" size="sm" icon={LockKeyhole} testId="open-permissions">
        権限管理で設定
      </ButtonLink>
    );
    expect(html).toMatch(/^<a href="\/security\/permissions\?role=r1"/);
    // 既定は secondary（移動は主操作にしない）。
    expect(html).toContain("border-border-control");
    expect(html).toContain("h-[var(--button-height-sm)]");
    expect(html).toContain('data-testid="open-permissions"');
    expect(html).toMatch(/<svg[^>]*width="16"[^>]*aria-hidden="true"/);
    expect(html).toContain("<span>権限管理で設定</span>");
  });

  it("linkComponent を渡すとルーターのリンクで描く", () => {
    const html = renderToStaticMarkup(
      <ButtonLink to="/next" linkComponent={FakeRouterLink} variant="primary" aria-label="次の画面へ移動">
        次へ
      </ButtonLink>
    );
    expect(html).toContain('href="#router:/next"');
    expect(html).toContain("data-router-link");
    expect(html).toContain('aria-label="次の画面へ移動"');
    expect(html).toContain("bg-accent-emphasis");
  });
});

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AppearanceSettingsPage } from "../src/appearance/AppearanceSettingsPage";

describe("AppearanceSettingsPage", () => {
  it("3択を aria-pressed で表示し、現在の選好だけを押下状態にする", () => {
    const html = renderToStaticMarkup(<AppearanceSettingsPage theme="dark" onThemeChange={() => undefined} />);
    expect(html).toContain('data-testid="appearance-theme-toggle"');
    expect(html).toContain('aria-label="配色テーマ"');
    expect(html.match(/aria-pressed="true"/g)).toHaveLength(1);
    expect(html).toMatch(/aria-pressed="true"[^>]*>ダーク</);
    expect(html).toContain("自動（OS 設定）");
  });

  it("文言は messages で部分的に上書きできる", () => {
    const html = renderToStaticMarkup(
      <AppearanceSettingsPage theme="light" onThemeChange={() => undefined} messages={{ title: "Appearance" }} />,
    );
    expect(html).toContain("Appearance");
    expect(html).toContain("配色テーマ");
  });
});

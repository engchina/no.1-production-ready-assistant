import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AppearanceSettingsPage } from "../src/appearance/AppearanceSettingsPage";

describe("AppearanceSettingsPage", () => {
  it("配色テーマの選択を出さない（#17。配色はライトの 1 種類だけ）", () => {
    const html = renderToStaticMarkup(<AppearanceSettingsPage />);
    expect(html).not.toContain('data-testid="appearance-theme-toggle"');
    expect(html).not.toContain("配色テーマ");
    expect(html).not.toContain("ダーク");
    expect(html).not.toContain("aria-pressed");
    expect(html).toContain("HTTPS の証明書");
  });

  it("文言は messages で部分的に上書きできる", () => {
    const html = renderToStaticMarkup(<AppearanceSettingsPage messages={{ title: "Appearance" }} />);
    expect(html).toContain("Appearance");
  });
});

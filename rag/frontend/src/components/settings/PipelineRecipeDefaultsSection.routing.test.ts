import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { PipelineRecipeDefaultsSection } from "./PipelineRecipeDefaultsSection";

// このテストはリンクの解決を対象とし、共有の操作部品のイベントは E2E で確認する。
vi.mock("@production-ready/ui", async (importOriginal) => ({
  ...await importOriginal<typeof import("@production-ready/ui")>(),
  FormActionBar: () => null,
  Switch: () => null,
}));
vi.mock("@/components/security/AuthProvider", () => ({ useAuth: () => ({ hasPermission: () => true }) }));
vi.mock("@/lib/leave-guard", () => ({ useLeaveGuard: () => undefined }));
vi.mock("@/lib/queries", () => ({
  usePipelineSettings: () => ({ isPending: false, isError: false, data: {
    auto_parse_after_preprocess_enabled: true,
    auto_chunk_after_extract_enabled: true,
    auto_index_after_chunk_enabled: true,
    recipe_defaults: {},
    vector_index_profile: "accurate",
  } }),
  useUpdatePipelineSettings: () => ({ isPending: false, isError: false }),
}));

describe("設定概要の配備基点", () => {
  for (const basename of ["/", "/rag"]) {
    it(`${basename} の全設定リンクを Router が解決し hash を保持する`, () => {
      const path = basename === "/" ? "" : basename;
      const html = renderToStaticMarkup(
        createElement(MemoryRouter, { basename, initialEntries: [`${path}/settings/pipeline`] },
          createElement(PipelineRecipeDefaultsSection))
      );
      const hrefs = [...html.matchAll(/href="([^"]+)"/g)].map((match) => match[1]);
      expect(hrefs).toHaveLength(11);
      expect(hrefs.every((href) => href.startsWith(`${path}/settings/`))).toBe(true);
      expect(hrefs).toContain(`${path}/settings/parser-adapters#post-parse-navigation-summary`);
      expect(hrefs).toContain(`${path}/settings/vector-index`);
      expect(hrefs).toContain(`${path}/settings/entity-index`);
    });
  }
});

import { describe, expect, it } from "vitest";

import { PARSER_VALUES } from "@/components/documents/DocumentProcessingConfigPanel.values";
import { SERVICE_PROFILE_ORDER } from "@/components/settings/ServicesManagementClient";

import { PARSER_BACKEND_ORDER, sortParserBackends } from "./parser-backend-order";

describe("文書解析エンジンの並び", () => {
  it("Docling → Unstructured の順", () => {
    expect([...PARSER_BACKEND_ORDER]).toEqual(["docling", "unstructured"]);
  });

  it("処理レシピの選択肢も同じ正本の順", () => {
    expect([...PARSER_VALUES]).toEqual([...PARSER_BACKEND_ORDER]);
  });

  it("サービス管理の節も CPU → GPU", () => {
    expect(SERVICE_PROFILE_ORDER).toEqual(["cpu", "gpu"]);
  });

  it("画面のカードの並びを正本の順にそろえ、知らないエンジンは後ろに残す", () => {
    expect(sortParserBackends(["local", "unstructured", "docling"])).toEqual([
      "docling",
      "unstructured",
      "local",
    ]);
  });
});

import { describe, expect, it } from "vitest";

import {
  ENTITY_ATTRIBUTE_COLUMNS_MAX,
  ENTITY_NAME_COLUMNS_MAX,
  entityColumnsError,
  entityColumnsSummary,
} from "./EntityIndexColumnsRow";

// #1388: 文書レシピの「実体・別名索引の列」。
describe("実体・別名索引の列", () => {
  it("継承の値を、空欄は「列名で決める」として要約する", () => {
    expect(entityColumnsSummary(null)).toBe("名前の列: 列名で決める · 属性の列: 列名で決める");
    expect(
      entityColumnsSummary({
        entity_name_columns: ["ID", "正式名"],
        entity_attribute_columns: ["担当部署"],
      })
    ).toBe("名前の列: ID、正式名 · 属性の列: 担当部署");
  });

  it("backend の検証（数・長さの上限）と同じ所を欄の下に示す", () => {
    expect(ENTITY_NAME_COLUMNS_MAX).toBe(20);
    expect(ENTITY_ATTRIBUTE_COLUMNS_MAX).toBe(40);
    expect(entityColumnsError(["ID"], ENTITY_NAME_COLUMNS_MAX)).toBeUndefined();
    expect(
      entityColumnsError(
        Array.from({ length: 21 }, (_, index) => `列${index}`),
        ENTITY_NAME_COLUMNS_MAX
      )
    ).toBe("列名は 20 個までです。");
    expect(entityColumnsError(["あ".repeat(81)], ENTITY_ATTRIBUTE_COLUMNS_MAX)).toBe(
      "列名は 1 つ 80 文字までです。"
    );
  });
});

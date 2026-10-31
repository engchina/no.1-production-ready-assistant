import { describe, expect, it } from "vitest";

import type { DocumentElement } from "@/lib/api";
import { layoutVisionDetails, elementCropUrl, elementVision } from "./layout-element";

function element(metadata: DocumentElement["metadata"]): DocumentElement {
  return { kind: "figure", text: "", order: 0, metadata } as DocumentElement;
}

describe("elementVision", () => {
  it("Vision の状態と説明を取り出す", () => {
    expect(
      elementVision(element({ vision_status: "succeeded", vision_retrieval_text: "画面説明" }))
    ).toEqual({ status: "succeeded", retrievalText: "画面説明", excluded: false });
  });

  it("装飾画像は除外として扱い、無関係な要素は null", () => {
    expect(elementVision(element({ visual_role: "decorative" }))?.excluded).toBe(true);
    expect(elementVision(element({ category: "Text" }))).toBeNull();
  });
});

describe("layoutVisionDetails", () => {
  const extraction = {
    parser_artifacts: {
      layout_records: {
        records: [
          {
            id: "docling-p1-2",
            raw: {
              vision_description: {
                visual_kind: "screenshot",
                visible_buttons: ["登録", "取消"],
                table_rows: [
                  ["受注番号", "必須"],
                  ["顧客名", "任意"],
                ],
                condition_result_pairs: [{ condition: "未入力", result: "エラー", visible_text: "" }],
                retrieval_text: "検索用",
              },
            },
          },
          { id: "docling-p1-3", raw: { visual_role: "decorative", visual_role_reason: "ロゴ" } },
        ],
      },
    },
  };

  it("値のある Vision 項目だけを整形して返す", () => {
    const details = layoutVisionDetails(extraction, "docling-p1-2");
    expect(details?.lines).toEqual([
      { field: "visual_kind", value: "screenshot" },
      { field: "visible_buttons", value: "登録\n取消" },
      { field: "table_rows", value: "受注番号 | 必須\n顧客名 | 任意" },
      { field: "condition_result_pairs", value: "未入力 → エラー" },
    ]);
  });

  it("装飾画像は除外理由を返し、該当しない要素は null", () => {
    expect(layoutVisionDetails(extraction, "docling-p1-3")?.excludedReason).toBe("ロゴ");
    expect(layoutVisionDetails(extraction, "missing")).toBeNull();
    expect(layoutVisionDetails({}, "docling-p1-2")).toBeNull();
  });
});

describe("elementCropUrl", () => {
  it("bbox とページ寸法から crop API の URL を作る", () => {
    const url = elementCropUrl("doc 1", {
      kind: "figure",
      text: "",
      order: 0,
      page_number: 2,
      bbox: [10, 20, 110, 70],
      metadata: { page_width: 1000, page_height: 1400 },
    } as DocumentElement);
    expect(url).toBe(
      "/api/documents/doc%201/crop?page=2&x0=10&y0=20&x1=110&y1=70&page_width=1000&page_height=1400"
    );
  });

  it("位置情報が足りなければ null", () => {
    expect(
      elementCropUrl("doc-1", { kind: "figure", text: "", order: 0 } as DocumentElement)
    ).toBeNull();
  });
});

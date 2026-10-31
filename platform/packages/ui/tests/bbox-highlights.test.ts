import { describe, expect, it } from "vitest";

import {
  buildPreviewHighlights,
  displayRegionsFromMetadata,
  highlightPages,
  highlightRectsForPage,
  resolveRegionPageSize,
} from "../src/lib/bbox";

// rag_poc の viewer（getImageBoxStyle）と同じく、解析時のページ画像 px（左上原点・[x1, y1, x2, y2]）を
// ページに対する % に直す。実データ（Docling・A4 を 300dpi で描いた 2480x3509 px）の形で確かめる。
const engineMetadata = {
  page_width: 2480,
  page_height: 3509,
  bbox: "[342.6, 392.9, 2127.0, 1247.4]",
  bbox_unit: "absolute",
  engine_metadata_json: JSON.stringify({
    schema_version: 4,
    layout: {
      display_regions: [
        {
          page: 1,
          boxes: [
            {
              record_id: "docling-p1-1",
              seq_no: 1,
              category: "Text",
              bbox: [1518.3, 392.9, 2127.0, 569.6],
              text_preview: "平成24年10月22日",
            },
            { record_id: "docling-p1-2", seq_no: 2, category: "Text", bbox: [715.5, 659.7, 1765.8, 767.4] },
          ],
        },
        {
          page: 2,
          boxes: [{ record_id: "docling-p2-1", seq_no: 1, category: "Table", bbox: [0, 0, 1240, 1754.5] }],
        },
      ],
    },
  }),
};

describe("displayRegionsFromMetadata", () => {
  it("親子階層の chunk の metadata JSON から、ページごとの要素 bbox を取り出す", () => {
    const regions = displayRegionsFromMetadata(engineMetadata);
    expect(regions.map((region) => region.page)).toEqual([1, 2]);
    expect(regions[0].boxes[0]).toEqual({
      recordId: "docling-p1-1",
      seqNo: 1,
      category: "Text",
      bbox: [1518.3, 392.9, 2127.0, 569.6],
      textPreview: "平成24年10月22日",
    });
  });

  it("layout を直接持つ metadata と、壊れた値を扱う", () => {
    expect(
      displayRegionsFromMetadata({
        layout: { display_regions: [{ page: 3, boxes: [{ bbox: [1, 2, 3, 4] }, { bbox: [1, 2] }] }] },
      })
    ).toEqual([
      { page: 3, boxes: [{ recordId: "", seqNo: 0, category: "", bbox: [1, 2, 3, 4], textPreview: null }] },
    ]);
    expect(displayRegionsFromMetadata({ engine_metadata_json: "{broken" })).toEqual([]);
    expect(displayRegionsFromMetadata({ layout: { display_regions: [{ page: 0, boxes: [] }] } })).toEqual(
      []
    );
    expect(displayRegionsFromMetadata(null)).toEqual([]);
  });
});

describe("buildPreviewHighlights", () => {
  it("表示領域があれば要素ごとの bbox を強調し、包含 bbox は重ねない", () => {
    const highlights = buildPreviewHighlights({
      focusPage: 1,
      focusBbox: [342.6, 392.9, 2127.0, 1247.4],
      focusPageSize: { width: 2480, height: 3509 },
      regions: displayRegionsFromMetadata(engineMetadata),
    });
    expect(highlights).toHaveLength(3);
    expect(highlights.every((highlight) => highlight.tone === "primary")).toBe(true);
    expect(highlights.every((highlight) => highlight.unit === "absolute")).toBe(true);
    expect(highlightPages(highlights)).toEqual([1, 2]);

    const rects = highlightRectsForPage(highlights, 1);
    expect(rects).toHaveLength(2);
    // 1518.3 / 2480 = 61.22%、392.9 / 3509 = 11.20%
    expect(rects[0].rect.leftPercent).toBeCloseTo(61.222, 2);
    expect(rects[0].rect.topPercent).toBeCloseTo(11.197, 2);
    expect(rects[0].rect.widthPercent).toBeCloseTo(((2127.0 - 1518.3) / 2480) * 100, 3);
    expect(rects[0].rect.heightPercent).toBeCloseTo(((569.6 - 392.9) / 3509) * 100, 3);
    expect(rects[0].label).toBe("平成24年10月22日");
  });

  it("同じ要素の重複を除く", () => {
    const region = { page: 1, boxes: [{ recordId: "a", seqNo: 1, category: "Text", bbox: [0, 0, 10, 10], textPreview: null }] };
    expect(
      buildPreviewHighlights({ focusPageSize: { width: 100, height: 100 }, regions: [region, region] })
    ).toHaveLength(1);
  });

  it("表示領域が無ければ包含 bbox 1 つを強調する（従来の mode / unit の推定のまま）", () => {
    const highlights = buildPreviewHighlights({
      focusPage: 2,
      focusBbox: [10, 15, 40, 30],
      focusBboxMode: "xywh",
      focusBboxUnit: "percent",
    });
    expect(highlights).toHaveLength(1);
    expect(highlightRectsForPage(highlights, 2)[0].rect).toMatchObject({
      leftPercent: 10,
      topPercent: 15,
      widthPercent: 40,
      heightPercent: 30,
    });
    // 別のページには重ねない。
    expect(highlightRectsForPage(highlights, 1)).toEqual([]);
    expect(buildPreviewHighlights({ focusBbox: null })).toEqual([]);
  });

  it("ページごとの寸法が分かるページはその寸法を基準にする", () => {
    const highlights = buildPreviewHighlights({
      focusPage: 1,
      focusPageSize: { width: 2480, height: 3509 },
      regions: displayRegionsFromMetadata(engineMetadata),
      pageSizeFor: (page) => (page === 2 ? { width: 2480, height: 3509 } : null),
    });
    const page2 = highlightRectsForPage(highlights, 2);
    expect(page2).toHaveLength(1);
    expect(page2[0].rect.widthPercent).toBeCloseTo(50, 3);
    expect(page2[0].rect.heightPercent).toBeCloseTo(50, 3);
  });
});

describe("resolveRegionPageSize", () => {
  it("基準ページと寸法の違うページでは、同じ dpi で描いた前提で比例させる（rag_poc の _restore_record と同じ）", () => {
    // 基準: 100x200 pt のページを 1000x2000 px で解析。表示中: 200x100 pt のページ → 2000x1000 px。
    const size = resolveRegionPageSize(
      { page: 2, pageSize: { width: 1000, height: 2000 }, pageSizePage: 1 },
      2,
      { pagePoints: (page) => (page === 1 ? { width: 100, height: 200 } : { width: 200, height: 100 }) }
    );
    expect(size).toEqual({ width: 2000, height: 1000 });
  });

  it("同じページ・寸法が分からないときは基準の寸法のまま", () => {
    const base = { width: 1000, height: 2000 };
    expect(resolveRegionPageSize({ page: 1, pageSize: base, pageSizePage: 1 }, 1)).toBe(base);
    expect(
      resolveRegionPageSize({ page: 2, pageSize: base, pageSizePage: 1 }, 2, { pagePoints: () => null })
    ).toBe(base);
  });

  it("基準の寸法が無ければ代わりの寸法（画像の実寸 px）を使う", () => {
    expect(
      resolveRegionPageSize({ page: null, pageSize: { rotation: 90 } }, null, {
        fallbackPageSize: { width: 400, height: 300 },
      })
    ).toEqual({ width: 400, height: 300, rotation: 90 });
    const rects = highlightRectsForPage(
      [{ key: "k", page: null, bbox: [100, 30, 300, 150], unit: "absolute", tone: "primary" }],
      null,
      { fallbackPageSize: { width: 400, height: 300 } }
    );
    expect(rects[0].rect).toMatchObject({ leftPercent: 25, topPercent: 10, widthPercent: 50, heightPercent: 40 });
  });
});

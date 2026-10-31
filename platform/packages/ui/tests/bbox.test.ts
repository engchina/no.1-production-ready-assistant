import { describe, expect, it } from "vitest";

import {
  bboxCoordinateModeFromMetadata,
  bboxFromMetadata,
  bboxPageAspectRatio,
  bboxPageRotationFromMetadata,
  bboxPageSizeFromMetadata,
  bboxUnitFromMetadata,
  formatBboxPercent,
  normalizeBboxForPreview,
} from "../src/lib/bbox";

describe("normalizeBboxForPreview", () => {
  it("normalizes ratio xyxy coordinates into preview percentages", () => {
    expect(normalizeBboxForPreview([0.25, 0.25, 0.5, 0.5])).toEqual({
      leftPercent: 25,
      topPercent: 25,
      widthPercent: 25,
      heightPercent: 25,
      unit: "ratio",
      coordinateMode: "xyxy",
    });
  });

  it("normalizes percent xyxy coordinates", () => {
    expect(normalizeBboxForPreview([25, 10, 50, 40])).toEqual({
      leftPercent: 25,
      topPercent: 10,
      widthPercent: 25,
      heightPercent: 30,
      unit: "percent",
      coordinateMode: "xyxy",
    });
  });

  it("keeps xywh coordinates when the third and fourth values are extents", () => {
    expect(normalizeBboxForPreview([0.25, 0.25, 0.2, 0.1])).toEqual({
      leftPercent: 25,
      topPercent: 25,
      widthPercent: 20,
      heightPercent: 10,
      unit: "ratio",
      coordinateMode: "xywh",
    });
  });

  it("clips xywh extents to the visible preview area", () => {
    expect(normalizeBboxForPreview([90, 90, 20, 20])).toEqual({
      leftPercent: 90,
      topPercent: 90,
      widthPercent: 10,
      heightPercent: 10,
      unit: "percent",
      coordinateMode: "xywh",
    });
  });

  it("uses explicit xywh mode instead of the numeric xyxy heuristic", () => {
    expect(normalizeBboxForPreview([25, 10, 50, 40], null, "xywh")).toEqual({
      leftPercent: 25,
      topPercent: 10,
      widthPercent: 50,
      heightPercent: 40,
      unit: "percent",
      coordinateMode: "xywh",
    });
  });

  it("uses explicit ratio unit instead of numeric percent heuristics", () => {
    expect(normalizeBboxForPreview([0.25, 0.1, 0.5, 0.4], null, "xywh", "ratio")).toEqual({
      leftPercent: 25,
      topPercent: 10,
      widthPercent: 50,
      heightPercent: 40,
      unit: "ratio",
      coordinateMode: "xywh",
    });
  });

  it("uses explicit percent unit for sub-1 values when metadata says percent", () => {
    expect(normalizeBboxForPreview([0.25, 0.1, 0.5, 0.4], null, "xywh", "percent")).toEqual({
      leftPercent: 0.25,
      topPercent: 0.1,
      widthPercent: 0.5,
      heightPercent: 0.4,
      unit: "percent",
      coordinateMode: "xywh",
    });
  });

  it("normalizes absolute xyxy coordinates when page dimensions are available", () => {
    expect(normalizeBboxForPreview([153, 198, 306, 396], { width: 612, height: 792 })).toEqual({
      leftPercent: 25,
      topPercent: 25,
      widthPercent: 25,
      heightPercent: 25,
      unit: "absolute",
      coordinateMode: "xyxy",
    });
  });

  it("rotates preview rectangles when page rotation metadata is present", () => {
    expect(
      normalizeBboxForPreview([153, 198, 306, 396], {
        width: 612,
        height: 792,
        rotation: 90,
      })
    ).toEqual({
      leftPercent: 25,
      topPercent: 50,
      widthPercent: 25,
      heightPercent: 25,
      unit: "absolute",
      coordinateMode: "xyxy",
    });
    expect(
      normalizeBboxForPreview([153, 198, 306, 396], {
        width: 612,
        height: 792,
        rotation: 180,
      })
    ).toEqual({
      leftPercent: 50,
      topPercent: 50,
      widthPercent: 25,
      heightPercent: 25,
      unit: "absolute",
      coordinateMode: "xyxy",
    });
    expect(
      normalizeBboxForPreview([153, 198, 306, 396], {
        width: 612,
        height: 792,
        rotation: 270,
      })
    ).toEqual({
      leftPercent: 50,
      topPercent: 25,
      widthPercent: 25,
      heightPercent: 25,
      unit: "absolute",
      coordinateMode: "xyxy",
    });
  });

  it("normalizes flat polygon coordinates into an enclosing preview rectangle", () => {
    expect(
      normalizeBboxForPreview(
        [153, 198, 306, 198, 306, 396, 153, 396],
        { width: 612, height: 792 },
        null,
        "absolute"
      )
    ).toEqual({
      leftPercent: 25,
      topPercent: 25,
      widthPercent: 25,
      heightPercent: 25,
      unit: "absolute",
      coordinateMode: "xyxy",
    });
  });

  it("rejects absolute page coordinates without page dimensions", () => {
    expect(normalizeBboxForPreview([10, 20, 300, 400])).toBeNull();
  });

  it("formats preview percentages with one decimal", () => {
    expect(formatBboxPercent(25)).toBe("25.0");
    expect(formatBboxPercent(12.345)).toBe("12.3");
  });

  it("returns a stable page aspect ratio for locator overlays", () => {
    expect(bboxPageAspectRatio({ width: 612, height: 792 })).toBe("612 / 792");
    expect(bboxPageAspectRatio({ width: 612, height: 792, rotation: 90 })).toBe(
      "792 / 612"
    );
    expect(bboxPageAspectRatio(null)).toBe("1 / 1.414");
  });
});

describe("bboxUnitFromMetadata", () => {
  it("reads supported bbox unit aliases", () => {
    expect(bboxUnitFromMetadata({ bbox_unit: "normalized" })).toBe("ratio");
    expect(bboxUnitFromMetadata({ bbox_coordinate_unit: "percentage" })).toBe("percent");
    expect(bboxUnitFromMetadata({ coordinate_unit: "px" })).toBe("absolute");
    expect(bboxUnitFromMetadata({ unit: "points" })).toBe("absolute");
  });

  it("ignores unknown or non-string bbox unit metadata", () => {
    expect(bboxUnitFromMetadata({ bbox_unit: "page" })).toBeNull();
    expect(bboxUnitFromMetadata({ bbox_unit: true })).toBeNull();
    expect(bboxUnitFromMetadata(null)).toBeNull();
  });
});

describe("bboxCoordinateModeFromMetadata", () => {
  it("reads supported bbox coordinate mode aliases", () => {
    expect(bboxCoordinateModeFromMetadata({ bbox_coordinate_mode: "x,y,width,height" })).toBe(
      "xywh"
    );
    expect(bboxCoordinateModeFromMetadata({ bbox_format: "x1_y1_x2_y2" })).toBe("xyxy");
    expect(bboxCoordinateModeFromMetadata({ coordinate_mode: "left-top-width-height" })).toBe(
      "xywh"
    );
  });

  it("ignores unknown or non-string bbox mode metadata", () => {
    expect(bboxCoordinateModeFromMetadata({ bbox_mode: "polygon" })).toBeNull();
    expect(bboxCoordinateModeFromMetadata({ bbox_mode: true })).toBeNull();
    expect(bboxCoordinateModeFromMetadata(null)).toBeNull();
  });
});

describe("bboxFromMetadata", () => {
  it("reads JSON encoded bbox metadata", () => {
    expect(bboxFromMetadata({ bbox_json: "[10,20,110,220]" })).toEqual([10, 20, 110, 220]);
  });

  it("reads object bbox metadata and infers xywh mode", () => {
    const metadata = { bbox: { x: 10, y: 20, width: 30, height: 40 } };

    expect(bboxFromMetadata(metadata)).toEqual([10, 20, 30, 40]);
    expect(bboxCoordinateModeFromMetadata(metadata)).toBe("xywh");
  });

  it("reads nested polygon points from adapter metadata", () => {
    const metadata = {
      polygon: [
        [153, 198],
        [306, 198],
        [306, 396],
        [153, 396],
      ],
    };

    expect(bboxFromMetadata(metadata)).toEqual([153, 198, 306, 396]);
    expect(bboxCoordinateModeFromMetadata(metadata)).toBe("xyxy");
  });

  it("reads flat polygon coordinates from nested bbox metadata", () => {
    const metadata = {
      bbox: {
        points: [153, 198, 306, 198, 306, 396, 153, 396],
      },
    };

    expect(bboxFromMetadata(metadata)).toEqual([153, 198, 306, 396]);
    expect(bboxCoordinateModeFromMetadata(metadata)).toBe("xyxy");
  });

  it("reads coordinates aliases used by layout parsers", () => {
    expect(
      bboxFromMetadata({
        coordinates: [
          { ignored: true },
          [10, 20],
          [60, 20],
          [60, 45],
          [10, 45],
        ],
      })
    ).toEqual([10, 20, 60, 45]);
  });

  it("reads scalar bbox metadata", () => {
    expect(
      bboxFromMetadata({
        bbox_x1: 10,
        bbox_y1: 20,
        bbox_x2: 110,
        bbox_y2: 220,
      })
    ).toEqual([10, 20, 110, 220]);
  });

  it("ignores malformed bbox metadata", () => {
    expect(bboxFromMetadata({ bbox_json: "not json" })).toBeNull();
    expect(bboxFromMetadata({ bbox: [10, Number.NaN, 30, 40] })).toBeNull();
  });
});

describe("bboxPageSizeFromMetadata", () => {
  it("reads page size metadata for absolute bbox projection", () => {
    expect(
      bboxPageSizeFromMetadata({ page_width: 612, page_height: 792, page_rotation: 90 })
    ).toEqual({
      width: 612,
      height: 792,
      rotation: 90,
    });
  });

  it("reads nested page dimensions", () => {
    expect(bboxPageSizeFromMetadata({ dimensions: { width: "612", height: "792" } })).toEqual({
      width: 612,
      height: 792,
      rotation: null,
    });
  });
});

describe("bboxPageRotationFromMetadata", () => {
  it("reads supported page rotation aliases", () => {
    expect(bboxPageRotationFromMetadata({ page_rotation: 90 })).toBe(90);
    expect(bboxPageRotationFromMetadata({ bbox_page_rotation: "270" })).toBe(270);
    expect(bboxPageRotationFromMetadata({ source_page_rotation: -90 })).toBe(270);
  });

  it("ignores unsupported page rotations", () => {
    expect(bboxPageRotationFromMetadata({ page_rotation: 45 })).toBeNull();
    expect(bboxPageRotationFromMetadata({ page_rotation: 90.5 })).toBeNull();
  });
});

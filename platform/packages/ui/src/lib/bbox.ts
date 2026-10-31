export type BboxOverlayUnit = "ratio" | "percent" | "absolute";
export type BboxCoordinateMode = "xyxy" | "xywh";

export type BboxOverlayRect = {
  leftPercent: number;
  topPercent: number;
  widthPercent: number;
  heightPercent: number;
  unit: BboxOverlayUnit;
  coordinateMode: BboxCoordinateMode;
};

export type BboxPageSize = {
  width?: number | null;
  height?: number | null;
  rotation?: number | null;
};

const MIN_BBOX_VALUES = 4;
const DEFAULT_PAGE_ASPECT_RATIO = "1 / 1.414";
const BBOX_MODE_KEYS = [
  "bbox_mode",
  "bbox_coordinate_mode",
  "bbox_format",
  "coordinate_mode",
] as const;
const BBOX_UNIT_KEYS = [
  "bbox_unit",
  "bbox_coordinate_unit",
  "coordinate_unit",
  "unit",
] as const;
const BBOX_VALUE_KEYS = [
  "bbox",
  "bbox_json",
  "bbox_xyxy",
  "bbox_xywh",
  "bounding_box",
  "boundingBox",
  "polygon",
  "points",
  "coordinates",
] as const;
const PAGE_WIDTH_KEYS = ["page_width", "bbox_page_width", "source_page_width"] as const;
const PAGE_HEIGHT_KEYS = ["page_height", "bbox_page_height", "source_page_height"] as const;
const PAGE_ROTATION_KEYS = [
  "page_rotation",
  "bbox_page_rotation",
  "source_page_rotation",
  "rotation",
] as const;

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, value));
}

function clampExtent(value: number, remainingPercent: number): number {
  return Math.max(0, Math.min(value, remainingPercent));
}

export function formatBboxPercent(value: number): string {
  return value.toFixed(1);
}

export function normalizeBboxForPreview(
  bbox?: number[] | null,
  pageSize?: BboxPageSize | null,
  preferredMode?: BboxCoordinateMode | null,
  preferredUnit?: BboxOverlayUnit | null
): BboxOverlayRect | null {
  if (!bbox || bbox.length < MIN_BBOX_VALUES) return null;

  const values = normalizeBboxValues(bbox);
  if (values.some((value) => !Number.isFinite(value))) return null;

  const max = Math.max(...values);
  const scale = preferredUnit
    ? resolveExplicitScale(preferredUnit, pageSize)
    : resolveScale(max, pageSize);
  if (!scale) return null;

  const rawLeft = values[0] * scale.xFactor;
  const rawTop = values[1] * scale.yFactor;
  const rawRightOrWidth = values[2] * scale.xFactor;
  const rawBottomOrHeight = values[3] * scale.yFactor;
  const leftPercent = clampPercent(rawLeft);
  const topPercent = clampPercent(rawTop);
  const coordinateMode: BboxCoordinateMode =
    preferredMode ??
    (rawRightOrWidth > rawLeft && rawBottomOrHeight > rawTop ? "xyxy" : "xywh");

  const widthPercent =
    coordinateMode === "xyxy"
      ? clampExtent(clampPercent(rawRightOrWidth) - leftPercent, 100 - leftPercent)
      : clampExtent(rawRightOrWidth, 100 - leftPercent);
  const heightPercent =
    coordinateMode === "xyxy"
      ? clampExtent(clampPercent(rawBottomOrHeight) - topPercent, 100 - topPercent)
      : clampExtent(rawBottomOrHeight, 100 - topPercent);

  if (widthPercent <= 0 || heightPercent <= 0) return null;

  return rotateOverlayRect(
    {
      leftPercent,
      topPercent,
      widthPercent,
      heightPercent,
      unit: scale.unit,
      coordinateMode,
    },
    pageSize?.rotation
  );
}

export function bboxCoordinateModeFromMetadata(
  metadata?: Record<string, unknown> | null
): BboxCoordinateMode | null {
  if (!metadata) return null;
  for (const key of BBOX_MODE_KEYS) {
    const value = metadata[key];
    const normalized = typeof value === "string" ? normalizeBboxModeValue(value) : "";
    if (normalized) return normalized;
  }
  if (metadata.bbox_xywh != null) return "xywh";
  if (metadata.bbox_xyxy != null) return "xyxy";
  for (const key of BBOX_VALUE_KEYS) {
    const inferred = inferBboxModeFromValue(metadata[key]);
    if (inferred) return inferred;
  }
  if (metadata.bbox_width != null || metadata.width != null || metadata.w != null) return "xywh";
  if (metadata.bbox_right != null || metadata.right != null || metadata.xmax != null) {
    return "xyxy";
  }
  return null;
}

export function bboxUnitFromMetadata(
  metadata?: Record<string, unknown> | null
): BboxOverlayUnit | null {
  if (!metadata) return null;
  for (const key of BBOX_UNIT_KEYS) {
    const value = metadata[key];
    const normalized = typeof value === "string" ? normalizeBboxUnitValue(value) : "";
    if (normalized) return normalized;
  }
  return null;
}

function rotateOverlayRect(
  rect: BboxOverlayRect,
  rotation?: number | null
): BboxOverlayRect {
  const normalized = normalizePageRotation(rotation);
  if (normalized === 90) {
    return {
      ...rect,
      leftPercent: clampPercent(rect.topPercent),
      topPercent: clampPercent(100 - rect.leftPercent - rect.widthPercent),
      widthPercent: clampExtent(rect.heightPercent, 100 - rect.topPercent),
      heightPercent: clampExtent(rect.widthPercent, rect.leftPercent + rect.widthPercent),
    };
  }
  if (normalized === 180) {
    return {
      ...rect,
      leftPercent: clampPercent(100 - rect.leftPercent - rect.widthPercent),
      topPercent: clampPercent(100 - rect.topPercent - rect.heightPercent),
    };
  }
  if (normalized === 270) {
    return {
      ...rect,
      leftPercent: clampPercent(100 - rect.topPercent - rect.heightPercent),
      topPercent: clampPercent(rect.leftPercent),
      widthPercent: clampExtent(rect.heightPercent, rect.topPercent + rect.heightPercent),
      heightPercent: clampExtent(rect.widthPercent, 100 - rect.leftPercent),
    };
  }
  return rect;
}

export function bboxPageRotationFromMetadata(
  metadata?: Record<string, unknown> | null
): number | null {
  if (!metadata) return null;
  for (const key of PAGE_ROTATION_KEYS) {
    const parsed = normalizePageRotation(metadata[key]);
    if (parsed != null) return parsed;
  }
  return null;
}

export function withBboxPageRotation(
  pageSize: BboxPageSize | null,
  rotation?: number | null
): BboxPageSize | null {
  const normalized = normalizePageRotation(rotation);
  if (!pageSize && normalized == null) return null;
  return { ...(pageSize ?? {}), rotation: normalized };
}

function normalizePageRotation(value: unknown): number | null {
  const parsed = numberValue(value);
  if (parsed == null || !Number.isInteger(parsed)) return null;
  const normalized = ((parsed % 360) + 360) % 360;
  return normalized === 0 || normalized === 90 || normalized === 180 || normalized === 270
    ? normalized
    : null;
}

export function bboxPageAspectRatio(pageSize?: BboxPageSize | null): string {
  const width = Number(
    pageSize?.rotation === 90 || pageSize?.rotation === 270
      ? pageSize?.height
      : pageSize?.width
  );
  const height = Number(
    pageSize?.rotation === 90 || pageSize?.rotation === 270
      ? pageSize?.width
      : pageSize?.height
  );
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return DEFAULT_PAGE_ASPECT_RATIO;
  }
  return `${width} / ${height}`;
}

export function bboxFromMetadata(metadata?: Record<string, unknown> | null): number[] | null {
  if (!metadata) return null;
  for (const key of BBOX_VALUE_KEYS) {
    const parsed = parseBboxValue(metadata[key]);
    if (parsed) return parsed;
  }
  const xyxy = numbersFromMetadata(metadata, [
    ["bbox_x1", "x1", "left", "xmin"],
    ["bbox_y1", "y1", "top", "ymin"],
    ["bbox_x2", "x2", "right", "xmax"],
    ["bbox_y2", "y2", "bottom", "ymax"],
  ]);
  if (xyxy) return xyxy;
  return numbersFromMetadata(metadata, [
    ["bbox_x", "x", "left"],
    ["bbox_y", "y", "top"],
    ["bbox_width", "width", "w"],
    ["bbox_height", "height", "h"],
  ]);
}

export function bboxPageSizeFromMetadata(
  metadata?: Record<string, unknown> | null
): BboxPageSize | null {
  if (!metadata) return null;
  const width = firstNumber(metadata, PAGE_WIDTH_KEYS);
  const height = firstNumber(metadata, PAGE_HEIGHT_KEYS);
  const rotation = bboxPageRotationFromMetadata(metadata);
  if (width && height) return { width, height, rotation };
  const pageSize = recordValue(metadata.page_size ?? metadata.pageSize ?? metadata.dimensions);
  const nestedWidth = numberValue(pageSize?.width);
  const nestedHeight = numberValue(pageSize?.height);
  const nestedRotation = bboxPageRotationFromMetadata(pageSize);
  if (nestedWidth && nestedHeight) {
    return { width: nestedWidth, height: nestedHeight, rotation: nestedRotation ?? rotation };
  }
  return rotation != null ? { rotation } : null;
}

function resolveExplicitScale(
  unit: BboxOverlayUnit,
  pageSize?: BboxPageSize | null
): { xFactor: number; yFactor: number; unit: BboxOverlayUnit } | null {
  if (unit === "ratio") return { xFactor: 100, yFactor: 100, unit };
  if (unit === "percent") return { xFactor: 1, yFactor: 1, unit };
  const width = Number(pageSize?.width);
  const height = Number(pageSize?.height);
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return null;
  }
  return { xFactor: 100 / width, yFactor: 100 / height, unit };
}

function parseBboxValue(value: unknown): number[] | null {
  if (Array.isArray(value)) return arrayBboxValue(value);
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return null;
    if (trimmed.startsWith("[")) {
      try {
        return parseBboxValue(JSON.parse(trimmed));
      } catch {
        return null;
      }
    }
    return numberArrayValue(trimmed.split(/[,\s]+/));
  }
  const source = recordValue(value);
  if (!source) return null;
  const polygon = polygonFromRecord(source);
  if (polygon) return polygon;
  const xyxy = numbersFromMetadata(source, [
    ["x1", "left", "xmin"],
    ["y1", "top", "ymin"],
    ["x2", "right", "xmax"],
    ["y2", "bottom", "ymax"],
  ]);
  if (xyxy) return xyxy;
  return numbersFromMetadata(source, [
    ["x", "left"],
    ["y", "top"],
    ["width", "w"],
    ["height", "h"],
  ]);
}

function inferBboxModeFromValue(value: unknown): BboxCoordinateMode | null {
  if (Array.isArray(value) && polygonArrayValue(value)) return "xyxy";
  const source = recordValue(value);
  if (!source) return null;
  if (polygonFromRecord(source)) return "xyxy";
  if (source.width != null || source.w != null) return "xywh";
  if (source.right != null || source.bottom != null || source.xmax != null || source.ymax != null) {
    return "xyxy";
  }
  return null;
}

function numbersFromMetadata(
  metadata: Record<string, unknown>,
  keyGroups: readonly (readonly string[])[]
): number[] | null {
  const values = keyGroups.map((keys) => firstNumber(metadata, keys));
  return values.every((value): value is number => value != null) ? values : null;
}

function firstNumber(
  metadata: Record<string, unknown>,
  keys: readonly string[]
): number | null {
  for (const key of keys) {
    const parsed = numberValue(metadata[key]);
    if (parsed != null) return parsed;
  }
  return null;
}

function numberArrayValue(value: unknown[]): number[] | null {
  if (value.length < MIN_BBOX_VALUES) return null;
  const numbers = value.slice(0, MIN_BBOX_VALUES).map(numberValue);
  return numbers.every((item): item is number => item != null) ? numbers : null;
}

function arrayBboxValue(value: unknown[]): number[] | null {
  const polygon = polygonArrayValue(value);
  if (polygon) return polygon;
  return numberArrayValue(value);
}

function normalizeBboxValues(value: unknown[]): number[] {
  const polygon = polygonArrayValue(value);
  if (polygon) return polygon;
  return value.slice(0, MIN_BBOX_VALUES).map(Number);
}

function polygonFromRecord(source: Record<string, unknown>): number[] | null {
  for (const key of ["polygon", "points", "coordinates", "vertices"] as const) {
    const value = source[key];
    if (Array.isArray(value)) {
      const polygon = polygonArrayValue(value);
      if (polygon) return polygon;
    }
  }
  return null;
}

function polygonArrayValue(value: unknown[]): number[] | null {
  const points = polygonPoints(value);
  if (points.length < 2) return null;
  const xs = points.map((point) => point[0]);
  const ys = points.map((point) => point[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}

function polygonPoints(value: unknown[]): Array<[number, number]> {
  const nested = value
    .map((item): [number, number] | null => {
      if (!Array.isArray(item) || item.length < 2) return null;
      const x = numberValue(item[0]);
      const y = numberValue(item[1]);
      return x != null && y != null ? [x, y] : null;
    })
    .filter((point): point is [number, number] => point != null);
  if (nested.length >= 2) return nested;

  if (value.length < 8 || value.length % 2 !== 0) return [];
  const flatNumbers = value.map(numberValue);
  if (flatNumbers.some((item) => item == null)) return [];
  const points: Array<[number, number]> = [];
  for (let index = 0; index < flatNumbers.length; index += 2) {
    points.push([flatNumbers[index] as number, flatNumbers[index + 1] as number]);
  }
  return points;
}

function numberValue(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value !== "string") return null;
  const parsed = Number(value.trim());
  return Number.isFinite(parsed) ? parsed : null;
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function resolveScale(
  maxValue: number,
  pageSize?: BboxPageSize | null
): { xFactor: number; yFactor: number; unit: BboxOverlayUnit } | null {
  if (maxValue <= 1) return { xFactor: 100, yFactor: 100, unit: "ratio" };
  if (maxValue <= 100) return { xFactor: 1, yFactor: 1, unit: "percent" };

  const width = Number(pageSize?.width);
  const height = Number(pageSize?.height);
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return null;
  }

  return { xFactor: 100 / width, yFactor: 100 / height, unit: "absolute" };
}

function normalizeBboxModeValue(value: string): BboxCoordinateMode | null {
  const normalized = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  if (normalized === "xyxy" || normalized === "x1_y1_x2_y2") return "xyxy";
  if (
    normalized === "xywh" ||
    normalized === "x_y_width_height" ||
    normalized === "left_top_width_height"
  ) {
    return "xywh";
  }
  return null;
}

function normalizeBboxUnitValue(value: string): BboxOverlayUnit | null {
  const normalized = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9%]+/g, "_")
    .replace(/^_+|_+$/g, "");
  if (
    normalized === "ratio" ||
    normalized === "relative" ||
    normalized === "normalized" ||
    normalized === "fraction"
  ) {
    return "ratio";
  }
  if (normalized === "percent" || normalized === "percentage" || normalized === "%") {
    return "percent";
  }
  if (
    normalized === "absolute" ||
    normalized === "pixel" ||
    normalized === "pixels" ||
    normalized === "px" ||
    normalized === "point" ||
    normalized === "points" ||
    normalized === "pt"
  ) {
    return "absolute";
  }
  return null;
}

// ---- 強調する領域（#349） ----
// 親子階層（small-to-big）の chunk は metadata.engine_metadata_json.layout.display_regions に、根拠にした要素ごとの
// bbox（解析時のページ画像 px・左上原点・[x1, y1, x2, y2]）をページ別に持つ（rag_poc と同じ形式）。
// プレビューでは要素ごとに矩形を重ね、無い chunk だけ包含 bbox 1 つを重ねる。

export type BboxHighlightTone = "primary" | "secondary";

/** プレビューに重ねる 1 つの領域。座標の解釈は normalizeBboxForPreview と同じ。 */
export type PreviewHighlight = {
  key: string;
  /** 1 始まりのページ番号。null はページを持たない（画像 1 枚・フォーカス中のページ）。 */
  page: number | null;
  bbox: number[];
  mode?: BboxCoordinateMode | null;
  unit?: BboxOverlayUnit | null;
  /** bbox の基準にしたページの寸法。unit が absolute のときに必要。 */
  pageSize?: BboxPageSize | null;
  /** pageSize がどのページの寸法か（別のページに重ねるときに比例させる）。null は page と同じ。 */
  pageSizePage?: number | null;
  tone: BboxHighlightTone;
  label?: string | null;
};

export type BboxDisplayRegionBox = {
  recordId: string;
  seqNo: number;
  category: string;
  bbox: number[];
  textPreview: string | null;
};

export type BboxDisplayRegion = { page: number; boxes: BboxDisplayRegionBox[] };

/** chunk metadata から要素の表示領域（ページごとの要素 bbox）を取り出す。 */
export function displayRegionsFromMetadata(
  metadata?: Record<string, unknown> | null
): BboxDisplayRegion[] {
  if (!metadata) return [];
  const layout =
    recordValue(engineMetadata(metadata)?.layout) ?? recordValue(metadata.layout);
  const raw = layout?.display_regions ?? metadata.display_regions;
  if (!Array.isArray(raw)) return [];
  const regions: BboxDisplayRegion[] = [];
  for (const item of raw) {
    const region = recordValue(item);
    const page = numberValue(region?.page);
    if (!region || page == null || !Number.isInteger(page) || page < 1) continue;
    const boxes: BboxDisplayRegionBox[] = [];
    for (const boxItem of Array.isArray(region.boxes) ? region.boxes : []) {
      const box = recordValue(boxItem);
      const bbox = Array.isArray(box?.bbox) ? numberArrayValue(box.bbox) : null;
      if (!box || !bbox) continue;
      boxes.push({
        recordId: typeof box.record_id === "string" ? box.record_id : "",
        seqNo: numberValue(box.seq_no) ?? 0,
        category: typeof box.category === "string" ? box.category : "",
        bbox,
        textPreview: typeof box.text_preview === "string" ? box.text_preview : null,
      });
    }
    if (boxes.length > 0) regions.push({ page, boxes });
  }
  return regions;
}

function engineMetadata(metadata: Record<string, unknown>): Record<string, unknown> | null {
  const raw = metadata.engine_metadata_json;
  if (typeof raw === "string" && raw.trim()) {
    try {
      return recordValue(JSON.parse(raw));
    } catch {
      return null;
    }
  }
  return recordValue(raw);
}

/**
 * フォーカス対象の bbox と表示領域から、プレビューに重ねる領域の一覧を作る。
 *
 * - 表示領域がある（親子階層の chunk）: 要素ごとの bbox をすべて primary にする（rag_poc の子 chunk と同じ）。
 *   座標は解析時のページ画像 px なので unit は absolute、基準寸法は chunk の page_width / page_height。
 *   別のページの寸法は、同じ dpi で描いた前提で基準ページとの表示寸法（pt）の比率から求める（resolveRegionPageSize）。
 * - 無い: 包含 bbox 1 つを primary にする（従来どおり mode / unit を推定する）。
 */
export function buildPreviewHighlights({
  focusPage,
  focusBbox,
  focusBboxMode,
  focusBboxUnit,
  focusPageSize,
  regions = [],
  pageSizeFor,
}: {
  focusPage?: number | null;
  focusBbox?: number[] | null;
  focusBboxMode?: BboxCoordinateMode | null;
  focusBboxUnit?: BboxOverlayUnit | null;
  focusPageSize?: BboxPageSize | null;
  regions?: BboxDisplayRegion[];
  /** ページごとの解析時の寸法（抽出結果の pages）。分かるページはこちらを優先する。 */
  pageSizeFor?: (page: number) => BboxPageSize | null;
}): PreviewHighlight[] {
  const seen = new Set<string>();
  const highlights: PreviewHighlight[] = [];
  for (const region of regions) {
    for (const box of region.boxes) {
      const key = `${region.page}:${box.recordId || box.seqNo}:${box.bbox.join(",")}`;
      if (seen.has(key)) continue;
      seen.add(key);
      highlights.push({
        key,
        page: region.page,
        bbox: box.bbox,
        mode: "xyxy",
        unit: "absolute",
        ...regionPageSize(region.page, focusPage ?? null, focusPageSize ?? null, pageSizeFor),
        tone: "primary",
        label: box.textPreview,
      });
    }
  }
  if (highlights.length > 0) return highlights;
  if (!focusBbox || focusBbox.length < MIN_BBOX_VALUES) return [];
  return [
    {
      key: `focus:${focusPage ?? ""}:${focusBbox.join(",")}`,
      page: focusPage ?? null,
      bbox: focusBbox,
      mode: focusBboxMode ?? null,
      unit: focusBboxUnit ?? null,
      pageSize: focusPageSize ?? null,
      pageSizePage: focusPage ?? null,
      tone: "primary",
    },
  ];
}

function regionPageSize(
  page: number,
  focusPage: number | null,
  focusPageSize: BboxPageSize | null,
  pageSizeFor?: (page: number) => BboxPageSize | null
): Pick<PreviewHighlight, "pageSize" | "pageSizePage"> {
  const own = pageSizeFor?.(page) ?? null;
  if (own?.width && own?.height) return { pageSize: own, pageSizePage: page };
  return { pageSize: focusPageSize, pageSizePage: focusPage ?? page };
}

/**
 * 表示中のページに重ねる矩形（ページに対する %）を返す。
 *
 * `highlight.pageSize` は bbox の基準ページ（通常は chunk の先頭ページ）の寸法。表示中のページが
 * 基準と違い、両ページの表示寸法（pt）が分かるときは、同じ dpi で描いた前提で基準の px 寸法を比例させる。
 */
export function highlightRectsForPage(
  highlights: readonly PreviewHighlight[],
  page: number | null,
  options: {
    pagePoints?: (page: number) => { width: number; height: number } | null;
    /** bbox の基準寸法が無いときに使う寸法（画像ファイルは画像の実寸 px）。 */
    fallbackPageSize?: BboxPageSize | null;
  } = {}
): Array<{ key: string; tone: BboxHighlightTone; label: string | null; rect: BboxOverlayRect }> {
  const rects: Array<{
    key: string;
    tone: BboxHighlightTone;
    label: string | null;
    rect: BboxOverlayRect;
  }> = [];
  for (const highlight of highlights) {
    if (highlight.page != null && page != null && highlight.page !== page) continue;
    const pageSize = resolveRegionPageSize(highlight, page, options);
    const rect = normalizeBboxForPreview(
      highlight.bbox,
      pageSize,
      highlight.mode ?? null,
      highlight.unit ?? null
    );
    if (rect) rects.push({ key: highlight.key, tone: highlight.tone, label: highlight.label ?? null, rect });
  }
  return rects;
}

export function resolveRegionPageSize(
  highlight: Pick<PreviewHighlight, "page" | "pageSize" | "pageSizePage">,
  page: number | null,
  {
    pagePoints,
    fallbackPageSize = null,
  }: {
    pagePoints?: (page: number) => { width: number; height: number } | null;
    fallbackPageSize?: BboxPageSize | null;
  } = {}
): BboxPageSize | null {
  const base = highlight.pageSize?.width && highlight.pageSize?.height
    ? highlight.pageSize
    : fallbackPageSize
      ? { ...fallbackPageSize, rotation: highlight.pageSize?.rotation ?? fallbackPageSize.rotation }
      : (highlight.pageSize ?? null);
  if (base !== highlight.pageSize) return base;
  const referencePage = highlight.pageSizePage ?? highlight.page ?? null;
  if (!base || page == null || referencePage == null || page === referencePage || !pagePoints) {
    return base;
  }
  const width = Number(base.width);
  const height = Number(base.height);
  const reference = pagePoints(referencePage);
  const target = pagePoints(page);
  if (!reference || !target || !(width > 0) || !(height > 0)) return base;
  if (!(reference.width > 0) || !(reference.height > 0)) return base;
  return {
    ...base,
    width: (width * target.width) / reference.width,
    height: (height * target.height) / reference.height,
  };
}

/** 強調する領域のあるページ（昇順・重複なし）。 */
export function highlightPages(highlights: readonly PreviewHighlight[]): number[] {
  return [
    ...new Set(
      highlights
        .map((highlight) => highlight.page)
        .filter((page): page is number => page != null)
    ),
  ].sort((a, b) => a - b);
}

// ---- 根拠 1 件のプレビューの強調（#2389） ----

/** 根拠（chunk）の metadata から決めた、プレビューの最初のページと強調。 */
export type EvidencePreviewFocus = {
  focusPage: number | null;
  focusBbox: number[] | null;
  focusBboxMode: BboxCoordinateMode | null;
  focusBboxUnit: BboxOverlayUnit | null;
  focusPageSize: BboxPageSize | null;
  highlights: PreviewHighlight[];
};

/**
 * 根拠 1 件の metadata から、プレビューの最初のページと強調する領域を決める（RAG の根拠のパネルと Agent の根拠の
 * パネルで同じ計算。#349・#2389）。要素の表示領域（要素ごとの bbox）があれば要素ごとに、無ければ包含 bbox 1 つを強調する。
 */
export function evidencePreviewFocus(metadata?: Record<string, unknown> | null): EvidencePreviewFocus {
  const focusPage = firstIntegerValue(metadata, ["page_start", "page"]);
  const focusBbox = bboxFromMetadata(metadata);
  const focusBboxMode = bboxCoordinateModeFromMetadata(metadata);
  const focusBboxUnit = bboxUnitFromMetadata(metadata);
  const focusPageSize = withBboxPageRotation(
    bboxPageSizeFromMetadata(metadata),
    bboxPageRotationFromMetadata(metadata)
  );
  const highlights = buildPreviewHighlights({
    focusPage,
    focusBbox,
    focusBboxMode,
    focusBboxUnit,
    focusPageSize,
    regions: displayRegionsFromMetadata(metadata),
  });
  return { focusPage, focusBbox, focusBboxMode, focusBboxUnit, focusPageSize, highlights };
}

/** 最初に整数として読める値（数値か数字の文字列。小数は切り捨て）。 */
function firstIntegerValue(
  metadata: Record<string, unknown> | null | undefined,
  keys: readonly string[]
): number | null {
  if (!metadata) return null;
  for (const key of keys) {
    const value = metadata[key];
    if (typeof value === "number" && Number.isFinite(value)) return Math.trunc(value);
    if (typeof value === "string" && value.trim()) {
      const parsed = Number(value.trim());
      if (Number.isFinite(parsed)) return Math.trunc(parsed);
    }
  }
  return null;
}

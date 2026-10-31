// 分割比率の保存先（localStorage）の既定の前置き。製品は FixedSplitPane の storagePrefix で上書きできる。
export const FIXED_SPLIT_STORAGE_PREFIX = "production-ready.fixedSplitPane";
export const GOLDEN_RATIO = 1.618;
export const FIXED_SPLIT_MIN_FRACTION = 0.25;
export const FIXED_SPLIT_MAX_FRACTION = 0.75;
export const FIXED_SPLIT_EQUAL_FRACTION = 0.5;
export const FIXED_SPLIT_LEFT_WIDE_FRACTION = GOLDEN_RATIO / (GOLDEN_RATIO + 1);
export const FIXED_SPLIT_RIGHT_WIDE_FRACTION = 1 / (GOLDEN_RATIO + 1);
export const FIXED_SPLIT_DIVIDER_SIZE_PX = 14;
export const FIXED_SPLIT_DEFAULT_MIN_PANE_WIDTH_PX = 320;
/** 横並びから縦積みへ戻す幅の余裕（境目の近くで往復しない。スクロールバー 1 本分より広く。#1222）。 */
export const FIXED_SPLIT_LAYOUT_HYSTERESIS_PX = 24;

/**
 * 横並びにできる幅か（#1222）。縦積みから横並びへは境目以上で、横並びから縦積みへは境目より
 * `FIXED_SPLIT_LAYOUT_HYSTERESIS_PX` 狭くなったときだけ切り替える（幅の小さな揺れで往復しない）。
 */
export function nextFixedSplitWideEnough(current: boolean, width: number, threshold: number): boolean {
  return current ? width >= threshold - FIXED_SPLIT_LAYOUT_HYSTERESIS_PX : width >= threshold;
}
export const FIXED_SPLIT_KEYBOARD_STEP_PX = 24;
export const FIXED_SPLIT_KEYBOARD_FAST_STEP_PX = 72;

export type FixedSplitRatio = "equal" | "leftWide" | "rightWide";

const FIXED_SPLIT_RATIOS = new Set<FixedSplitRatio>(["equal", "leftWide", "rightWide"]);

export interface FixedSplitPaneState {
  ratio: FixedSplitRatio;
  leftFraction: number;
}

interface FixedSplitStorageValue {
  ratio?: unknown;
  leftFraction?: unknown;
}

export function fixedSplitStorageKey(splitId: string, prefix = FIXED_SPLIT_STORAGE_PREFIX) {
  return `${prefix}.${splitId}`;
}

export function isFixedSplitRatio(value: string | null): value is FixedSplitRatio {
  return value !== null && FIXED_SPLIT_RATIOS.has(value as FixedSplitRatio);
}

export function nextFixedSplitRatio(current: FixedSplitRatio): FixedSplitRatio {
  if (current === "equal") return "leftWide";
  if (current === "leftWide") return "rightWide";
  return "equal";
}

export function clampFixedSplitFraction(value: number) {
  if (!Number.isFinite(value)) return FIXED_SPLIT_EQUAL_FRACTION;
  return Math.min(FIXED_SPLIT_MAX_FRACTION, Math.max(FIXED_SPLIT_MIN_FRACTION, value));
}

function nonNegativeWidth(value: number) {
  return Number.isFinite(value) ? Math.max(0, value) : 0;
}

export function fixedSplitFractionBounds(
  availableWidthPx: number,
  minLeftPaneWidthPx = FIXED_SPLIT_DEFAULT_MIN_PANE_WIDTH_PX,
  minRightPaneWidthPx = FIXED_SPLIT_DEFAULT_MIN_PANE_WIDTH_PX
) {
  if (!Number.isFinite(availableWidthPx) || availableWidthPx <= 0) {
    return {
      minFraction: FIXED_SPLIT_MIN_FRACTION,
      maxFraction: FIXED_SPLIT_MAX_FRACTION,
    };
  }

  const minFraction = Math.max(
    FIXED_SPLIT_MIN_FRACTION,
    nonNegativeWidth(minLeftPaneWidthPx) / availableWidthPx
  );
  const maxFraction = Math.min(
    FIXED_SPLIT_MAX_FRACTION,
    1 - nonNegativeWidth(minRightPaneWidthPx) / availableWidthPx
  );

  // 必要幅を確保できないコンテナは UI 側で単列化する。計測の瞬間的なずれでも
  // 分割線が一方へ寄らないよう、境界が交差した場合は中央へ固定する。
  if (minFraction > maxFraction) {
    return {
      minFraction: FIXED_SPLIT_EQUAL_FRACTION,
      maxFraction: FIXED_SPLIT_EQUAL_FRACTION,
    };
  }

  return { minFraction, maxFraction };
}

export function clampFixedSplitFractionToPaneWidths(
  value: number,
  availableWidthPx: number,
  minLeftPaneWidthPx = FIXED_SPLIT_DEFAULT_MIN_PANE_WIDTH_PX,
  minRightPaneWidthPx = FIXED_SPLIT_DEFAULT_MIN_PANE_WIDTH_PX
) {
  const fallbackValue = clampFixedSplitFraction(value);
  const { minFraction, maxFraction } = fixedSplitFractionBounds(
    availableWidthPx,
    minLeftPaneWidthPx,
    minRightPaneWidthPx
  );
  return Math.min(maxFraction, Math.max(minFraction, fallbackValue));
}

export function fixedSplitFractionForRatio(ratio: FixedSplitRatio) {
  if (ratio === "leftWide") return FIXED_SPLIT_LEFT_WIDE_FRACTION;
  if (ratio === "rightWide") return FIXED_SPLIT_RIGHT_WIDE_FRACTION;
  return FIXED_SPLIT_EQUAL_FRACTION;
}

export function nearestFixedSplitRatio(leftFraction: number): FixedSplitRatio {
  const fraction = clampFixedSplitFraction(leftFraction);
  const distances: Array<[FixedSplitRatio, number]> = [
    ["equal", Math.abs(fraction - FIXED_SPLIT_EQUAL_FRACTION)],
    ["leftWide", Math.abs(fraction - FIXED_SPLIT_LEFT_WIDE_FRACTION)],
    ["rightWide", Math.abs(fraction - FIXED_SPLIT_RIGHT_WIDE_FRACTION)],
  ];
  distances.sort(([, a], [, b]) => a - b);
  return distances[0][0];
}

export function fixedSplitStateForRatio(ratio: FixedSplitRatio): FixedSplitPaneState {
  return {
    ratio,
    leftFraction: fixedSplitFractionForRatio(ratio),
  };
}

export function fixedSplitStateForFraction(leftFraction: number): FixedSplitPaneState {
  const clampedFraction = clampFixedSplitFraction(leftFraction);
  return {
    ratio: nearestFixedSplitRatio(clampedFraction),
    leftFraction: clampedFraction,
  };
}

export function nextFixedSplitStateFromFraction(leftFraction: number): FixedSplitPaneState {
  return fixedSplitStateForRatio(nextFixedSplitRatio(nearestFixedSplitRatio(leftFraction)));
}

export function adjustFixedSplitFraction(
  leftFraction: number,
  deltaPx: number,
  availableWidthPx: number,
  minLeftPaneWidthPx = 0,
  minRightPaneWidthPx = 0
) {
  if (!Number.isFinite(availableWidthPx) || availableWidthPx <= 0) {
    return clampFixedSplitFraction(leftFraction);
  }
  return clampFixedSplitFractionToPaneWidths(
    leftFraction + deltaPx / availableWidthPx,
    availableWidthPx,
    minLeftPaneWidthPx,
    minRightPaneWidthPx
  );
}

function parseStorageObject(value: FixedSplitStorageValue): FixedSplitPaneState | null {
  const ratio = typeof value.ratio === "string" && isFixedSplitRatio(value.ratio) ? value.ratio : null;
  const leftFraction =
    typeof value.leftFraction === "number" && Number.isFinite(value.leftFraction) ? value.leftFraction : null;

  if (leftFraction !== null) return fixedSplitStateForFraction(leftFraction);
  if (ratio !== null) return fixedSplitStateForRatio(ratio);
  return null;
}

export function parseFixedSplitStorageValue(
  value: string | null,
  fallbackState: FixedSplitPaneState = fixedSplitStateForRatio("equal")
): FixedSplitPaneState {
  if (isFixedSplitRatio(value)) return fixedSplitStateForRatio(value);
  if (value === null) return fallbackState;

  try {
    const parsed = JSON.parse(value) as unknown;
    if (parsed && typeof parsed === "object") {
      return parseStorageObject(parsed as FixedSplitStorageValue) ?? fallbackState;
    }
  } catch {
    // 旧版以外の壊れた storage は既定値に戻す。
  }
  return fallbackState;
}

export function serializeFixedSplitState(state: FixedSplitPaneState) {
  return JSON.stringify({
    ratio: state.ratio,
    leftFraction: clampFixedSplitFraction(state.leftFraction),
  });
}

function formatFraction(value: number) {
  return Number(value.toFixed(6)).toString();
}

export function fixedSplitGridTemplateColumns(ratioOrLeftFraction: FixedSplitRatio | number) {
  if (typeof ratioOrLeftFraction === "number") {
    const left = clampFixedSplitFraction(ratioOrLeftFraction);
    const right = 1 - left;
    return `minmax(0, ${formatFraction(left)}fr) ${FIXED_SPLIT_DIVIDER_SIZE_PX}px minmax(0, ${formatFraction(
      right
    )}fr)`;
  }

  const ratio = ratioOrLeftFraction;
  if (ratio === "leftWide") return `minmax(0, ${GOLDEN_RATIO}fr) 14px minmax(0, 1fr)`;
  if (ratio === "rightWide") return `minmax(0, 1fr) 14px minmax(0, ${GOLDEN_RATIO}fr)`;
  return "minmax(0, 1fr) 14px minmax(0, 1fr)";
}

export function fixedSplitValueText(ratio: FixedSplitRatio) {
  if (ratio === "leftWide") return "left-wide";
  if (ratio === "rightWide") return "right-wide";
  return "equal";
}

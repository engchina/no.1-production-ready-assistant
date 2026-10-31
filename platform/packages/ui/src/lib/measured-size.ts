/**
 * DOM で測った大きさを state・style に書き戻すときの揺れ止め（#1222）。
 *
 * 測った値で同じ（または中の）要素の大きさを変える処理（`ResizeObserver` → `setState` → `style`）は、
 * ブラウザーの拡大（125% など）で大きさが小数になると、整数への丸め（`offsetHeight` / `clientWidth`
 * など）が書き換えのたびに変わり、測定が 1px 違う 2 つの値を往復することがある。そのまま書くと
 * 画面が揺れ続ける（RAG の利用者フィードバック画面で報告）。
 *
 * 3 製品で、測った大きさを書き戻す処理は必ずこの関数を通す（design-system README §4「測った大きさの
 * 書き戻し」）。差が `tolerance`（既定 1px）以内なら今の値を保ち、どちらへ寄せるかを `prefer` で選ぶ。
 * - `larger`: 中身を収める大きさ（表の max-height など）。小さい方に寄せると中身が欠けてスクロールが出る。
 * - `smaller`: 容れ物の内側に置く大きさの上限（プレビューのページの幅など）。大きい方に寄せるとはみ出す。
 */
export type MeasuredSizePreference = "larger" | "smaller";

export const MEASURED_SIZE_TOLERANCE_PX = 1;

export function stabilizeMeasuredSize(
  current: number | undefined,
  next: number | undefined,
  { prefer = "larger", tolerance = MEASURED_SIZE_TOLERANCE_PX }: {
    prefer?: MeasuredSizePreference;
    tolerance?: number;
  } = {}
): number | undefined {
  if (current == null || next == null) return next;
  if (Math.abs(current - next) > tolerance) return next;
  return prefer === "larger" ? Math.max(current, next) : Math.min(current, next);
}

/** 幅・高さの組（`stabilizeMeasuredSize` を両方の軸に当てる。どちらも変わらなければ同じ object）。 */
export function stabilizeMeasuredBox<T extends { width: number; height: number }>(
  current: T | null | undefined,
  next: T,
  options?: { prefer?: MeasuredSizePreference; tolerance?: number }
): T {
  if (!current) return next;
  const width = stabilizeMeasuredSize(current.width, next.width, options) ?? next.width;
  const height = stabilizeMeasuredSize(current.height, next.height, options) ?? next.height;
  return width === current.width && height === current.height ? current : { ...next, width, height };
}

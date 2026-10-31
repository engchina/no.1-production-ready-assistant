import { useState } from "react";

/**
 * 値が前回のレンダーから変わったか（初回は true）を返す。
 *
 * props や server 値の変化に合わせて state を直すとき、effect で setState せずに render 中で直すための
 * 「前回値を state に持つ」パターン（https://react.dev/learn/you-might-not-need-an-effect）。
 * `useEffect(() => { … }, deps)` の deps と同じ値を渡し、true のレンダーで同じ処理を行う。
 * 比較は `Object.is`（effect の deps と同じ）。
 */
export function useValuesChanged(values: readonly unknown[]): boolean {
  const [previous, setPrevious] = useState<readonly unknown[] | null>(null);
  const changed =
    previous === null ||
    previous.length !== values.length ||
    values.some((value, index) => !Object.is(value, previous[index]));
  if (changed) setPrevious(values);
  return changed;
}

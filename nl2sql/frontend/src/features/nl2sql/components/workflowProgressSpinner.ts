/**
 * 帯の中で回すスピナーの位置。job が進んでいる間は、帯全体でちょうど 1 つ回す（同じ処理のスピナーは 1 つ。
 * UX 契約 messaging §3.7、#416）。
 *
 * - 見えている実行中の工程があれば、その先頭の工程の丸（並行して複数が実行中でも 1 つ）。
 * - 実行中の工程が無い（待機中・工程の切り替わりの間）か、工程を畳んでいるときは、見出しのアイコン。
 * - 進んでいなければ回さない。
 *
 * 開始のボタンは job の間は `loading` にしない（`disabled` にする）。スピナーはこの帯が出す。
 */
export function activitySpinnerTarget({
  active,
  collapsed,
  steps,
}: {
  active: boolean;
  collapsed: boolean;
  steps: ReadonlyArray<{ status: string }>;
}): number | "header" | null {
  if (!active) return null;
  const runningIndex = collapsed ? -1 : steps.findIndex((step) => step.status === "running");
  return runningIndex >= 0 ? runningIndex : "header";
}

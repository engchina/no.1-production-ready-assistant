/**
 * その場で結果を待つ操作（検索・チャットの送信・検索テスト）の「実行 / 停止」を 1 つのボタンで切り替えるときの判定（#413 / #805）。
 * UX 契約 buttons.md §3.1「その場の実行と停止」。DOM に依存しない形にして単体テストする。
 */

export type RunStopAction = "run" | "stop" | "ignore";

/**
 * ボタンのクリック（Enter / Space による暗黙のクリックを含む）を、どの操作にするか。
 *
 * - `clickCount` は `MouseEvent.detail`（キーボードの暗黙のクリックは 0、マウスの 1 回目は 1、ダブルクリックの 2 回目は 2）。
 *   2 回目以降は無視する。1 回目で実行が始まり、同じ位置のボタンが「停止」に変わった直後に、
 *   ダブルクリックの 2 回目が停止を押してしまわないように。
 * - 実行中は停止だけ。実行できない間（`runDisabled`）は何もしない。
 */
export function runStopClickAction({
  running,
  runDisabled = false,
  clickCount,
}: {
  running: boolean;
  runDisabled?: boolean;
  clickCount: number;
}): RunStopAction {
  if (clickCount > 1) return "ignore";
  if (running) return "stop";
  return runDisabled ? "ignore" : "run";
}

/**
 * 押し続けた Enter / Space の自動の繰り返し（`repeat`）か。
 * Enter は keydown ごとにクリックになるため、押し続けると「実行 → 停止」が続けて起きる。繰り返しの keydown は止める。
 */
export function isRepeatedActivationKey(event: { key: string; repeat?: boolean }): boolean {
  return Boolean(event.repeat) && (event.key === "Enter" || event.key === " ");
}

import { Square, type LucideIcon } from "lucide-react";

import { isRepeatedActivationKey, runStopClickAction } from "../../lib/run-stop";
import { cn } from "../../lib/utils";
import { Button, type ButtonProps } from "./button";

export interface RunStopButtonProps {
  /** 実行中か。true の間は同じボタンが「停止」になる。 */
  running: boolean;
  onRun: () => void;
  onStop: () => void;
  /** 実行のラベル（翻訳済み。例:「送信」「検索」「検索テスト」）。 */
  runLabel: string;
  /** 停止のラベル（翻訳済み。例:「停止」）。 */
  stopLabel: string;
  /** 実行のアイコン。停止のアイコンは `Square` に固定する。 */
  runIcon: LucideIcon;
  /** 実行できない（入力が空など）。実行中の停止には効かない。 */
  runDisabled?: boolean;
  /** 高さ。並べる入力欄と同じ size にする（既定 lg。README §4「操作部品の高さと幅」、#613）。 */
  size?: ButtonProps["size"];
  className?: string;
  testId?: string;
}

/**
 * その場で結果を待つ操作（検索・チャットの送信・検索テスト）の「実行」と「停止」を 1 つのボタンで出す（#413。#805 で RAG から移した）。
 * UX 契約 buttons.md §3.1「その場の実行と停止」。
 *
 * - 同じ `<button>` のまま、実行中は `secondary` の「停止」（Square）に切り替える。要素が変わらないためフォーカスが残る。
 *   停止・完了の後も同じボタンにフォーカスが残る。
 * - 実行中に押せるのは停止だけ。停止は取り消せる停止なので赤塗りにしない（buttons.md §3）。
 * - ボタンのスピナー（`loading`）は使わない。処理中は結果の領域の `ProcessingIndicator` が出し、
 *   その `role="status"` が読み上げる（messaging §3.7）。
 * - 実行できない間は `aria-disabled`（フォーカスを受ける）。停止・完了の後に実行できない状態へ戻っても
 *   （チャットは送信で入力欄が空になる）ネイティブの `disabled` のようにフォーカスが `body` へ外れない（#355）。
 * - ダブルクリックの 2 回目と、押し続けた Enter / Space の繰り返しは無視する（実行の直後に停止しない）。
 * - 2 つのラベルを重ねて置き、幅を長い方にそろえる（「検索テスト」→「停止」で幅が縮まない）。
 * - 入力欄の Enter は呼び出し側が `onRun` 相当の送信にだけつなぐ（実行中の Enter で停止しない）。
 */
export function RunStopButton({
  running,
  onRun,
  onStop,
  runLabel,
  stopLabel,
  runIcon,
  runDisabled = false,
  size = "lg",
  className,
  testId,
}: RunStopButtonProps) {
  return (
    <Button
      type="button"
      variant={running ? "secondary" : "primary"}
      size={size}
      icon={running ? Square : runIcon}
      aria-disabled={!running && runDisabled ? true : undefined}
      data-state={running ? "running" : "idle"}
      data-testid={testId}
      className={className}
      onKeyDown={(event) => {
        if (isRepeatedActivationKey(event)) event.preventDefault();
      }}
      onClick={(event) => {
        const action = runStopClickAction({ running, runDisabled, clickCount: event.detail });
        if (action === "run") onRun();
        else if (action === "stop") onStop();
      }}
    >
      {/* 2 つのラベルを同じセルに重ね、見えない方（visibility: hidden）は名前にも入らない。
          左寄せにして、短いラベルでもアイコンと文字の間を開けない。 */}
      <span className="grid justify-items-start">
        <span className={cn("col-start-1 row-start-1", running && "invisible")}>{runLabel}</span>
        <span className={cn("col-start-1 row-start-1", !running && "invisible")}>{stopLabel}</span>
      </span>
    </Button>
  );
}

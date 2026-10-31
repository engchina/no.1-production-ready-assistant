import { useEffect, useRef, useState, type ReactNode } from "react";
import { CirclePlay, Clock3 } from "lucide-react";

import {
  elapsedMsBetween,
  elapsedMsSince,
  formatElapsedClock,
  type OperationTimestamp,
} from "../../lib/operation-timing";
import { Button } from "../ui/button";
import { Spinner } from "../ui/spinner";
import { TEXT_ICON_SIZE } from "../ui/text-icon";

const DEFAULT_SLOW_AFTER_MS = 10_000;

/** 処理中表示の文言。製品の i18n で上書きできる。 */
export interface ProcessingLabels {
  /** 実行中の経過時間の見出し。 */
  elapsed: string;
  /** 完了後の所要時間の見出し。 */
  duration: string;
  /** `finalLabel` がないときの完了の文言。 */
  completed: string;
  /** 遅延の案内。 */
  slow: string;
  cancel: string;
}

export const DEFAULT_PROCESSING_LABELS: ProcessingLabels = {
  elapsed: "経過時間",
  duration: "処理時間",
  completed: "処理が完了しました",
  slow: "通常より時間がかかっています。",
  cancel: "キャンセル",
};

/**
 * 処理が影響する最小領域。
 * 表示位置を座標ではなく情報構造で固定し、ページごとの漂流を防ぐ。
 */
export type ProcessingPlacement =
  | "page"
  | "workspace"
  | "panel"
  | "tab"
  | "result"
  | "action"
  | "job";

/**
 * 処理中の印。`spinner` は回す、`static` は回さない実行中の印（`CirclePlay`。同じ処理のスピナーをほかの場所〔実行の
 * 詳細の処理の経過の今の段〕が回している間。文字の位置を変えない。#3445）、`none` は印を出さない（起点のボタンが
 * `loading` を出している間）。
 */
export type ProcessingActivityIcon = "spinner" | "static" | "none";

export interface UseOperationTimingOptions {
  active: boolean;
  /** 同じ component instance で別処理へ切り替わるとき timer をリセットする識別子。 */
  operationKey?: string | number | null;
  /** durable job 等、server が返す開始/終了時刻。未指定時は component 側で開始時刻を保持する。 */
  startedAt?: OperationTimestamp;
  finishedAt?: OperationTimestamp;
  /** server が確定済みの所要時間を返す場合に使用する。 */
  elapsedMs?: number | null;
  slowAfterMs?: number;
}

export interface OperationTiming {
  active: boolean;
  elapsedMs: number;
  elapsedClock: string;
  slow: boolean;
}

/**
 * client request と durable job の両方で使う共通 timer。
 * interval が background tab で間引かれても毎回 Date.now() から再計算する。
 */
export function useOperationTiming({
  active,
  operationKey = null,
  startedAt,
  finishedAt,
  elapsedMs: reportedElapsedMs,
  slowAfterMs = DEFAULT_SLOW_AFTER_MS,
}: UseOperationTimingOptions): OperationTiming {
  const localStartRef = useRef<{ key: string | number | null; at: number | null }>({
    key: operationKey,
    at: active ? Date.now() : null,
  });
  const wasActiveRef = useRef(active);
  const [nowMs, setNowMs] = useState(() => Date.now());

  if (
    active &&
    (!wasActiveRef.current ||
      localStartRef.current.at === null ||
      localStartRef.current.key !== operationKey)
  ) {
    localStartRef.current = { key: operationKey, at: Date.now() };
  } else if (!active && (wasActiveRef.current || localStartRef.current.key !== operationKey)) {
    localStartRef.current = { key: operationKey, at: null };
  }
  wasActiveRef.current = active;

  useEffect(() => {
    if (!active) return undefined;
    const update = () => setNowMs(Date.now());
    update();
    const timer = window.setInterval(update, 1000);
    document.addEventListener("visibilitychange", update);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", update);
    };
  }, [active, operationKey]);

  const externalElapsed = elapsedMsBetween(startedAt, finishedAt, nowMs);
  const localStart = localStartRef.current.at;
  const elapsedMs =
    !active && reportedElapsedMs != null
      ? Math.max(0, reportedElapsedMs)
      : externalElapsed ?? (localStart === null ? 0 : elapsedMsSince(localStart, nowMs));

  return {
    active,
    elapsedMs,
    elapsedClock: formatElapsedClock(elapsedMs),
    slow: active && elapsedMs >= slowAfterMs,
  };
}

export interface UseLatchedSlowOptions {
  /** 判断の対象の処理が続いているか。false になったら（処理の終わり・案内を出さない状態）忘れる。 */
  active: boolean;
  /** 対象（送信・Run・ジョブの ID など）。変わったら忘れる（別の処理）。 */
  resetKey?: string | number | null;
}

/**
 * 遅延の案内を一度出したら、処理が終わるまで消さない（3 製品共通。#2426）。`slow` が一度 true になったら、
 * `active` が false になるか `resetKey` が変わるまで true を返す。段階が変わって今の段階の経過時間が 0 に戻っても、
 * 案内が消えて出直す（出たり消えたりする）ことがない。閾値の判断（`useOperationTiming` の `slow`）は変えない。
 */
export function useLatchedSlow(slow: boolean, { active, resetKey = null }: UseLatchedSlowOptions): boolean {
  const [state, setState] = useState({ latched: false, active, resetKey });
  let next = state;
  if (state.active !== active || state.resetKey !== resetKey) next = { latched: false, active, resetKey };
  if (active && slow && !next.latched) next = { ...next, latched: true };
  // 描画の中で前の状態から決まる値を更新する（React の「前の props から状態を決める」型。effect を待たない）。
  if (next !== state) setState(next);
  return active && (next.latched || slow);
}

export interface ProcessingIndicatorProps extends UseOperationTimingOptions {
  label: string;
  finalLabel?: string;
  onCancel?: () => void;
  placement?: ProcessingPlacement;
  className?: string;
  testId?: string;
  showSlowMessage?: boolean;
  /** 遅延案内を live region にするか。別の status が状態を担う場合は false。 */
  announceSlow?: boolean;
  /**
   * 同じ operation の主ボタンが loading の場合は "none"、同じ処理の別の場所（実行の詳細の処理の経過の今の段）が回して
   * いる場合は "static" にして、動的 icon を 1 つへ絞る。
   */
  activityIcon?: ProcessingActivityIcon;
  /** 別の live region が状態遷移を通知する場合は false にして二重読み上げを避ける。 */
  announceActivity?: boolean;
  labels?: Partial<ProcessingLabels>;
}

/**
 * 処理ラベル・spinner・経過時間・取消を一列にまとめた全画面共通表示。
 *
 * 遅延の案内（「通常より時間がかかっています。」）は処理の名前の直下に出す（#2426）。行は処理の開始から見えない
 * 文言で高さを予約し（#902）、一度出たら処理が終わるまで消さない（`useLatchedSlow`）。
 */
export function ProcessingIndicator({
  label,
  finalLabel,
  onCancel,
  placement = "panel",
  className = "",
  testId,
  showSlowMessage = true,
  announceSlow = true,
  activityIcon = "spinner",
  announceActivity = true,
  labels: labelOverrides,
  ...timingOptions
}: ProcessingIndicatorProps) {
  const labels = { ...DEFAULT_PROCESSING_LABELS, ...labelOverrides };
  const timing = useOperationTiming(timingOptions);
  const showSlowLine = showSlowMessage && timing.active;
  const slow = useLatchedSlow(timing.slow, { active: showSlowLine, resetKey: timingOptions.operationKey ?? null });
  const displayLabel = timing.active ? label : finalLabel ?? labels.completed;
  const showActivityIcon = activityIcon === "spinner";

  return (
    <div
      className={`pr-message-text grid min-w-0 gap-2 ${className}`}
      aria-busy={timing.active}
      data-processing-placement={placement}
      data-processing-activity-icon={activityIcon}
      data-testid={testId}
    >
      {timing.active && announceActivity ? (
        <span className="sr-only" role="status">
          {label}
        </span>
      ) : null}
      <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        {/* 印は折り返した文字の 1 行目の中心にそろえる（#2371）。広い幅では 1 行目を右の経過時間（高さ 1.75rem）の
            中央の高さに置き、遅延の案内はその下に続ける（#2426）。 */}
        <span className="flex min-w-0 items-start gap-2 text-sm font-medium text-fg sm:pt-[calc((1.75rem-1lh)/2)]">
          {timing.active && showActivityIcon ? (
            <span className="pr-icon-line">
              <Spinner size={16} className="text-accent-fg" />
            </span>
          ) : timing.active && activityIcon === "static" ? (
            <span className="pr-icon-line text-accent-fg" data-processing-static-icon="true">
              <CirclePlay size={16} aria-hidden="true" />
            </span>
          ) : !timing.active && activityIcon !== "none" ? (
            <span className="pr-icon-line text-fg-muted">
              <Clock3 size={16} aria-hidden="true" />
            </span>
          ) : null}
          <span className="grid min-w-0">
            {/* e2e は見える処理の名前を data-processing-label で探す（class で探さない。直下の案内の行と区別する）。 */}
            <span className="min-w-0 break-words" data-processing-label="true">
              {displayLabel}
            </span>
            {/*
              遅延の案内は処理の名前の直下（#2426）。行は最初から置き、遅延するまでは同じ文言を見えない状態で高さだけ
              予約する（#902）。後から行を足すと領域が 1 行高くなり、下の内容を押し下げる。role="status" は遅延してから付ける（予約の行を status に
              しない。処理中の見出しの status と 2 つにならないよう、従来どおり遅延の案内だけを status にする）。
            */}
            {showSlowLine ? (
              <span
                className="min-w-0 break-words text-xs font-normal leading-5 text-fg-muted"
                role={announceSlow && slow ? "status" : undefined}
                data-testid={testId && slow ? `${testId}-slow` : undefined}
                data-processing-slow={slow ? "true" : "false"}
              >
                {slow ? (
                  labels.slow
                ) : (
                  // 予約用の文言は ::before の content で描く（textContent・読み上げに入れない。折り返しは同じ）。
                  <span
                    className="invisible before:content-[attr(data-placeholder)]"
                    aria-hidden="true"
                    data-placeholder={labels.slow}
                  />
                )}
              </span>
            ) : null}
          </span>
        </span>
        {/* 右の列: 経過時間（と取り消し）だけ。 */}
        <span className="flex flex-wrap items-center gap-2 sm:shrink-0 sm:justify-end">
          <span
            className="inline-flex min-h-7 items-center gap-1.5 whitespace-nowrap rounded-md border border-border bg-surface px-2 py-1 text-xs font-medium text-fg-muted"
            role="timer"
            aria-live="off"
            aria-label={`${timing.active ? labels.elapsed : labels.duration} ${timing.elapsedClock}`}
            data-testid={testId ? `${testId}-timer` : undefined}
          >
            <Clock3 size={TEXT_ICON_SIZE.xs} aria-hidden="true" />
            <span>{timing.active ? labels.elapsed : labels.duration}</span>
            <span className="min-w-[3.25rem] text-right font-sans tabular-nums text-fg">
              {timing.elapsedClock}
            </span>
          </span>
          {timing.active && onCancel ? (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={onCancel}
            >
              {labels.cancel}
            </Button>
          ) : null}
        </span>
      </div>
    </div>
  );
}

export interface TimedLoadingStateProps {
  label: string;
  children?: ReactNode;
  operationKey?: string | number | null;
  startedAt?: OperationTimestamp;
  onCancel?: () => void;
  placement?: ProcessingPlacement;
  className?: string;
  testId?: string;
  framed?: boolean;
  activityIcon?: ProcessingActivityIcon;
  labels?: Partial<ProcessingLabels>;
}

/** Skeleton/結果領域の寸法を保ったまま共通 timer を付ける loading container。 */
export function TimedLoadingState({
  label,
  children,
  operationKey,
  startedAt,
  onCancel,
  placement = "panel",
  className = "",
  testId,
  framed = true,
  activityIcon,
  labels,
}: TimedLoadingStateProps) {
  const effectiveActivityIcon = activityIcon ?? (placement === "result" ? "none" : "spinner");

  return (
    <section
      className={`grid min-w-0 gap-3 ${
        framed ? "rounded-md border border-border bg-surface-sunken p-3" : ""
      } ${className}`}
      aria-busy="true"
      aria-label={label}
      data-processing-placement={placement}
      data-testid={testId}
    >
      <ProcessingIndicator
        active
        label={label}
        operationKey={operationKey}
        startedAt={startedAt}
        onCancel={onCancel}
        placement={placement}
        testId={testId ? `${testId}-processing` : undefined}
        activityIcon={effectiveActivityIcon}
        labels={labels}
      />
      {children}
    </section>
  );
}

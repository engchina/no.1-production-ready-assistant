import {
  CircleCheck,
  CirclePause,
  CirclePlay,
  CircleSlash,
  CircleX,
  Clock,
  ShieldAlert,
  type LucideIcon,
} from "lucide-react";

import { StatusBadge, type StatusVariant } from "./status-badge";

/*
 * 実行履歴の状態（#1955）。3 製品（RAG・NL2SQL・Agent）の実行の状態を 7 つの共通の値にそろえる。
 * 製品は自分の状態（Agent の waiting_approval・RAG の blocked・NL2SQL の安全性の判定など）をこの値に写し、
 * 製品に固有の言い方が要るときだけ `label` で文言を上書きする（例: waiting →「承認待ち」）。
 * 規則は UX 契約 run-history.md「状態」。
 */

/** 実行の状態（3 製品で共通）。 */
export type RunStatus = "queued" | "running" | "waiting" | "succeeded" | "blocked" | "failed" | "cancelled";

/** 一覧の絞り込み・凡例で並べる順（進行中 → 終端）。 */
export const RUN_STATUSES: readonly RunStatus[] = [
  "queued",
  "running",
  "waiting",
  "succeeded",
  "blocked",
  "failed",
  "cancelled",
] as const;

/** 状態の文言（翻訳済み）。製品の i18n で上書きできる。 */
export type RunStatusLabels = Record<RunStatus, string>;

export const DEFAULT_RUN_STATUS_LABELS: RunStatusLabels = {
  queued: "待機中",
  running: "実行中",
  waiting: "応答待ち",
  succeeded: "完了",
  blocked: "ブロック",
  failed: "失敗",
  cancelled: "停止",
};

/**
 * 状態ごとの色（`StatusBadge` の variant）と形（アイコン）。
 * 7 つの状態はアイコンがすべて違う（グレースケール・強制カラーモードでも見分けられる。色だけに頼らない）。
 * 実行中もアイコンは回さない（一覧に回るものが並ぶと読みにくい。回すのは `Spinner` だけ。処理の経過の行は Spinner）。
 */
const RUN_STATUS_VIEW: Record<RunStatus, { variant: StatusVariant; icon: LucideIcon }> = {
  queued: { variant: "neutral", icon: Clock },
  running: { variant: "info", icon: CirclePlay },
  waiting: { variant: "warning", icon: CirclePause },
  succeeded: { variant: "success", icon: CircleCheck },
  blocked: { variant: "warning", icon: ShieldAlert },
  failed: { variant: "danger", icon: CircleX },
  cancelled: { variant: "neutral", icon: CircleSlash },
};

/** 状態の `StatusBadge` の variant。 */
export function runStatusVariant(status: RunStatus): StatusVariant {
  return RUN_STATUS_VIEW[status].variant;
}

/** 状態のアイコン。 */
export function runStatusIcon(status: RunStatus): LucideIcon {
  return RUN_STATUS_VIEW[status].icon;
}

/** まだ終わっていない（待機中・実行中・応答待ち）か。一覧の自動更新・停止の操作の判断に使う。 */
export function isRunActive(status: RunStatus): boolean {
  return status === "queued" || status === "running" || status === "waiting";
}

export interface RunStatusBadgeProps {
  status: RunStatus;
  /** 製品に固有の文言（翻訳済み。例: waiting を「承認待ち」）。無ければ `labels` の文言。 */
  label?: string;
  /** 状態の文言（製品の i18n）。既定は `DEFAULT_RUN_STATUS_LABELS`。 */
  labels?: Partial<RunStatusLabels>;
  className?: string;
  testId?: string;
}

/**
 * 実行の状態のバッジ（#1955）。一覧の「状態」列・狭い画面の行・詳細の概要で同じ部品を使う。
 * `data-run-status` に共通の状態の値を持つ（e2e はこの属性で状態を確かめる）。
 */
export function RunStatusBadge({ status, label, labels, className, testId }: RunStatusBadgeProps) {
  const text = label ?? labels?.[status] ?? DEFAULT_RUN_STATUS_LABELS[status];
  return (
    <span className="inline-flex" data-run-status={status} data-testid={testId}>
      <StatusBadge variant={runStatusVariant(status)} label={text} icon={runStatusIcon(status)} className={className} />
    </span>
  );
}

import {
  CalendarClock,
  CircleAlert,
  CircleCheck,
  History,
  Info,
  type LucideIcon,
  Minus,
  TriangleAlert,
} from "lucide-react";

import { surfaceTintFor } from "../../lib/surface-tint";
import { cn } from "../../lib/utils";
import { TEXT_ICON_SIZE } from "../ui/text-icon";

/**
 * 汎用ステータスバッジの配色トークン。
 * 各アプリは固有のステータス enum（RAG の FileStatus 等）を `variant` + 翻訳済み `label` に
 * マッピングして渡す（パッケージは特定ドメインの状態を知らない）。
 */
export type StatusVariant =
  | "neutral"
  | "info"
  /** @deprecated 旧実装で warning と完全に同値だった。warning を使うこと。 */
  | "pending"
  | "success"
  | "warning"
  | "danger";

const VARIANT_STYLES: Record<StatusVariant, string> = {
  neutral: "border-border-control bg-surface text-fg-muted",
  info: "border-info-border bg-info-subtle text-info-fg",
  pending: "border-warning-border bg-warning-subtle text-warning-fg",
  success: "border-success-border bg-success-subtle text-success-fg",
  warning: "border-warning-border bg-warning-subtle text-warning-fg",
  danger: "border-danger-border bg-danger-subtle text-danger-fg",
};

/*
 * 状態は色だけで表さない。success #047857 と danger #b91c1c は輝度がほぼ同じで
 * 1型・2型色覚では見分けられないため、形（アイコン）で冗長に符号化する。強制カラーモードでも意味が残る。
 */
const VARIANT_ICON: Record<StatusVariant, LucideIcon> = {
  neutral: Minus,
  info: Info,
  pending: History,
  success: CircleCheck,
  warning: TriangleAlert,
  danger: CircleAlert,
};

/** ステータスバッジ（アイコン + ラベル）。ラベルは i18n 済み文字列を渡す。 */
export function StatusBadge({
  variant,
  label,
  icon = true,
  className,
}: {
  variant: StatusVariant;
  label: string;
  /** 既定 true（バリアント既定のアイコン）。LucideIcon で上書き、false で非表示。 */
  icon?: boolean | LucideIcon;
  className?: string;
}) {
  const Icon = icon === true ? VARIANT_ICON[variant] : icon || null;
  return (
    <span
      data-status-variant={variant}
      data-surface-tint={surfaceTintFor(VARIANT_STYLES[variant])}
      className={cn(
        // 折り返すバッジでもアイコンは 1 行目の中心にそろえる（items-start + 行の高さの箱。#2371）。
        "inline-flex items-start justify-center gap-1 whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-medium",
        VARIANT_STYLES[variant],
        className
      )}
    >
      {/* アイコンはバッジの文字の大きさ（12px）にそろえる（#2371）。 */}
      {Icon ? (
        <span className="pr-icon-line">
          <Icon size={TEXT_ICON_SIZE.xs} aria-hidden />
        </span>
      ) : null}
      {label}
    </span>
  );
}

/**
 * 準備中（ロードマップ）の機能・選択肢の既定のラベル（#3374）。3 製品で同じ語にそろえるため `packages/ui` が持つ
 * （`DEFAULT_TAB_INVALID_LABEL` と同じ）。
 */
export const DEFAULT_ROADMAP_LABEL = "準備中（ロードマップ）";

/**
 * 準備中（ロードマップ）でまだ使えない機能・選択肢のバッジ（#3374）。中立色の `StatusBadge` に予定のアイコンを付け、
 * 色だけでなく文字とアイコンで「まだ使えない」を伝える。無効の選択肢（`SelectField` の `badge`）・カード・一覧の行で同じ形にする。
 */
export function RoadmapBadge({
  label = DEFAULT_ROADMAP_LABEL,
  className,
}: {
  /** 翻訳済みのラベル。既定 `DEFAULT_ROADMAP_LABEL`。 */
  label?: string;
  className?: string;
}) {
  return (
    <span data-roadmap-badge="" className="inline-flex">
      <StatusBadge variant="neutral" icon={CalendarClock} label={label} className={className} />
    </span>
  );
}

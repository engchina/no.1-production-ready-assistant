import { AlertCircle, AlertTriangle, CheckCircle2, Info, type LucideIcon } from "lucide-react";

/**
 * メッセージ機構の 4 トーン。
 * 色だけで意味を伝えないため、トーンごとにアイコンと role を必ず併置する。
 */
export type FeedbackTone = "success" | "info" | "warning" | "danger";

export const toneIcon: Record<FeedbackTone, LucideIcon> = {
  success: CheckCircle2,
  info: Info,
  warning: AlertTriangle,
  danger: AlertCircle,
};

/** 文字（アイコン・本文）色。 */
export const toneText: Record<FeedbackTone, string> = {
  success: "text-success-fg",
  info: "text-info-fg",
  warning: "text-warning-fg",
  danger: "text-danger-fg",
};

/**
 * バナー等の面（枠 + 背景 + 文字）。淡い面を塗るので、使う要素には `data-surface-tint={tone}` も付ける
 * （内側の副次テキストを 4.5:1 にする。tokens/colors.css。#1671）。
 */
export const toneSurface: Record<FeedbackTone, string> = {
  success: "border-success-border bg-success-subtle text-success-fg",
  info: "border-info-border bg-info-subtle text-info-fg",
  warning: "border-warning-border bg-warning-subtle text-warning-fg",
  danger: "border-danger-border bg-danger-subtle text-danger-fg",
};

/** danger は即時読み上げ（alert）、その他は polite（status）。 */
export function toneRole(tone: FeedbackTone): "alert" | "status" {
  return tone === "danger" ? "alert" : "status";
}

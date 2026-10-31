// スキルの出所の表示（スキルの一覧と業務 Agent のエディタで使う。#818）。
import { type StatusVariant } from "@production-ready/ui";
import { t } from "@/lib/i18n";

export function skillSourceLabel(source: string): string {
  switch (source) {
    case "builtin":
      return t("skills.sourceBuiltin");
    case "project":
      return t("skills.sourceProject");
    case "env":
      return t("skills.sourceEnv");
    default:
      return t("skills.sourceRuntime");
  }
}

export function skillSourceVariant(source: string): StatusVariant {
  if (source === "runtime") {
    return "success";
  }
  return source === "builtin" ? "neutral" : "info";
}

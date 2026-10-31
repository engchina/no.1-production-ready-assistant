import { t } from "@/lib/i18n";
import type { Nl2SqlEngine } from "./types";

export function engineLabel(engine: Nl2SqlEngine) {
  if (engine === "select_ai_agent") return t("nl2sql.engine.select_ai_agent");
  if (engine === "select_ai") return t("nl2sql.engine.select_ai");
  return t("nl2sql.engine.enterprise_ai_direct");
}

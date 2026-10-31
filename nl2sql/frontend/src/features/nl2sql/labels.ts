import { t } from "@/lib/i18n";
import type { Nl2SqlEngine } from "./types";

export function engineLabel(_engine: Nl2SqlEngine) {
  return t("nl2sql.engine.enterprise_ai_direct");
}

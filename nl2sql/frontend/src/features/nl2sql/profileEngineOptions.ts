import type { SelectFieldOption } from "@production-ready/ui";

// node --test から読むため、`@/` の別名ではなく相対パスで読む。
import { t } from "../../lib/i18n";
import { parseNl2SqlEngine } from "./queryPrefillState";
import type { Nl2SqlEngine, Nl2SqlEnginesData } from "./types";

/** この環境で使える生成の方式（#1524）。 */
export const NL2SQL_ENGINES_URL = "/api/nl2sql/engines";

const ENGINE_ORDER: readonly Nl2SqlEngine[] = ["select_ai", "select_ai_agent", "enterprise_ai_direct"];

function engineLabel(engine: Nl2SqlEngine) {
  return t(`nl2sql.engine.${engine}`);
}

/**
 * 既定の生成方式の選択肢（#1524）。この環境で使える方式だけを出す。保存済みの方式がこの環境で使えなくなっても、
 * 選択を黙って変えないように残し、使えないことをラベルで示す。選択肢を読むまで（と、応答に一覧が無いとき）は
 * Select AI と今の値だけにし、画面を落とさない。
 */
export function defaultEngineOptions(
  current: Nl2SqlEngine,
  engines: Nl2SqlEnginesData | undefined
): SelectFieldOption<Nl2SqlEngine>[] {
  const list = Array.isArray(engines?.engines) ? engines.engines : undefined;
  const available = new Set<Nl2SqlEngine>(
    list ? list.filter((item) => item.available).map((item) => item.engine) : ["select_ai"]
  );
  return ENGINE_ORDER.filter((engine) => available.has(engine) || engine === current).map((engine) => ({
    value: engine,
    label:
      available.has(engine) || !list
        ? engineLabel(engine)
        : t("profiles.field.defaultEngineUnavailable", { engine: engineLabel(engine) }),
  }));
}

/**
 * 画面の生成の方式（#1524）。利用者が業務プロファイルで選んだ方式（作業状態の文字列）があればそれ、無ければ
 * 業務プロファイルの既定の生成方式、それも無ければ Select AI。
 */
export function resolveProfileEngine(
  choice: string,
  profileDefault: Nl2SqlEngine | undefined
): Nl2SqlEngine {
  return parseNl2SqlEngine(choice) ?? profileDefault ?? "select_ai";
}

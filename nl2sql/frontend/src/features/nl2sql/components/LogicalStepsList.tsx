import { t } from "@/lib/i18n";
import type { Nl2SqlLogicalStep } from "../types";

/**
 * 処理手順を「業務者向け(1 行目)+ 技術者向け(2 行目)」で併記するリスト。
 * SQL 生成(Workbench)と SQL から質問を生成の両画面で共有する。
 * backend が構造化 details を返さない(過去 job / 旧 API)ときは文字列版へフォールバックする。
 */
export function LogicalStepsList({
  steps,
  fallbackSteps,
  ordered = true,
  surface = "background",
  listAriaLabel,
}: {
  steps?: Nl2SqlLogicalStep[] | null;
  /** 構造化 details が無いときに業務行として描画する従来の文字列手順。 */
  fallbackSteps?: string[] | null;
  ordered?: boolean;
  /** 置かれるパネルの背景に合わせて項目の面色を選ぶ。 */
  surface?: "background" | "card";
  listAriaLabel?: string;
}) {
  const items = normalizeSteps(steps, fallbackSteps);
  if (items.length === 0) return null;

  const ListTag = ordered ? "ol" : "ul";
  const itemSurface = surface === "card" ? "bg-surface" : "bg-surface-sunken";
  return (
    <ListTag className="grid gap-2" aria-label={listAriaLabel} data-testid="nl2sql-logical-steps-list">
      {items.map((step, index) => (
        <li
          // 同一文の手順が並ぶことがあるため index を key に含める。
          key={`${index}-${step.business}-${step.technical}`}
          className={`flex min-w-0 items-start gap-2 rounded-md border border-border ${itemSurface} px-3 py-2`}
          data-step-kind={step.kind || undefined}
        >
          {ordered && (
            <span
              data-surface-tint="neutral"
              className="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-surface-hover text-xs font-medium text-fg-muted"
              aria-hidden="true"
            >
              {index + 1}
            </span>
          )}
          <div className="grid min-w-0 gap-1">
            <span className="min-w-0 text-sm leading-6 text-fg [overflow-wrap:anywhere]">
              {step.business}
            </span>
            {step.technical && (
              <span className="flex min-w-0 items-start gap-1.5 text-xs leading-5 text-fg-muted">
                <span className="sr-only">{t("nl2sql.logicalSteps.technicalSrLabel")}</span>
                <span
                  data-surface-tint="neutral"
                  className="mt-0.5 shrink-0 rounded bg-surface-hover px-1.5 font-medium"
                  aria-hidden="true"
                >
                  {t("nl2sql.logicalSteps.technicalLabel")}
                </span>
                <code className="min-w-0 font-mono [overflow-wrap:anywhere]">{step.technical}</code>
              </span>
            )}
          </div>
        </li>
      ))}
    </ListTag>
  );
}

type NormalizedStep = { kind: string; business: string; technical: string };

/**
 * details を優先し、無ければ従来の文字列手順を業務行として扱う。
 * business が空の details は technical だけを 1 行目に出して情報を落とさない。
 */
function normalizeSteps(
  steps?: Nl2SqlLogicalStep[] | null,
  fallbackSteps?: string[] | null
): NormalizedStep[] {
  const details = (steps ?? [])
    .map((step) => ({
      kind: step.kind ?? "",
      business: (step.business ?? "").trim(),
      technical: (step.technical ?? "").trim(),
    }))
    .filter((step) => step.business || step.technical)
    .map((step) =>
      step.business ? step : { ...step, business: step.technical, technical: "" }
    );
  if (details.length > 0) return details;
  return (fallbackSteps ?? [])
    .map((step) => (step ?? "").trim())
    .filter(Boolean)
    .map((step) => ({ kind: "", business: step, technical: "" }));
}

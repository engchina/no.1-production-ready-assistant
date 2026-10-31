import { t } from "@/lib/i18n";
import { qualityCodeLabel } from "@/lib/source-profile-labels";

/** 表頭の推定の信頼度が低いシートの warning（rag_parser_core の EXCEL_HEADER_LOW_CONFIDENCE_WARNING。#1229）。 */
const HEADER_LOW_CONFIDENCE = "excel_header_low_confidence";

function splitWarning(warning: string): [string, string[]] {
  const [code, ...args] = warning.split(":");
  return [code.trim(), args];
}

/**
 * Excel の前処理の warning（`<code>:<シート名>[:<件数>]`）の日本語の説明。Excel の warning でなければ null。
 * シート名に「:」を含むときも、件数の付く warning 以外はシート名として残りをつなげる。
 */
export function excelWarningLabel(warning: string): string | null {
  const [code, args] = splitWarning(warning);
  switch (code) {
    case HEADER_LOW_CONFIDENCE:
      return t("documents.excelWarning.headerLowConfidence", { sheet: args.join(":") });
    case "excel_formula_without_cached_value": {
      const count = args.length > 1 ? args[args.length - 1] : "";
      const sheet = args.length > 1 ? args.slice(0, -1).join(":") : args.join(":");
      return t("documents.excelWarning.formulaWithoutValue", { sheet, count });
    }
    case "excel_sheet_not_found":
      return t("documents.excelWarning.sheetNotFound", { sheet: args.join(":") });
    case "excel_range_sheet_not_found":
      return t("documents.excelWarning.rangeSheetNotFound", { sheet: args.join(":") });
    default:
      return null;
  }
}

/** 表頭の推定の信頼度が低いシート（重複は除く）。文書レシピが確認（REVIEW）で止まった理由。 */
export function lowConfidenceHeaderSheets(warnings: readonly string[]): string[] {
  const sheets: string[] = [];
  for (const warning of warnings) {
    const [code, args] = splitWarning(warning);
    const sheet = args.join(":");
    if (code === HEADER_LOW_CONFIDENCE && sheet && !sheets.includes(sheet)) sheets.push(sheet);
  }
  return sheets;
}

/** 表頭の推定の信頼度が低いシートの warning か（文書詳細の先頭の確認の案内と同じ内容。#2034）。 */
export function isHeaderLowConfidenceWarning(warning: string): boolean {
  return splitWarning(warning)[0] === HEADER_LOW_CONFIDENCE;
}

/** backend の warning のコード（`snake_case` と `:` の後の引数）。利用者の文ではない。 */
const WARNING_CODE_PATTERN = /^[a-z][a-z0-9_]*(?::.*)?$/;

/**
 * 抽出結果の warning を、利用者の言葉の文（重複なし）と、「詳細」に畳むコードに分ける（#2034）。
 * 文にできないコードは共通の文 1 つにまとめ、コードは「詳細」にだけ出す。`omit` に当たる warning は
 * 同じ画面のほかの場所（文書の先頭の確認の案内など）が出すので、ここでは出さない（同じ案内は 1 か所）。
 */
export function extractionWarningView(
  warnings: readonly string[],
  omit: (warning: string) => boolean = () => false
): { messages: string[]; codes: string[] } {
  const messages: string[] = [];
  const codes: string[] = [];
  for (const raw of warnings) {
    const warning = raw.trim();
    if (!warning || omit(warning)) continue;
    const isCode = WARNING_CODE_PATTERN.test(warning);
    const qualityLabel = qualityCodeLabel(warning);
    const message =
      excelWarningLabel(warning) ??
      (qualityLabel !== warning
        ? qualityLabel
        : isCode
          ? t("flow.extraction.warning.generic")
          : warning);
    if (!messages.includes(message)) messages.push(message);
    if (isCode && !codes.includes(warning)) codes.push(warning);
  }
  return { messages, codes };
}

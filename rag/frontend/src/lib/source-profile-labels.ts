import type { DocumentSummary, SourceModality, SourcePreviewKind } from "./api";
import { t, type I18nKey } from "./i18n";

const PARSER_PROFILE_KEYS: Record<string, I18nKey> = {
  enterprise_ai_pdf_layout: "sourceProfile.parser.pdf",
  enterprise_ai_image_ocr: "sourceProfile.parser.image",
  enterprise_ai_text_structure: "sourceProfile.parser.text",
  local_text_structure: "sourceProfile.parser.text",
  enterprise_ai_office_structure: "sourceProfile.parser.office",
  local_office_structure: "sourceProfile.parser.office",
  local_html_semantic: "sourceProfile.parser.html",
  local_email_thread: "sourceProfile.parser.email",
  unsupported_outlook_msg: "sourceProfile.parser.outlookMsg",
  unsupported_tiff_image: "sourceProfile.parser.tiffImage",
  unsupported_legacy_office_binary: "sourceProfile.parser.legacyOffice",
  unsupported_macro_enabled_office: "sourceProfile.parser.macroOffice",
  unsupported_odt_rtf: "sourceProfile.parser.odtRtf",
  enterprise_ai_generic: "sourceProfile.parser.generic",
  legacy: "evaluation.ingestionQuality.parser.legacy",
};

const PARSER_BACKEND_ALIASES: Record<string, string> = {
  docling_adapter: "docling",
  unstructured_adapter: "unstructured",
};

const PARSER_BACKEND_LABELS: Record<string, string> = {
  docling: "Docling",
  unstructured: "Unstructured",
  structured_records_jsonl: "構造化レコード JSONL",
  xml_tree_json: "XML ツリー JSON",
  enterprise_ai: "OCI Enterprise AI",
  local: t("sourceProfile.backend.local"),
  local_partition: t("sourceProfile.backend.local"),
};

const SOURCE_WARNING_KEYS: Record<string, I18nKey> = {
  duplicate_content: "sourceProfile.warning.duplicate",
  content_type_missing: "sourceProfile.warning.contentTypeMissing",
  content_type_extension_mismatch: "sourceProfile.warning.contentTypeMismatch",
  large_file: "sourceProfile.warning.largeFile",
  unknown_modality: "sourceProfile.warning.unknown",
  unsupported_outlook_msg: "sourceProfile.warning.unsupportedOutlookMsg",
  unsupported_tiff_image: "sourceProfile.warning.unsupportedTiffImage",
  unsupported_legacy_office_binary: "sourceProfile.warning.unsupportedLegacyOffice",
  unsupported_macro_enabled_office: "sourceProfile.warning.unsupportedMacroOffice",
  unsupported_odt_rtf: "sourceProfile.warning.unsupportedOdtRtf",
  parser_fallback_used: "sourceProfile.warning.parserFallback",
  office_local_parse_failed: "sourceProfile.warning.officeFallback",
  office_segment_parse_failed: "sourceProfile.warning.officeSegmentFailed",
  failed_segments: "sourceProfile.warning.failedSegments",
  formula_review: "sourceProfile.warning.formulaReview",
  low_confidence_elements: "sourceProfile.warning.lowConfidenceElements",
};

const UNSUPPORTED_REASON_KEYS: Record<string, I18nKey> = {
  outlook_msg_not_supported: "sourceProfile.unsupported.outlookMsgNotSupported",
  tiff_image_not_supported: "sourceProfile.unsupported.tiffImageNotSupported",
  legacy_office_binary_not_supported: "sourceProfile.unsupported.legacyOfficeNotSupported",
  macro_enabled_office_not_supported: "sourceProfile.unsupported.macroOfficeNotSupported",
  odt_rtf_not_supported: "sourceProfile.unsupported.odtRtfNotSupported",
  unknown_file_type: "sourceProfile.unsupported.unknownFileType",
};

const EVALUATION_WARNING_KEYS: Record<string, I18nKey> = {
  table_structure_review: "evaluation.ingestionQuality.warning.table",
  figure_ocr_review: "evaluation.ingestionQuality.warning.figure",
  long_document: "evaluation.ingestionQuality.warning.longDocument",
  low_extraction_confidence: "evaluation.ingestionQuality.warning.lowConfidence",
  no_structured_elements: "evaluation.ingestionQuality.warning.noElements",
  content_type_missing: "evaluation.ingestionQuality.warning.contentTypeMissing",
  content_type_extension_mismatch: "evaluation.ingestionQuality.warning.contentTypeMismatch",
  unknown_modality: "evaluation.ingestionQuality.warning.unknownModality",
  duplicate_content: "evaluation.ingestionQuality.warning.duplicate",
  large_file: "evaluation.ingestionQuality.warning.largeFile",
  parser_fallback_used: "evaluation.ingestionQuality.warning.parserFallback",
  failed_segments: "evaluation.ingestionQuality.warning.failedSegments",
  segment_extraction_artifact_cache_miss:
    "evaluation.ingestionQuality.warning.segmentArtifactCacheMiss",
  unsupported_outlook_msg: "evaluation.ingestionQuality.warning.unsupportedOutlookMsg",
  unsupported_tiff_image: "evaluation.ingestionQuality.warning.unsupportedTiffImage",
  unsupported_legacy_office_binary: "evaluation.ingestionQuality.warning.unsupportedLegacyOffice",
  unsupported_macro_enabled_office: "evaluation.ingestionQuality.warning.unsupportedMacroOffice",
  unsupported_odt_rtf: "evaluation.ingestionQuality.warning.unsupportedOdtRtf",
};

export function parserProfileKey(profile: string): I18nKey {
  return PARSER_PROFILE_KEYS[profile] ?? "sourceProfile.parser.generic";
}

export function canonicalParserBackend(value: string | null | undefined): string | null {
  if (!value) return null;
  const normalized = value.trim().toLowerCase();
  return PARSER_BACKEND_ALIASES[normalized] ?? normalized;
}

export function parserBackendLabel(value: string | null | undefined): string {
  if (!value) return "-";
  const canonical = canonicalParserBackend(value) ?? value;
  return PARSER_BACKEND_LABELS[canonical] ?? qualityCodeLabel(value);
}

export function isSameParserBackend(
  left: string | null | undefined,
  right: string | null | undefined
): boolean {
  const leftCanonical = canonicalParserBackend(left);
  const rightCanonical = canonicalParserBackend(right);
  return leftCanonical !== null && leftCanonical === rightCanonical;
}

export function sourceModalityKey(modality: SourceModality): I18nKey {
  return `sourceProfile.modality.${modality}` as I18nKey;
}

export function sourcePreviewKey(kind: SourcePreviewKind): I18nKey {
  return `sourceProfile.preview.${kind}` as I18nKey;
}

export function sourceWarningKey(warning: string): I18nKey {
  return SOURCE_WARNING_KEYS[warning] ?? "sourceProfile.warning.generic";
}

export function unsupportedReasonLabel(reason: string | null | undefined): string {
  if (!reason) return "";
  const key = UNSUPPORTED_REASON_KEYS[reason];
  return key ? t(key) : reason;
}

/**
 * 原本情報に出す警告の key。未対応理由と同じ文になる警告（旧形式・マクロ付きの Office など）は、
 * 未対応理由の行と重ねて出さない（#2834）。
 */
export function visibleSourceWarningKeys(
  warnings: readonly string[],
  unsupportedReason: string | null | undefined
): I18nKey[] {
  const reason = unsupportedReasonLabel(unsupportedReason);
  return warnings.map(sourceWarningKey).filter((key) => !reason || t(key) !== reason);
}

/** 取込 job の skip_reason（重複・未対応形式など）を利用者向けの文に直す。 */
export function ingestionSkipReasonLabel(reason: string | null | undefined): string {
  if (reason === "duplicate_content") return t("sourceProfile.warning.duplicate");
  return (
    unsupportedReasonLabel(reason) ||
    t("flow.phase.skipped", { phase: t("flow.jobs.phase.preprocess") })
  );
}

export function qualityCodeLabel(value: string): string {
  const warningKey = EVALUATION_WARNING_KEYS[value] ?? SOURCE_WARNING_KEYS[value];
  if (warningKey) return t(warningKey);
  if (value in PARSER_PROFILE_KEYS) return t(parserProfileKey(value));
  return value;
}


const OFFICE_TYPE_KEYS: Record<string, I18nKey> = {
  ".doc": "sourceProfile.fileType.word", ".docx": "sourceProfile.fileType.word", ".docm": "sourceProfile.fileType.word",
  ".xls": "sourceProfile.fileType.excel", ".xlsx": "sourceProfile.fileType.excel", ".xlsm": "sourceProfile.fileType.excel", ".xlsb": "sourceProfile.fileType.excel",
  ".ppt": "sourceProfile.fileType.powerpoint", ".pptx": "sourceProfile.fileType.powerpoint", ".pptm": "sourceProfile.fileType.powerpoint",
};

/** 原本の種類を表示する。旧カテゴリや本文から抽出した分類は参照しない。 */
export function documentFileTypeLabel(document: Pick<DocumentSummary, "source_profile" | "file_name" | "content_type">): string {
  const profile = document.source_profile;
  const extension = (profile?.extension || document.file_name.match(/\.[a-z0-9]+$/i)?.[0] || "").toLowerCase();
  if (profile?.modality === "pdf") return t(sourceModalityKey("pdf"));
  const officeKey = OFFICE_TYPE_KEYS[extension];
  if (officeKey && (!profile || profile.modality === "office")) return t(officeKey);
  if (profile && profile.modality !== "unknown") return t(sourceModalityKey(profile.modality));
  if (extension) return extension.slice(1).toUpperCase();
  const mime = document.content_type?.split(";", 1)[0]?.trim().toLowerCase();
  if (mime === "application/pdf") return t(sourceModalityKey("pdf"));
  if (mime?.startsWith("image/")) return t(sourceModalityKey("image"));
  if (mime === "text/html") return t(sourceModalityKey("html"));
  if (mime?.startsWith("text/")) return t(sourceModalityKey("text"));
  return t(sourceModalityKey("unknown"));
}

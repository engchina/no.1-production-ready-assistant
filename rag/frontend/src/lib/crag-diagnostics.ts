/** CRAG の生成可否と必須観点の充足を別々に読む。旧 sufficient は充足の証明にしない。 */
export type CragAspectStatus = "supported" | "data_confirmation" | "missing" | "unknown";
export type CragDiagnostics = {
  generationUsable: boolean | null;
  coverage: "complete" | "partial" | "missing" | "unknown";
  execution: "evaluated" | "partial" | "not_run" | "error";
  missingAspects: string[];
  unknownAspects: string[];
  reason: string;
  aspects: {
    aspect: string;
    status: CragAspectStatus;
    sourceIds: string[];
    reason: string;
    evidenceValidation: string;
    evidence: { sourceId: string; quote: string }[];
  }[];
};
const object = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const rows = (value: unknown): unknown[] => Array.isArray(value) ? value : [];
const strings = (value: unknown): string[] => rows(value).filter((item): item is string => typeof item === "string" && item.length > 0);
const string = (value: unknown): string => typeof value === "string" ? value : "";

export function parseCragDiagnostics(value: unknown): CragDiagnostics | null {
  const attempts = rows(value);
  if (!attempts.length) return null;
  const raw = object(attempts.at(-1));
  if (!Object.keys(raw).length) return null;
  const execution = raw.grade_error || raw.grade_execution === "error" ? "error"
    : raw.grade_execution === "not_run" || object(raw.recovery_trace).grade_skipped ? "not_run"
      : raw.grade_execution === "partial" ? "partial" : "evaluated";
  const coverage = execution === "evaluated" && ["complete", "partial", "missing"].includes(String(raw.required_aspect_coverage))
    ? raw.required_aspect_coverage as "complete" | "partial" | "missing" : "unknown";
  const legacy = typeof raw.generation_usable !== "boolean" && typeof raw.required_aspect_coverage !== "string" && typeof raw.grade_execution !== "string";
  return {
    generationUsable: typeof raw.generation_usable === "boolean" ? raw.generation_usable : typeof raw.sufficient === "boolean" ? raw.sufficient : null,
    coverage,
    execution,
    missingAspects: legacy ? [] : strings(raw.missing_aspects),
    unknownAspects: [...new Set([...strings(raw.unknown_aspects), ...(legacy ? strings(raw.missing_aspects) : [])])],
    reason: string(raw.reason),
    aspects: rows(raw.aspect_checks).map((value) => {
      const item = object(value);
      return {
        aspect: string(item.aspect),
        status: (execution === "evaluated" || execution === "partial") && ["supported", "data_confirmation", "missing"].includes(String(item.status)) ? item.status as CragAspectStatus : "unknown",
        sourceIds: strings(item.source_ids),
        reason: string(item.reason),
        evidenceValidation: string(item.evidence_validation),
        evidence: rows(item.evidence).flatMap((value) => {
          const evidence = object(value);
          return typeof evidence.source_id === "string" && typeof evidence.quote === "string" ? [{ sourceId: evidence.source_id, quote: evidence.quote }] : [];
        }),
      };
    }).filter((item) => item.aspect),
  };
}

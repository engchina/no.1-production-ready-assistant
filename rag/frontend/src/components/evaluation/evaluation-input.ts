/**
 * 品質評価の入力（評価リクエストの Excel。#2065）と、結果の表のケースの見出しの判定。画面の部品から
 * 切り離して単体テストする。
 */

import type {
  EvaluationCaseResult,
  EvaluationImportIssue,
  EvaluationImportResult,
  EvaluationProcessDraft,
  EvaluationProcessRow,
} from "@/lib/api";
import { t } from "@/lib/i18n";

/** Excel の行番号から作ったケースの id（backend の `evaluation_case_id`）。 */
const ROW_CASE_ID = /^row-(\d+)$/;

/** ケースの id の Excel の行番号（Excel から作ったケースでなければ null）。 */
export function evaluationCaseRow(caseId: string): number | null {
  const match = ROW_CASE_ID.exec(caseId);
  return match ? Number(match[1]) : null;
}

/** ケースの短い見出し（「5 行目」。Excel から作ったケースでなければ id のまま）。 */
export function evaluationCaseRowLabel(caseId: string): string {
  const row = evaluationCaseRow(caseId);
  return row === null ? caseId : t("evaluation.case.row", { row });
}

/** 2 つの ID の集合が同じか（順序は問わない）。 */
export function sameIdSet(left: readonly string[], right: readonly string[]): boolean {
  if (left.length !== right.length) return false;
  const set = new Set(left);
  return right.every((id) => set.has(id));
}

/** Excel の誤りの位置の文言（「3 行目・期待する根拠」「ファイル」）。 */
export function evaluationImportIssueLocation(issue: EvaluationImportIssue): string {
  if (issue.row === null) return t("evaluation.import.issue.file");
  return issue.column
    ? t("evaluation.import.issue.rowColumn", { row: issue.row, column: issue.column })
    : t("evaluation.import.issue.row", { row: issue.row });
}

/** 検証した Excel の件数の内訳（答えるべき質問・資料に答えが無い質問・標準回答のある行）。 */
export function evaluationImportCounts(result: Pick<EvaluationImportResult, "rows">) {
  const answerable = result.rows.filter((row) => row.expected_response_decision === "answer").length;
  return {
    total: result.rows.length,
    answerable,
    unanswerable: result.rows.filter((row) => row.expected_response_decision === "hold").length,
    withStandardAnswer: result.rows.filter((row) => row.has_standard_answer).length,
  };
}

/** 結果の表に出す 1 つの期待する根拠（Excel の 1 行）。 */
export interface EvaluationEvidenceLabel {
  /** Excel に書いた元の 1 行。 */
  label: string;
  /** 根拠の文書の ID。 */
  documentId: string;
  /** ページ・章節・抜粋のある根拠の id（`required_evidence[].id`）。ファイル名だけなら null。 */
  evidenceId: string | null;
}

/** 評価を実行したケースの見出し（結果の表で質問と期待する根拠を出す。作業状態に残す）。 */
export interface EvaluationCaseLabel {
  question: string;
  evidence: EvaluationEvidenceLabel[];
}

export type EvaluationCaseLabels = Record<string, EvaluationCaseLabel>;

/** 質問の見出しの長さ（作業状態を小さく保つ。全文は Excel にある）。 */
const QUESTION_LABEL_MAX_CHARS = 200;

/**
 * 検証した Excel から、結果の表のケースの見出しを作る。期待する根拠は Excel に書いた順で、
 * ファイル名だけの行は正解の文書、ページ・章節・抜粋のある行は必要な根拠として照合した結果を出す。
 */
export function buildEvaluationCaseLabels(
  result: Pick<EvaluationImportResult, "cases" | "rows" | "document_names">
): EvaluationCaseLabels {
  const labels: EvaluationCaseLabels = {};
  const names = Object.entries(result.document_names);
  for (const item of result.cases) {
    const row = result.rows.find((candidate) => candidate.case_id === item.id);
    const evidenceByLabel = new Map(
      (item.required_evidence ?? [])
        .filter((evidence) => evidence.label)
        .map((evidence) => [evidence.label as string, evidence])
    );
    labels[item.id] = {
      question: truncate(item.query, QUESTION_LABEL_MAX_CHARS),
      evidence: (row?.evidence ?? []).flatMap((line) => {
        const evidence = evidenceByLabel.get(line);
        const documentId =
          evidence?.document_id ?? documentIdForLine(line, names, item.relevant_document_ids ?? []);
        return documentId
          ? [{ label: line, documentId, evidenceId: evidence?.id ?? null }]
          : [];
      }),
    };
  }
  return labels;
}

function documentIdForLine(
  line: string,
  names: readonly [string, string][],
  relevant: readonly string[]
): string | null {
  const fileName = line.split(/[|｜]/)[0]?.trim().normalize("NFKC").toLowerCase() ?? "";
  const match = names.find(
    ([id, name]) => relevant.includes(id) && name.trim().normalize("NFKC").toLowerCase() === fileName
  );
  return match?.[0] ?? null;
}

/** 1 つのケースの必要な根拠の上限（backend の `EvaluationCase.required_evidence`）。 */
const REQUIRED_EVIDENCE_MAX = 30;

/** 回答の過程のある行（期待の下書きを作る対象。#2402）。 */
export function evaluationProcessRows(
  result: Pick<EvaluationImportResult, "rows">
): EvaluationProcessRow[] {
  return result.rows.flatMap((row) =>
    row.answer_process
      ? [
          {
            case_id: row.case_id,
            row: row.row,
            question: row.question,
            answer_process: row.answer_process,
          },
        ]
      : []
  );
}

/**
 * 確かめた期待の下書き（#2402）を、検証した Excel のケースに足す。作れなかった下書き（error）は
 * 足さない。手順・対応は Excel に無ければ下書きのもの、条件・正解の文書は合わせ、必要な根拠は
 * 後ろに足す（上限まで）。足した根拠と資料は、結果の表に出るよう行の「期待する根拠」にも足す。
 */
export function applyProcessDrafts<
  T extends Pick<EvaluationImportResult, "cases" | "rows" | "document_names">,
>(result: T, drafts: Readonly<Record<string, EvaluationProcessDraft>>): T {
  const documentNames = { ...result.document_names };
  const addedLines: Record<string, string[]> = {};
  const cases = result.cases.map((item) => {
    const draft = drafts[item.id];
    if (!draft || draft.error) return item;
    const evidence = [...(item.required_evidence ?? [])];
    const lines: string[] = [];
    for (const extra of draft.required_evidence) {
      if (evidence.length >= REQUIRED_EVIDENCE_MAX) break;
      if (evidence.some((current) => current.id === extra.id)) continue;
      evidence.push(extra);
      if (extra.label) lines.push(extra.label);
    }
    const relevant = [...(item.relevant_document_ids ?? [])];
    for (const reference of draft.references) {
      if (reference.status !== "resolved" || !reference.document_id) continue;
      if (reference.file_name) documentNames[reference.document_id] = reference.file_name;
      if (!relevant.includes(reference.document_id)) {
        relevant.push(reference.document_id);
        if (!draft.required_evidence.some((extra) => extra.document_id === reference.document_id)) {
          lines.push(reference.file_name ?? reference.name);
        }
      }
    }
    addedLines[item.id] = lines;
    return {
      ...item,
      required_answer_points: [...new Set([...(item.required_answer_points ?? []), ...draft.required_answer_points])],
      expected_outcomes: item.expected_outcomes?.length
        ? item.expected_outcomes
        : draft.expected_outcomes,
      required_conditions: [
        ...new Set([...(item.required_conditions ?? []), ...draft.required_conditions]),
      ],
      relevant_document_ids: relevant,
      required_evidence: evidence,
    };
  });
  const rows = result.rows.map((row) =>
    addedLines[row.case_id]?.length
      ? { ...row, evidence: [...row.evidence, ...addedLines[row.case_id]] }
      : row
  );
  return { ...result, cases, rows, document_names: documentNames };
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

/** 1 つの期待する根拠の照合の結果。measured=false は測れなかった（検索をしない回答・失敗したケース）。 */
export interface EvaluationEvidenceOutcome extends EvaluationEvidenceLabel {
  found: boolean;
}

export interface EvaluationEvidenceSummary {
  measured: boolean;
  found: number;
  total: number;
  items: EvaluationEvidenceOutcome[];
}

/**
 * ケースの期待する根拠ごとに、回答の引用で見つかったかを求める。ファイル名だけの根拠は引用に
 * その文書があったか（`hit_document_ids`）、ページ・章節・抜粋のある根拠は必要な根拠の照合
 * （`missing_evidence` に無いか）で判定する。根拠の無いケースは null。
 */
export function evaluationEvidenceSummary(
  result: Pick<
    EvaluationCaseResult,
    "status" | "mrr" | "required_evidence_recall" | "hit_document_ids" | "missing_evidence"
  >,
  label: EvaluationCaseLabel | undefined
): EvaluationEvidenceSummary | null {
  const evidence = label?.evidence ?? [];
  if (!evidence.length) return null;
  const hits = new Set(result.hit_document_ids ?? []);
  const missing = new Set(result.missing_evidence ?? []);
  const measured =
    result.status === "success" &&
    (result.mrr != null || result.required_evidence_recall != null);
  const items = evidence.map((item) => ({
    ...item,
    found:
      measured &&
      (item.evidenceId === null
        ? hits.has(item.documentId)
        : result.required_evidence_recall != null && !missing.has(item.evidenceId)),
  }));
  return {
    measured,
    found: items.filter((item) => item.found).length,
    total: items.length,
    items,
  };
}

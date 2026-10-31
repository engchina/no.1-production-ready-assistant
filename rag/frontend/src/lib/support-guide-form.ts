/**
 * 業務ガイド（#1237）の編集フォームの状態と、内容（`SupportGuideContent`）との変換・保存前の確かめ。
 *
 * 文字の一覧（質問の例・検索の手がかりなど）は、編集中は改行を含む文字のまま持ち（1 行に 1 つ）、
 * 内容へ変えるときに行へ分ける（入力中に分けると、改行を打った直後の空行が消えて入力できない）。
 * 保存前の確かめは、backend の Pydantic の型（app/schemas/support_guide.py）が保存を拒む入力だけを見る。
 * 依存の循環・分岐の網羅などの内容の整合は backend の検証（app/rag/support_guide.py）に任せる。
 */

import type {
  DocumentSummary,
  SupportGuideAnswerSummary,
  SupportGuideBranchOperator,
  SupportGuideChange,
  SupportGuideChangeKind,
  SupportGuideCondition,
  SupportGuideConditionSource,
  SupportGuideConditionType,
  SupportGuideContent,
  SupportGuideDiffSection,
  SupportGuideDraftTryStep,
  SupportGuideImpactScope,
  SupportGuideIssue,
  SupportGuidePageRange,
  SupportGuideReference,
  SupportGuideSheetRange,
  SupportGuideStepStatus,
  SupportGuideSummary,
  SupportGuideTool,
  SupportGuideUnknownHandling,
} from "./api";
import { displayFileTimestamp } from "@production-ready/ui";

import { t, type I18nKey } from "./i18n";

export const SUPPORT_GUIDE_TOOLS: readonly SupportGuideTool[] = [
  "rag_search",
  "rag_read_source",
  "rag_retrieve_evidence",
  "nl2sql_query",
];
export const SUPPORT_GUIDE_ID_PATTERN = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
/** 1 つの資料の頁の範囲・1 つの手順の参照の上限（backend の MAX_REFERENCE_PAGE_RANGES / MAX_STEP_REFERENCES）。 */
export const MAX_PAGE_RANGES = 10;
export const MAX_STEP_REFERENCES = 10;
/** 全手順に共通の資料の上限（backend の SupportGuideContent.references の max_length）。 */
export const MAX_GUIDE_REFERENCES = 30;
const MAX_PAGE = 100_000;
/** 表計算の行の番号の上限（backend の MAX_SHEET_ROW。Excel のシートの行の数）。 */
const MAX_SHEET_ROW = 1_048_576;
const MAX_SHEET_COLUMN = 16_384;
const BOOLEAN_VALUES: Record<string, string> = { true: "はい", yes: "はい", はい: "はい", false: "いいえ", no: "いいえ", いいえ: "いいえ" };

export function booleanConditionValue(value: string): string | null {
  const key = value.trim().toLowerCase();
  return Object.hasOwn(BOOLEAN_VALUES, key) ? BOOLEAN_VALUES[key] : null;
}
const CELL_RANGE = /^([A-Z]{1,3})([1-9]\d{0,6})(?::([A-Z]{1,3})([1-9]\d{0,6}))?$/;

export type ConditionForm = {
  key: string;
  id: string;
  label: string;
  type: SupportGuideConditionType;
  allowedValues: string;
  required: boolean;
  source: SupportGuideConditionSource;
  unknownHandling: SupportGuideUnknownHandling;
  question: string;
  /** 言い換え（1 行に「選択肢: 語、語」）。 */
  valueAliases: string;
};

export type StepForm = {
  key: string;
  id: string;
  title: string;
  purpose: string;
  dependsOn: string[];
  retrievalHints: string;
  evidenceRequirements: string;
  allowedTools: string[];
  doneWhen: string;
  /** この手順が参照する資料と頁（#2395）。 */
  references: ReferenceForm[];
};

export type BranchForm = {
  key: string;
  id: string;
  conditionId: string;
  operator: SupportGuideBranchOperator;
  /** 値（1 行に 1 つ）。 */
  values: string;
  gotoStep: string;
  note: string;
};

export type ReferenceForm = {
  key: string;
  documentId: string;
  title: string;
  sectionPath: string;
  version: string;
  /** 根拠の頁（「3-5, 8」の形。空なら文書の全体。#2395）。 */
  pageRanges: string;
  /** 表計算の資料のシートの行（1 行に 1 つ。「シート名!42-45」「シート名!B3:D10」。#2663）。 */
  sheetRanges: string;
};

export type CompletionForm = {
  key: string;
  id: string;
  description: string;
  checkMethod: string;
};

export type GuideForm = {
  title: string;
  description: string;
  expectedResult: string;
  intentExamples: string;
  matchTerms: string;
  businessDomains: string;
  objectTypes: string;
  versions: string;
  effectiveFrom: string;
  effectiveTo: string;
  conditions: ConditionForm[];
  steps: StepForm[];
  branches: BranchForm[];
  references: ReferenceForm[];
  completion: CompletionForm[];
  impactScope: SupportGuideImpactScope;
  approvalRequired: boolean;
  approvalNote: string;
  /** 影響範囲と承認が係る手順の id（#1320）。 */
  impactSteps: string[];
  handoffConditions: string;
  handoffContact: string;
};

export type GuideRowSection = "conditions" | "steps" | "branches" | "references" | "completion";

let rowSequence = 0;

/** 行の React の key（内容には入れない）。 */
export function newRowKey(prefix: string): string {
  rowSequence += 1;
  return `${prefix}-${rowSequence}`;
}

/** 1 行に 1 つの文字を一覧にする（前後の空白・空行・重複を除く。backend の `_clean_list` と同じ）。 */
export function splitLines(text: string): string[] {
  return [...new Set(text.split("\n").map((line) => line.trim()).filter(Boolean))];
}

export function joinLines(values: readonly string[]): string {
  return values.join("\n");
}

export function emptyGuideForm(): GuideForm {
  return {
    title: "",
    description: "",
    expectedResult: "",
    intentExamples: "",
    matchTerms: "",
    businessDomains: "",
    objectTypes: "",
    versions: "",
    effectiveFrom: "",
    effectiveTo: "",
    conditions: [],
    steps: [],
    branches: [],
    references: [],
    completion: [],
    impactScope: "individual",
    approvalRequired: false,
    approvalNote: "",
    impactSteps: [],
    handoffConditions: "",
    handoffContact: "",
  };
}

export function guideFormFromContent(content: SupportGuideContent): GuideForm {
  return {
    title: content.title,
    description: content.description,
    expectedResult: content.goal.expected_result,
    intentExamples: joinLines(content.goal.intent_examples),
    matchTerms: joinLines(content.goal.match_terms),
    businessDomains: joinLines(content.applicability.business_domains),
    objectTypes: joinLines(content.applicability.object_types),
    versions: joinLines(content.applicability.versions),
    effectiveFrom: content.applicability.effective_from ?? "",
    effectiveTo: content.applicability.effective_to ?? "",
    conditions: content.conditions.map((condition) => ({
      key: newRowKey("condition"),
      id: condition.id,
      label: condition.label,
      type: condition.type,
      allowedValues: joinLines(condition.allowed_values),
      required: condition.required,
      source: condition.source,
      unknownHandling: condition.unknown_handling,
      question: condition.question,
      valueAliases: aliasesToText(condition.value_aliases ?? {}),
    })),
    steps: content.steps.map((step) => ({
      key: newRowKey("step"),
      id: step.id,
      title: step.title,
      purpose: step.purpose,
      dependsOn: [...step.depends_on],
      retrievalHints: joinLines(step.retrieval_hints),
      evidenceRequirements: joinLines(step.evidence_requirements),
      allowedTools: [...step.allowed_tools],
      doneWhen: step.done_when,
      references: (step.references ?? []).map(referenceFormFromContent),
    })),
    branches: content.branches.map((branch) => ({
      key: newRowKey("branch"),
      id: branch.id,
      conditionId: branch.when.condition_id,
      operator: branch.when.operator,
      values: joinLines(branch.when.values),
      gotoStep: branch.goto_step,
      note: branch.note,
    })),
    references: content.references.map(referenceFormFromContent),
    completion: content.completion.map((item) => ({
      key: newRowKey("completion"),
      id: item.id,
      description: item.description,
      checkMethod: item.check_method,
    })),
    impactScope: content.impact.scope,
    approvalRequired: content.impact.approval_required,
    approvalNote: content.impact.approval_note,
    impactSteps: [...(content.impact.steps ?? [])],
    handoffConditions: joinLines(content.handoff.conditions),
    handoffContact: content.handoff.contact,
  };
}

export function guideContentFromForm(form: GuideForm): SupportGuideContent {
  return {
    schema_version: 2,
    title: form.title.trim(),
    description: form.description.trim(),
    goal: {
      expected_result: form.expectedResult.trim(),
      intent_examples: splitLines(form.intentExamples),
      match_terms: splitLines(form.matchTerms),
    },
    applicability: {
      business_domains: splitLines(form.businessDomains),
      object_types: splitLines(form.objectTypes),
      versions: splitLines(form.versions),
      effective_from: form.effectiveFrom.trim() || null,
      effective_to: form.effectiveTo.trim() || null,
    },
    conditions: form.conditions.map((condition) => ({
      id: condition.id.trim(),
      label: condition.label.trim(),
      type: condition.type,
      // 選択肢は型が選択のときだけ送る（ほかの型で送ると backend が拒む）。
      allowed_values: condition.type === "enum" ? splitLines(condition.allowedValues) : [],
      required: condition.required,
      source: condition.source,
      unknown_handling: condition.unknownHandling,
      question: condition.question.trim(),
      value_aliases: condition.type === "enum" ? textToAliases(condition.valueAliases) : {},
    })),
    steps: form.steps.map((step) => ({
      id: step.id.trim(),
      title: step.title.trim(),
      purpose: step.purpose.trim(),
      depends_on: [...new Set(step.dependsOn)],
      retrieval_hints: splitLines(step.retrievalHints),
      evidence_requirements: splitLines(step.evidenceRequirements),
      allowed_tools: SUPPORT_GUIDE_TOOLS.filter((tool) => step.allowedTools.includes(tool)).concat(
        // 許可の一覧に無い道具（取り込んだ内容など）は落とさず送り、検証で知らせる。
        step.allowedTools.filter(
          (tool) => !(SUPPORT_GUIDE_TOOLS as readonly string[]).includes(tool),
        ) as SupportGuideTool[],
      ),
      done_when: step.doneWhen.trim(),
      references: step.references.map(referenceContentFromForm),
    })),
    branches: form.branches.map((branch) => ({
      id: branch.id.trim(),
      when: {
        condition_id: branch.conditionId.trim(),
        operator: branch.operator,
        // 「分からない」は値を持たない（比べ方を変えた前の値は送らない）。
        values: branch.operator === "unknown" ? [] : splitLines(branch.values),
      },
      goto_step: branch.gotoStep.trim(),
      note: branch.note.trim(),
    })),
    references: form.references.map(referenceContentFromForm),
    completion: form.completion.map((item) => ({
      id: item.id.trim(),
      description: item.description.trim(),
      check_method: item.checkMethod.trim(),
    })),
    impact: {
      scope: form.impactScope,
      approval_required: form.approvalRequired,
      approval_note: form.approvalNote.trim(),
      steps: [...new Set(form.impactSteps)],
    },
    handoff: {
      conditions: splitLines(form.handoffConditions),
      contact: form.handoffContact.trim(),
    },
  };
}

function referenceFormFromContent(reference: SupportGuideReference): ReferenceForm {
  return {
    key: newRowKey("reference"),
    documentId: reference.document_id,
    title: reference.title,
    sectionPath: joinLines(reference.section_path),
    version: reference.version,
    pageRanges: formatPageRanges(reference.page_ranges ?? []),
    sheetRanges: formatSheetRanges(reference.sheet_ranges ?? []),
  };
}

function referenceContentFromForm(reference: ReferenceForm): SupportGuideReference {
  const sheets = parseSheetRanges(reference.sheetRanges) ?? [];
  return {
    document_id: reference.documentId.trim(),
    title: reference.title.trim(),
    section_path: splitLines(reference.sectionPath),
    version: reference.version.trim(),
    // 形の違う入力は保存の前の確かめ（guideFormIssues）で止める。ここでは読めた範囲だけを送る。
    page_ranges: parsePageRanges(reference.pageRanges) ?? [],
    // 表計算の資料だけが持つ（無ければ送らない。backend の既定は空）。
    ...(sheets.length > 0 ? { sheet_ranges: sheets } : {}),
  };
}

/** 頁の範囲を「3-5, 8」の文字にする。 */
export function formatPageRanges(ranges: readonly SupportGuidePageRange[]): string {
  return ranges
    .map((range) => (range.page_start === range.page_end ? `${range.page_start}` : `${range.page_start}-${range.page_end}`))
    .join(", ");
}

/**
 * 「3-5, 8」の文字を頁の範囲にする（区切りは「,」「、」・空白、範囲は「-」「〜」「~」）。空なら空の一覧。
 * 形が違う・始まりが終わりより後・頁が 1 未満・範囲が多すぎるときは null。
 */
export function parsePageRanges(text: string): SupportGuidePageRange[] | null {
  const tokens = text
    .normalize("NFKC")
    .split(/[,、\s]+/)
    .map((token) => token.trim())
    .filter(Boolean);
  const ranges: SupportGuidePageRange[] = [];
  for (const token of tokens) {
    const match = /^(\d+)(?:\s*[-~〜–]\s*(\d+))?$/.exec(token);
    if (!match) return null;
    const start = Number(match[1]);
    const end = match[2] === undefined ? start : Number(match[2]);
    if (start < 1 || end < start || end > MAX_PAGE) return null;
    ranges.push({ page_start: start, page_end: end });
  }
  return ranges.length > MAX_PAGE_RANGES ? null : ranges;
}

/** シートの行の範囲を「シート名!42-45」（セルの範囲があれば「シート名!B3:D10」）の行にする。 */
export function formatSheetRanges(ranges: readonly SupportGuideSheetRange[]): string {
  return ranges
    .map((range) => {
      if (range.cell_range) return `${range.sheet_name}!${range.cell_range}`;
      if (range.row_start == null) return range.sheet_name;
      const end = range.row_end ?? range.row_start;
      return `${range.sheet_name}!${end === range.row_start ? range.row_start : `${range.row_start}-${end}`}`;
    })
    .join("\n");
}

/**
 * 1 行に 1 つの「シート名!42-45」「シート名!B3:D10」「シート名」をシートの行の範囲にする（#2663）。
 * シート名は最後の「!」の前。空なら空の一覧。形が違う・行が 1 未満・逆の範囲・多すぎるときは null。
 */
export function parseSheetRanges(text: string): SupportGuideSheetRange[] | null {
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  const ranges: SupportGuideSheetRange[] = [];
  for (const line of lines) {
    const mark = Math.max(line.lastIndexOf("!"), line.lastIndexOf("！"));
    const sheet = (mark >= 0 ? line.slice(0, mark) : line).trim();
    const location = mark >= 0 ? line.slice(mark + 1).normalize("NFKC").replace(/\s+/g, "").toUpperCase() : "";
    if (!sheet || sheet.length > 200) return null;
    if (!location) {
      ranges.push({ sheet_name: sheet });
      continue;
    }
    const cells = CELL_RANGE.exec(location);
    if (cells) {
      const first = Number(cells[2]);
      const last = cells[4] === undefined ? first : Number(cells[4]);
      const columnNumber = (column: string) => [...column].reduce((total, char) => total * 26 + char.charCodeAt(0) - 64, 0);
      const firstColumn = columnNumber(cells[1]);
      const lastColumn = columnNumber(cells[3] ?? cells[1]);
      if (first > last || firstColumn > lastColumn || last > MAX_SHEET_ROW || lastColumn > MAX_SHEET_COLUMN) return null;
      ranges.push({
        sheet_name: sheet,
        row_start: Math.min(first, last),
        row_end: Math.max(first, last),
        cell_range: location,
      });
      continue;
    }
    const rows = /^(\d+)(?:[-~〜–](\d+))?$/.exec(location);
    if (!rows) return null;
    const start = Number(rows[1]);
    const end = rows[2] === undefined ? start : Number(rows[2]);
    if (start < 1 || end < start || end > MAX_SHEET_ROW) return null;
    ranges.push({ sheet_name: sheet, row_start: start, row_end: end });
  }
  return ranges.length > MAX_PAGE_RANGES ? null : ranges;
}

/** 未保存の変更の判定に使う（行の key と、行へ分ける前の空行の違いは変更にしない）。 */
export function guideFormSnapshot(form: GuideForm): string {
  return JSON.stringify(guideContentFromForm(form));
}

/** 既存の ID と重ならない新しい行の ID（`step1`・`step2`…）。 */
export function nextRowId(prefix: string, existing: readonly string[]): string {
  const used = new Set(existing.map((id) => id.trim()));
  let n = 1;
  while (used.has(`${prefix}${n}`)) n += 1;
  return `${prefix}${n}`;
}

export function newConditionRow(form: GuideForm): ConditionForm {
  return {
    key: newRowKey("condition"),
    id: nextRowId("cond", form.conditions.map((item) => item.id)),
    label: "",
    type: "enum",
    allowedValues: "",
    required: true,
    source: "user",
    unknownHandling: "ask",
    question: "",
    valueAliases: "",
  };
}

export function newStepRow(form: GuideForm): StepForm {
  return {
    key: newRowKey("step"),
    id: nextRowId("step", form.steps.map((item) => item.id)),
    title: "",
    purpose: "",
    dependsOn: [],
    retrievalHints: "",
    evidenceRequirements: "",
    allowedTools: ["rag_search"],
    doneWhen: "",
    references: [],
  };
}

export function newBranchRow(form: GuideForm): BranchForm {
  return {
    key: newRowKey("branch"),
    id: nextRowId("branch", form.branches.map((item) => item.id)),
    conditionId: "",
    operator: "equals",
    values: "",
    gotoStep: "",
    note: "",
  };
}

/** 選んだ文書から資料の行を作る（文書 ID と表示名は文書から入れる。#2749）。 */
export function referenceRowFromDocument(document: Pick<DocumentSummary, "id" | "file_name">): ReferenceForm {
  return {
    key: newRowKey("reference"),
    documentId: document.id,
    title: document.file_name,
    sectionPath: "",
    version: "",
    pageRanges: "",
    sheetRanges: "",
  };
}

export function newCompletionRow(form: GuideForm): CompletionForm {
  return {
    key: newRowKey("completion"),
    id: nextRowId("done", form.completion.map((item) => item.id)),
    description: "",
    checkMethod: "",
  };
}

/** 一覧の index の行を delta（-1 / +1）だけ動かす。端を越えるときはそのまま。 */
export function moveItem<T>(items: readonly T[], index: number, delta: number): T[] {
  const target = index + delta;
  if (index < 0 || index >= items.length || target < 0 || target >= items.length) {
    return [...items];
  }
  const next = [...items];
  const [item] = next.splice(index, 1);
  next.splice(target, 0, item);
  return next;
}

/**
 * 手順の ID を変えたとき、ほかの手順の依存と分岐の行き先も新しい ID に付け替える
 * （1 文字ずつ打つたびに参照が切れないように）。
 */
export function renameStepId(form: GuideForm, oldId: string, newId: string): GuideForm {
  if (!oldId || oldId === newId) return form;
  // 同じ ID の手順がほかにもあるときは、参照がどちらを指すか決められないので付け替えない。
  if (form.steps.filter((step) => step.id === oldId).length > 1) return form;
  return {
    ...form,
    steps: form.steps.map((step) => ({
      ...step,
      dependsOn: step.dependsOn.map((id) => (id === oldId ? newId : id)),
    })),
    branches: form.branches.map((branch) =>
      branch.gotoStep === oldId ? { ...branch, gotoStep: newId } : branch,
    ),
    impactSteps: form.impactSteps.map((id) => (id === oldId ? newId : id)),
  };
}

/** 条件の ID を変えたとき、分岐の条件も付け替える。 */
export function renameConditionId(form: GuideForm, oldId: string, newId: string): GuideForm {
  if (!oldId || oldId === newId) return form;
  if (form.conditions.filter((condition) => condition.id === oldId).length > 1) return form;
  return {
    ...form,
    branches: form.branches.map((branch) =>
      branch.conditionId === oldId ? { ...branch, conditionId: newId } : branch,
    ),
  };
}

/**
 * 手順を外したとき、ほかの手順の依存と、影響範囲・承認が係る手順からも外す（分岐の行き先は残し、
 * 検証で知らせる）。
 */
export function removeStep(form: GuideForm, index: number): GuideForm {
  const removed = form.steps[index];
  if (!removed) return form;
  const steps = form.steps.filter((_, i) => i !== index);
  const stillUsed = steps.some((step) => step.id === removed.id);
  return {
    ...form,
    steps: stillUsed
      ? steps
      : steps.map((step) => ({
          ...step,
          dependsOn: step.dependsOn.filter((id) => id !== removed.id),
        })),
    impactSteps: stillUsed ? form.impactSteps : form.impactSteps.filter((id) => id !== removed.id),
  };
}

function clientIssue(path: string, message: string): SupportGuideIssue {
  return { severity: "error", code: "client_check", path, message };
}

function requiredIssue(path: string, value: string, field: I18nKey): SupportGuideIssue | null {
  return value.trim() ? null : clientIssue(path, t("supportGuides.check.required", { field: t(field) }));
}

function idIssue(path: string, value: string): SupportGuideIssue | null {
  if (!value.trim()) {
    return clientIssue(path, t("supportGuides.check.required", { field: t("supportGuides.field.id") }));
  }
  return SUPPORT_GUIDE_ID_PATTERN.test(value.trim())
    ? null
    : clientIssue(path, t("supportGuides.check.idFormat"));
}

/**
 * 保存の前の確かめ（backend が保存を拒む入力）。path は backend の検証と同じ形（`steps[1].id`）。
 */
export function guideFormIssues(form: GuideForm): SupportGuideIssue[] {
  const issues: (SupportGuideIssue | null)[] = [
    requiredIssue("title", form.title, "supportGuides.field.title"),
    requiredIssue("goal.expected_result", form.expectedResult, "supportGuides.field.expectedResult"),
  ];
  for (const [path, value] of [
    ["applicability.effective_from", form.effectiveFrom],
    ["applicability.effective_to", form.effectiveTo],
  ] as const) {
    if (value.trim() && !DATE_PATTERN.test(value.trim())) {
      issues.push(clientIssue(path, t("supportGuides.check.date")));
    }
  }
  if (
    DATE_PATTERN.test(form.effectiveFrom.trim()) &&
    DATE_PATTERN.test(form.effectiveTo.trim()) &&
    form.effectiveFrom.trim() > form.effectiveTo.trim()
  ) {
    issues.push(clientIssue("applicability.effective_to", t("supportGuides.check.period")));
  }
  form.conditions.forEach((condition, index) => {
    const base = `conditions[${index}]`;
    issues.push(idIssue(`${base}.id`, condition.id));
    issues.push(requiredIssue(`${base}.label`, condition.label, "supportGuides.field.conditionLabel"));
    if (condition.type === "enum" && splitLines(condition.allowedValues).length < 2) {
      issues.push(clientIssue(`${base}.allowed_values`, t("supportGuides.check.enumValues")));
    }
    if (
      condition.unknownHandling === "ask" &&
      condition.source === "user" &&
      !condition.question.trim()
    ) {
      issues.push(clientIssue(`${base}.question`, t("supportGuides.check.question")));
    }
  });
  form.steps.forEach((step, index) => {
    const base = `steps[${index}]`;
    issues.push(idIssue(`${base}.id`, step.id));
    issues.push(requiredIssue(`${base}.title`, step.title, "supportGuides.field.stepTitle"));
    issues.push(...referenceIssues(`${base}.references`, step.references));
  });
  form.branches.forEach((branch, index) => {
    const base = `branches[${index}]`;
    issues.push(idIssue(`${base}.id`, branch.id));
    if (!branch.conditionId.trim()) {
      issues.push(
        clientIssue(
          `${base}.when.condition_id`,
          t("supportGuides.check.choose", { field: t("supportGuides.field.branchCondition") }),
        ),
      );
    }
    const values = splitLines(branch.values);
    if (branch.operator === "equals" && values.length !== 1) {
      issues.push(clientIssue(`${base}.when.values`, t("supportGuides.check.equalsOne")));
    }
    if (branch.operator === "in" && values.length === 0) {
      issues.push(clientIssue(`${base}.when.values`, t("supportGuides.check.inValues")));
    }
    const condition = form.conditions.find((item) => item.id === branch.conditionId);
    if (branch.operator !== "unknown" && condition?.type === "boolean" && values.some((value) => booleanConditionValue(value) === null)) {
      issues.push(clientIssue(`${base}.when.values`, t("supportGuides.check.booleanValues")));
    }
    if (!branch.gotoStep.trim()) {
      issues.push(
        clientIssue(
          `${base}.goto_step`,
          t("supportGuides.check.choose", { field: t("supportGuides.field.gotoStep") }),
        ),
      );
    }
  });
  issues.push(...referenceIssues("references", form.references));
  form.completion.forEach((item, index) => {
    const base = `completion[${index}]`;
    issues.push(idIssue(`${base}.id`, item.id));
    issues.push(
      requiredIssue(`${base}.description`, item.description, "supportGuides.field.completionDescription"),
    );
  });
  return issues.filter((issue): issue is SupportGuideIssue => issue !== null);
}

function referenceIssues(base: string, references: readonly ReferenceForm[]): SupportGuideIssue[] {
  const issues: (SupportGuideIssue | null)[] = [];
  references.forEach((reference, index) => {
    issues.push(
      requiredIssue(`${base}[${index}].document_id`, reference.documentId, "supportGuides.field.documentId"),
    );
    if (parsePageRanges(reference.pageRanges) === null) {
      issues.push(clientIssue(`${base}[${index}].page_ranges`, t("supportGuides.check.pageRanges")));
    }
    const sheets = parseSheetRanges(reference.sheetRanges);
    if (sheets === null) {
      issues.push(clientIssue(`${base}[${index}].sheet_ranges`, t("supportGuides.check.sheetRanges")));
    } else if (sheets.length > 0 && (parsePageRanges(reference.pageRanges) ?? []).length > 0) {
      issues.push(clientIssue(`${base}[${index}].sheet_ranges`, t("supportGuides.check.sheetAndPages")));
    }
  });
  return issues.filter((issue): issue is SupportGuideIssue => issue !== null);
}

export type SupportGuideState = "published" | "unpublished" | "draftOnly" | "archived";

/** 一覧・見出しの状態（アーカイブ > 未公開 > 未公開の変更あり > 公開中）。 */
export function supportGuideState(
  summary: Pick<SupportGuideSummary, "status" | "published_revision" | "has_unpublished_changes">,
): SupportGuideState {
  if (summary.status === "archived") return "archived";
  if (summary.published_revision == null) return "draftOnly";
  return summary.has_unpublished_changes ? "unpublished" : "published";
}

export const SUPPORT_GUIDE_STATE_VARIANT = {
  published: "success",
  unpublished: "warning",
  draftOnly: "info",
  archived: "neutral",
} as const satisfies Record<SupportGuideState, string>;

const SECTION_LABEL: Record<string, I18nKey> = {
  title: "supportGuides.field.title",
  description: "supportGuides.field.description",
  goal: "supportGuides.path.goal",
  applicability: "supportGuides.path.applicability",
  conditions: "supportGuides.path.conditions",
  steps: "supportGuides.path.steps",
  branches: "supportGuides.path.branches",
  references: "supportGuides.path.references",
  completion: "supportGuides.path.completion",
  impact: "supportGuides.path.impact",
  handoff: "supportGuides.path.handoff",
  schema_version: "supportGuides.path.schemaVersion",
};

const ROW_LABEL: Record<GuideRowSection, I18nKey> = {
  conditions: "supportGuides.row.condition",
  steps: "supportGuides.row.step",
  branches: "supportGuides.row.branch",
  references: "supportGuides.row.reference",
  completion: "supportGuides.row.completion",
};

const FIELD_LABEL: Record<string, I18nKey> = {
  "goal.expected_result": "supportGuides.field.expectedResult",
  "goal.intent_examples": "supportGuides.field.intentExamples",
  "goal.match_terms": "supportGuides.field.matchTerms",
  "applicability.business_domains": "supportGuides.field.businessDomains",
  "applicability.object_types": "supportGuides.field.objectTypes",
  "applicability.versions": "supportGuides.field.versions",
  "applicability.effective_from": "supportGuides.field.effectiveFrom",
  "applicability.effective_to": "supportGuides.field.effectiveTo",
  "conditions.id": "supportGuides.field.id",
  "conditions.label": "supportGuides.field.conditionLabel",
  "conditions.type": "supportGuides.field.conditionType",
  "conditions.allowed_values": "supportGuides.field.allowedValues",
  "conditions.value_aliases": "supportGuides.field.valueAliases",
  "conditions.required": "supportGuides.field.required",
  "conditions.source": "supportGuides.field.source",
  "conditions.unknown_handling": "supportGuides.field.unknownHandling",
  "conditions.question": "supportGuides.field.question",
  "steps.id": "supportGuides.field.id",
  "steps.title": "supportGuides.field.stepTitle",
  "steps.purpose": "supportGuides.field.purpose",
  "steps.depends_on": "supportGuides.field.dependsOn",
  "steps.retrieval_hints": "supportGuides.field.retrievalHints",
  "steps.evidence_requirements": "supportGuides.field.evidenceRequirements",
  "steps.allowed_tools": "supportGuides.field.allowedTools",
  "steps.done_when": "supportGuides.field.doneWhen",
  "steps.references": "supportGuides.field.stepReferences",
  "steps.references.document_id": "supportGuides.field.documentId",
  "steps.references.title": "supportGuides.field.referenceTitle",
  "steps.references.page_ranges": "supportGuides.field.pageRanges",
  "steps.references.sheet_ranges": "supportGuides.field.sheetRanges",
  "branches.id": "supportGuides.field.id",
  "branches.when": "supportGuides.field.branchCondition",
  "branches.when.condition_id": "supportGuides.field.branchCondition",
  "branches.when.operator": "supportGuides.field.operator",
  "branches.when.values": "supportGuides.field.values",
  "branches.goto_step": "supportGuides.field.gotoStep",
  "branches.note": "supportGuides.field.note",
  "references.document_id": "supportGuides.field.documentId",
  "references.title": "supportGuides.field.referenceTitle",
  "references.section_path": "supportGuides.field.sectionPath",
  "references.version": "supportGuides.field.version",
  "references.page_ranges": "supportGuides.field.pageRanges",
  "references.sheet_ranges": "supportGuides.field.sheetRanges",
  "completion.id": "supportGuides.field.id",
  "completion.description": "supportGuides.field.completionDescription",
  "completion.check_method": "supportGuides.field.checkMethod",
  "impact.scope": "supportGuides.field.impactScope",
  "impact.approval_required": "supportGuides.field.approvalRequired",
  "impact.approval_note": "supportGuides.field.approvalNote",
  "impact.steps": "supportGuides.field.impactSteps",
  "handoff.conditions": "supportGuides.field.handoffConditions",
  "handoff.contact": "supportGuides.field.handoffContact",
};

/** `steps.1.id`（取り込みの検証の位置）も `steps[1].id` にそろえる。 */
export function normalizeIssuePath(path: string): string {
  return path.replace(/\.(\d+)(?=\.|$)/g, "[$1]");
}

/**
 * 検証の結果の位置（`steps[1].depends_on`）を利用者の言葉（「手順 2・依存する手順」）にする。
 * 分からない位置は空文字（位置を出さない）。
 */
export function issuePathLabel(path: string): string {
  const normalized = normalizeIssuePath(path);
  const match = /^([a-z_]+)(?:\[(\d+)\])?(?:\.(.+))?$/.exec(normalized);
  if (!match) return "";
  const [, section, index, rest] = match;
  const sectionKey = SECTION_LABEL[section];
  if (!sectionKey) return "";
  const parts: string[] = [];
  if (index !== undefined && section in ROW_LABEL) {
    parts.push(t(ROW_LABEL[section as GuideRowSection], { n: Number(index) + 1 }));
  } else {
    parts.push(t(sectionKey));
  }
  if (rest) {
    const fieldKey = FIELD_LABEL[`${section}.${rest.replace(/\[\d+\]/g, "")}`];
    if (fieldKey) parts.push(t(fieldKey));
  }
  return parts.join(t("supportGuides.path.separator"));
}

// ---- 取込の差分（#1288） -------------------------------------------------------------------

/** 差分の種類の表示（色だけに頼らず、ラベルとアイコンの付いた StatusBadge で出す）。 */
export const SUPPORT_GUIDE_CHANGE_VARIANT = {
  added: "success",
  removed: "danger",
  changed: "warning",
} as const satisfies Record<SupportGuideChangeKind, string>;

/** 差分の節の名前（基本・確認する条件・手順など。編集の画面の節と同じ言葉）。 */
export function changeSectionLabel(section: SupportGuideDiffSection): string {
  return section === "basic" ? t("supportGuides.section.basic") : t(SECTION_LABEL[section]);
}

/** 変わった項目の名前（編集の画面の欄の名前）。分からない項目は backend の名前のまま。 */
export function changeFieldLabels(change: SupportGuideChange): string[] {
  return change.fields.map((field) => {
    if (field === "order") return t("supportGuides.change.order");
    const key = change.section === "basic" ? SECTION_LABEL[field] : FIELD_LABEL[`${change.section}.${field}`];
    return key ? t(key) : field;
  });
}

/** 差分を節ごとにまとめる（backend の並び = 編集の画面の節の順を保つ）。 */
export function groupChangesBySection(
  changes: readonly SupportGuideChange[],
): { section: SupportGuideDiffSection; changes: SupportGuideChange[] }[] {
  const groups: { section: SupportGuideDiffSection; changes: SupportGuideChange[] }[] = [];
  for (const change of changes) {
    const last = groups.at(-1);
    if (last && last.section === change.section) last.changes.push(change);
    else groups.push({ section: change.section, changes: [change] });
  }
  return groups;
}

/** 差分の件数（追加・削除・変更）。 */
export function countChanges(changes: readonly SupportGuideChange[]): Record<SupportGuideChangeKind, number> {
  const counts: Record<SupportGuideChangeKind, number> = { added: 0, removed: 0, changed: 0 };
  for (const change of changes) counts[change.kind] += 1;
  return counts;
}

// ---- 下書きで試す（#1288） -----------------------------------------------------------------

/** 業務ガイドで答えたときの進め方の表示。 */
export const SUPPORT_GUIDE_DECISION_VARIANT = {
  answer: "success",
  branch: "info",
  clarify: "warning",
  handoff: "warning",
} as const satisfies Record<SupportGuideAnswerSummary["decision"], string>;

/** 充足の未検証は不足と区別し、中性の表示にする。 */
export const SUPPORT_GUIDE_STEP_STATUS_VARIANT = {
  explained: "success",
  partial: "warning",
  unknown: "neutral",
  reference_only: "warning",
  unused: "warning",
  no_evidence: "danger",
  not_searched: "warning",
} as const satisfies Record<SupportGuideStepStatus, string>;

/** 必須の手順だけを、確認した不足・部分的な説明・未検証に分けて数える。 */
export function supportGuideStepCounts(steps: readonly SupportGuideDraftTryStep[]) {
  const required = steps.filter((step) => step.required);
  return {
    missing: required.filter((step) => ["reference_only", "unused", "no_evidence"].includes(step.status)).length,
    partial: required.filter((step) => step.status === "partial").length,
    unknown: required.filter((step) => ["unknown", "not_searched"].includes(step.status)).length,
  };
}

/** 試すときに値を選べる条件の選択肢（選択・はい / いいえ）。文字の条件は入力欄にする。 */
export function tryConditionOptions(condition: SupportGuideCondition): string[] {
  if (condition.type === "boolean") return ["はい", "いいえ"];
  if (condition.type === "enum") return [...condition.allowed_values];
  return [];
}

/** 試すときに渡す条件（空の値は渡さない = 分からない条件として試す）。 */
export function tryConditionsPayload(values: Record<string, string>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(values)
      .map(([id, value]) => [id, value.trim()] as const)
      .filter(([, value]) => value !== ""),
  );
}

export type ParsedImport = { guides: unknown[] } | { error: I18nKey };

/** 取り込む JSON を読む。書き出しの形（`{ guides: [...] }`）・ガイドの配列・ガイド 1 件を受け付ける。 */
export function parseSupportGuideImport(text: string): ParsedImport {
  if (!text.trim()) return { error: "supportGuides.importPanel.required" };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { error: "supportGuides.importPanel.parseError" };
  }
  let guides: unknown;
  if (Array.isArray(parsed)) {
    guides = parsed;
  } else if (parsed && typeof parsed === "object") {
    const record = parsed as Record<string, unknown>;
    guides = Array.isArray(record.guides) ? record.guides : "title" in record ? [record] : null;
  }
  if (!Array.isArray(guides) || guides.length === 0) {
    return { error: "supportGuides.importPanel.shapeError" };
  }
  if (guides.length > 100) return { error: "supportGuides.importPanel.tooMany" };
  return { guides };
}

/** 書き出しのファイル名（`support-guides-<プロファイル>-<日付>.json`。日付はシステムの設定の時間帯）。 */
export function supportGuideExportFileName(searchAnswerProfileId: string, now: Date): string {
  const date = displayFileTimestamp(now).slice(0, 8);
  const safeId = searchAnswerProfileId.replace(/[^A-Za-z0-9_-]/g, "_");
  return `support-guides-${safeId}-${date}.json`;
}

/** 言い換えを「選択肢: 語、語」の行にする。 */
export function aliasesToText(aliases: Record<string, string[]>): string {
  return Object.entries(aliases)
    .filter(([, words]) => words.length)
    .map(([value, words]) => `${value}: ${words.join("、")}`)
    .join("\n");
}

/** 「選択肢: 語、語」の行を言い換えにする（「:」「：」の無い行・語の無い行は捨てる）。 */
export function textToAliases(text: string): Record<string, string[]> {
  const aliases: Record<string, string[]> = {};
  for (const line of text.split(/\r?\n/)) {
    const match = /^([^:：]+)[:：](.*)$/.exec(line.trim());
    if (!match) continue;
    const value = match[1].trim();
    const words = match[2]
      .split(/[、,，]/)
      .map((word) => word.trim())
      .filter(Boolean);
    if (value && words.length) aliases[value] = [...(aliases[value] ?? []), ...words];
  }
  return aliases;
}


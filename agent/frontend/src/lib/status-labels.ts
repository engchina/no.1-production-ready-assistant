import type { StatusVariant } from "@production-ready/ui";

import type { ApprovalRequest, RunState, RunStep, ToolDefinition, ToolResult } from "@/lib/api";
import { t, type I18nKey } from "@/lib/i18n";

/**
 * ドメインの enum → 画面のラベル（i18n）と `StatusBadge` の variant の対応表（#802）。
 * design-system ARCHITECTURE §4「ドメイン enum のマッピングはアプリの責任。ラベルは必ず翻訳済み」。
 * 状態のバッジ・絞り込みの選択肢は、英語の生の値を出さずにここを通す。同じ状態は全画面で同じ色・同じ語にする。
 */

export type RunStatus = RunState["status"];
export type StepStatus = RunStep["status"];
export type ApprovalStatus = ApprovalRequest["status"];
export type PermissionLevel = ToolDefinition["permission_level"];
export type PolicyDecision = ToolResult["policy_decision"];

interface StatusEntry {
  label: I18nKey;
  variant: StatusVariant;
}

const RUN_STATUS: Record<RunStatus, StatusEntry> = {
  queued: { label: "status.run.queued", variant: "neutral" },
  running: { label: "status.run.running", variant: "info" },
  waiting_approval: { label: "status.run.waiting_approval", variant: "warning" },
  waiting_confirmation: { label: "status.run.waiting_confirmation", variant: "warning" },
  completed: { label: "status.run.completed", variant: "success" },
  failed: { label: "status.run.failed", variant: "danger" },
  cancelled: { label: "status.run.cancelled", variant: "neutral" },
};

const STEP_STATUS: Record<StepStatus, StatusEntry> = {
  pending: { label: "status.step.pending", variant: "neutral" },
  running: { label: "status.step.running", variant: "info" },
  waiting_approval: { label: "status.step.waiting_approval", variant: "warning" },
  completed: { label: "status.step.completed", variant: "success" },
  failed: { label: "status.step.failed", variant: "danger" },
  cancelled: { label: "status.step.cancelled", variant: "neutral" },
};

const APPROVAL_STATUS: Record<ApprovalStatus, StatusEntry> = {
  pending: { label: "status.approval.pending", variant: "warning" },
  approved: { label: "status.approval.approved", variant: "success" },
  rejected: { label: "status.approval.rejected", variant: "danger" },
  cancelled: { label: "status.approval.cancelled", variant: "neutral" },
};

const PERMISSION_LEVEL: Record<PermissionLevel, StatusEntry> = {
  read: { label: "status.permission.read", variant: "success" },
  write: { label: "status.permission.write", variant: "warning" },
  sensitive: { label: "status.permission.sensitive", variant: "danger" },
};

const POLICY_DECISION: Record<PolicyDecision, StatusEntry> = {
  allow: { label: "status.policy.allow", variant: "success" },
  ask: { label: "status.policy.ask", variant: "warning" },
  deny: { label: "status.policy.deny", variant: "danger" },
};

/** 成果物の種類（backend の `artifact_kind_by_tool` と組み込み Runtime の回答）。未知の種類は「その他」。 */
const ARTIFACT_KIND: Record<string, StatusEntry> = {
  answer: { label: "status.artifact.answer", variant: "success" },
  rag_evidence: { label: "status.artifact.rag_evidence", variant: "info" },
  structured_table: { label: "status.artifact.structured_table", variant: "info" },
  // 支援タスクの状態（Run をまたいで引き継ぐ条件・確認・根拠の参照・予算の消費。#1243）。
  support_task: { label: "status.artifact.support_task", variant: "neutral" },
  // 回答の最終の検証（rag_validate_answer の結果。#1246）。
  answer_validation: { label: "status.artifact.answer_validation", variant: "neutral" },
  // 回答の根拠（回答の確定のときに段落ごとの根拠を残す。#2328）。
  answer_evidence: { label: "status.artifact.answer_evidence", variant: "neutral" },
  runtime_artifact: { label: "status.artifact.runtime_artifact", variant: "neutral" },
};

/** Run のイベントの種類（`RunEventType`）。タイムラインのバッジに出す。未知の種類は区分（tool / runtime …）の語にする。 */
const EVENT_TYPE: Record<string, StatusEntry> = {
  "run.created": { label: "status.event.created", variant: "neutral" },
  "run.replayed": { label: "status.event.replayed", variant: "neutral" },
  "run.status_changed": { label: "status.event.statusChanged", variant: "neutral" },
  "run.completed": { label: "status.event.completed", variant: "success" },
  "run.cancelled": { label: "status.event.cancelled", variant: "neutral" },
  "runtime.submitted": { label: "status.event.started", variant: "info" },
  "runtime.dispatch_claimed": { label: "status.event.started", variant: "info" },
  "runtime.event": { label: "status.event.progress", variant: "info" },
  "runtime.failed": { label: "status.event.failed", variant: "danger" },
  // ツールの呼び出しの開始（回答前の確認のツールも含む。#3352。「不明」にしない）。
  "step.started": { label: "status.event.started", variant: "info" },
  "tool.completed": { label: "status.event.completed", variant: "success" },
  "tool.failed": { label: "status.event.failed", variant: "danger" },
  "tool.unhandled_error": { label: "status.event.failed", variant: "danger" },
  "tool.approval_required": { label: "status.event.approvalRequired", variant: "warning" },
  "tool.guardrail_warning": { label: "status.event.warning", variant: "warning" },
  "approval.decided": { label: "status.event.decided", variant: "info" },
  "artifact.created": { label: "status.event.saved", variant: "info" },
};

const EVENT_CATEGORY: Record<string, I18nKey> = {
  run: "status.eventCategory.run",
  runtime: "status.eventCategory.runtime",
  tool: "status.eventCategory.tool",
  step: "status.eventCategory.tool",
  approval: "status.eventCategory.approval",
  artifact: "status.eventCategory.artifact",
};

const UNKNOWN: StatusEntry = { label: "status.unknown", variant: "neutral" };

function resolve<K extends string>(table: Record<K, StatusEntry>, value: string | null | undefined): StatusEntry {
  if (value && Object.prototype.hasOwnProperty.call(table, value)) return table[value as K];
  return UNKNOWN;
}

export interface StatusView {
  label: string;
  variant: StatusVariant;
}

function view(entry: StatusEntry): StatusView {
  return { label: t(entry.label), variant: entry.variant };
}

export function runStatusView(status: string): StatusView {
  return view(resolve(RUN_STATUS, status));
}

export function stepStatusView(status: string): StatusView {
  return view(resolve(STEP_STATUS, status));
}

export function approvalStatusView(status: string | null | undefined): StatusView {
  return view(resolve(APPROVAL_STATUS, status));
}

export function permissionView(level: string | null | undefined): StatusView {
  return view(resolve(PERMISSION_LEVEL, level));
}

export function policyDecisionView(decision: string | null | undefined): StatusView {
  return view(resolve(POLICY_DECISION, decision));
}

export function artifactKindView(kind: string): StatusView {
  return view(resolve(ARTIFACT_KIND, kind));
}

/** タイムラインのイベントの種類。表に無い種類は、区分（実行 / 実行環境 / ツール / 承認 / 成果物）の語にする。 */
export function eventTypeView(type: string): StatusView {
  if (Object.prototype.hasOwnProperty.call(EVENT_TYPE, type)) return view(EVENT_TYPE[type]);
  const category = EVENT_CATEGORY[type.split(".")[0] ?? ""];
  return { label: category ? t(category) : t("status.unknown"), variant: type.includes("failed") ? "danger" : "neutral" };
}

/** 絞り込みの選択肢（値は API の enum のまま、ラベルは翻訳済み）。 */
export const RUN_STATUS_VALUES = Object.keys(RUN_STATUS) as RunStatus[];
export const STEP_STATUS_VALUES = Object.keys(STEP_STATUS) as StepStatus[];
export const APPROVAL_STATUS_VALUES = Object.keys(APPROVAL_STATUS) as ApprovalStatus[];

export function runStatusOptions(): { value: RunStatus; label: string }[] {
  return RUN_STATUS_VALUES.map((value) => ({ value, label: runStatusView(value).label }));
}

export function stepStatusOptions(): { value: StepStatus; label: string }[] {
  return STEP_STATUS_VALUES.map((value) => ({ value, label: stepStatusView(value).label }));
}

export function approvalStatusOptions(): { value: ApprovalStatus; label: string }[] {
  return APPROVAL_STATUS_VALUES.map((value) => ({ value, label: approvalStatusView(value).label }));
}

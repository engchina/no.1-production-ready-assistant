import type { ReactNode } from "react";

import type { ApiErrorDetail } from "../../lib/api-error";
import { cn } from "../../lib/utils";
import type { RunFact } from "../data/run-detail";
import { formatRunDateTime } from "../data/run-history";
import { Banner } from "../ui/banner";
import { ApiErrorDetailList } from "./api-error-banner";

/*
 * 処理の失敗の表示（3 製品共通。#2342。UX 契約 messaging.md §9 / §10.3「処理の失敗のカード」）。
 *
 * チャットの回答の場所と実行の詳細の概要に、同じ形で失敗を出す:
 *   1 文目: 何が起きたか（止まった段を含む利用者の言葉）
 *   本文: 原因（backend の利用者向けの文）→ 次の操作（再送信・開き直す・トレース ID を添えて管理者へ）
 *   「詳細」（畳める）: 止まった段・エラーの種類・エラーコード・トレース ID・時刻（製品の追加の項目）
 * 製品は自分の失敗の情報（SSE の error・Run の失敗のイベント・ジョブのエラー）をこの形
 * （`ProcessFailure`）に写すだけにし、並べ方・文の位置・「詳細」の項目は部品が持つ。
 * 作った結果を保存できなかったなど、処理は終わったが一部が欠けたときは `severity="warning"`
 * （結果は上に残したまま、欠けたことと理由を出す）。
 */

/** 処理の失敗の情報（製品が写す共通の形）。 */
export interface ProcessFailure {
  /** 何が起きたか（1 文目。止まった段を含める。例「回答の記録の保存で失敗しました。」）。 */
  summary: string;
  /** 原因（利用者の言葉。backend の利用者向けの文）。`summary` と同じなら渡さない。 */
  cause?: string | null;
  /**
   * 原因の平易な説明（コードから引ける具体的な説明。例 ORA-00904 →「列名・識別子が存在しません」）。`cause` が DB・
   * 外部の原文のとき、その下に別の段落で出す。汎用の説明（「エラーが発生しました」）は渡さない。
   */
  explanation?: string | null;
  /** 次の操作（例「もう一度送信してください。続くときは、トレース ID を添えて管理者に連絡してください。」）。 */
  nextAction?: string | null;
  /** 止まった段の名前（処理の経過の段と同じ名前）。 */
  failedStep?: string | null;
  /** エラーの種類（例外の型。`DatabaseError` など）。 */
  errorType?: string | null;
  /** エラーコード（`ORA-03146`・`JOB_FAILED` など）。 */
  errorCode?: string | null;
  /** トレース ID / 実行 ID（実行履歴・ログを引く鍵）。 */
  traceId?: string | null;
  /** 失敗した時刻（ISO 8601）。 */
  occurredAt?: string | null;
  /** 製品の追加の項目（元のメッセージ・理由など）。共通の項目の後に並べる。 */
  extraDetails?: readonly ApiErrorDetail[];
}

export interface ProcessFailureLabels {
  /** 畳む見出し。 */
  details: string;
  failedStep: string;
  errorType: string;
  errorCode: string;
  traceId: string;
  occurredAt: string;
  /** 時刻の表記（既定 `formatRunDateTime` の秒まで）。 */
  formatDateTime: (iso: string) => string;
}

export const DEFAULT_PROCESS_FAILURE_LABELS: ProcessFailureLabels = {
  details: "詳細",
  failedStep: "止まった段",
  errorType: "エラーの種類",
  errorCode: "エラーコード",
  traceId: "トレース ID",
  occurredAt: "時刻",
  formatDateTime: (iso) => formatRunDateTime(iso, { seconds: true }),
};

/**
 * 処理の経過の最後の失敗した段の名前（止まった段。無ければ null）。名前を状態で変える段階（「SQL を生成できません
 * でした」）は名詞の名前（`name`。「SQL の生成」）を使う。チャットの段階（`ChatProgressStep`）と実行の詳細の段
 * （`RunTimelineStep`）のどちらでも使える。
 */
export function failedStepName(steps: readonly { label: string; status: string; name?: string }[]): string | null {
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    const step = steps[index];
    if (step.status === "failed") return step.name || step.label;
  }
  return null;
}

function resolveLabels(labels?: Partial<ProcessFailureLabels>): ProcessFailureLabels {
  return { ...DEFAULT_PROCESS_FAILURE_LABELS, ...labels };
}

function present(value: string | null | undefined): value is string {
  return typeof value === "string" && value.trim() !== "";
}

/**
 * 失敗の技術的な詳細の並び（止まった段・エラーの種類・エラーコード・トレース ID・時刻・製品の項目）。値の無い項目は
 * 並べない。`ProcessFailureNotice` の「詳細」と、実行の詳細の技術的な詳細（`processFailureFacts`）が同じ並びを使う。
 */
export function processFailureDetails(
  failure: ProcessFailure,
  labels?: Partial<ProcessFailureLabels>
): ApiErrorDetail[] {
  const resolved = resolveLabels(labels);
  const details: ApiErrorDetail[] = [];
  if (present(failure.failedStep)) details.push({ label: resolved.failedStep, value: failure.failedStep });
  if (present(failure.errorType)) details.push({ label: resolved.errorType, value: failure.errorType });
  if (present(failure.errorCode)) details.push({ label: resolved.errorCode, value: failure.errorCode });
  if (present(failure.traceId)) details.push({ label: resolved.traceId, value: failure.traceId });
  if (present(failure.occurredAt)) {
    details.push({ label: resolved.occurredAt, value: resolved.formatDateTime(failure.occurredAt) });
  }
  for (const item of failure.extraDetails ?? []) {
    if (present(item.value)) details.push(item);
  }
  return details;
}

const MONO_DETAIL_KEYS = new Set(["errorType", "errorCode", "traceId"]);

/**
 * 実行の詳細の「技術的な詳細」に並べる事実（`RunFactList`）。チャットの失敗のカードの「詳細」と同じ項目・同じ名前
 * （止まった段・エラーの種類・エラーコード・トレース ID・時刻）。トレース ID は概要の実行 ID と同じなら渡さない。
 */
export function processFailureFacts(
  failure: ProcessFailure,
  labels?: Partial<ProcessFailureLabels>
): RunFact[] {
  const resolved = resolveLabels(labels);
  const entries: [string, string | null | undefined][] = [
    ["failedStep", failure.failedStep],
    ["errorType", failure.errorType],
    ["errorCode", failure.errorCode],
    ["traceId", failure.traceId],
    ["occurredAt", present(failure.occurredAt) ? resolved.formatDateTime(failure.occurredAt) : null],
  ];
  const facts: RunFact[] = entries
    .filter((entry): entry is [string, string] => present(entry[1]))
    .map(([id, value]) => ({
      id: `failure-${id}`,
      label: resolved[id as keyof Omit<ProcessFailureLabels, "details" | "formatDateTime">],
      value,
      mono: MONO_DETAIL_KEYS.has(id),
    }));
  for (const [index, item] of (failure.extraDetails ?? []).entries()) {
    if (present(item.value)) facts.push({ id: `failure-extra-${index}`, label: item.label, value: item.value, wide: true });
  }
  return facts;
}

export interface ProcessFailureNoticeProps {
  failure: ProcessFailure;
  /**
   * `danger`（既定。処理が失敗し結果が無い）/ `warning`（結果はあるが一部を保存できなかった・欠けた）。
   * danger は「詳細」を開いて出し、warning は閉じて出す（messaging.md §10.3）。
   */
  severity?: "danger" | "warning";
  /** 次の操作のボタン（再送信・再試行など）。本文の下に出す。 */
  action?: ReactNode;
  /** 「詳細」を最初から開くか（既定は danger で開く・warning で閉じる）。 */
  detailsDefaultOpen?: boolean;
  labels?: Partial<ProcessFailureLabels>;
  className?: string;
  testId?: string;
}

/**
 * 処理の失敗のカード（3 製品のチャットの回答の場所・実行の詳細の概要。#2342）。
 *
 * - 1 文目（title）は何が起きたか（止まった段を含む）。トレース ID だけの表示にしない。
 * - 本文は原因 → 次の操作の順。技術的な詳細は「詳細」（`Disclosure`）に畳み、本文に混ぜない。
 * - 状態は色だけでなくトーンのアイコンと文字で示す。danger は `role="alert"`（`Banner`）。
 */
export function ProcessFailureNotice({
  failure,
  severity = "danger",
  action,
  detailsDefaultOpen,
  labels,
  className,
  testId,
}: ProcessFailureNoticeProps) {
  const resolved = resolveLabels(labels);
  const details = processFailureDetails(failure, resolved);
  const cause = present(failure.cause) && failure.cause.trim() !== failure.summary.trim() ? failure.cause : null;
  const explanation = present(failure.explanation) ? failure.explanation : null;
  const next = present(failure.nextAction) ? failure.nextAction : null;
  return (
    <div className={cn("min-w-0", className)} data-testid={testId} data-process-failure={severity}>
      <Banner severity={severity} title={failure.summary} action={action}>
        {cause || explanation || next || details.length ? (
          <div className="min-w-0 space-y-2">
            {cause ? (
              <p className="whitespace-pre-line break-words [overflow-wrap:anywhere]" data-process-failure-cause="">
                {cause}
              </p>
            ) : null}
            {explanation ? <p data-process-failure-explanation="">{explanation}</p> : null}
            {next ? <p data-process-failure-next="">{next}</p> : null}
            <ApiErrorDetailList
              details={details}
              label={resolved.details}
              defaultOpen={detailsDefaultOpen ?? severity === "danger"}
            />
          </div>
        ) : null}
      </Banner>
    </div>
  );
}

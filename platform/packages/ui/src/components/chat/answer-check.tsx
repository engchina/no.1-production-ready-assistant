import { CircleOff, ShieldCheck } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "../../lib/utils";
import { StatusBadge, type StatusVariant } from "../data/status-badge";
import { Disclosure } from "../ui/disclosure";
import { TEXT_ICON_SIZE } from "../ui/text-icon";
import { answerPassageCounts, type AnswerPassageRecords } from "./answer-passage-records";

/**
 * 資料との照合（#141。Agent のチャットと実行の詳細、RAG のチャット・RAG 検索・実行の履歴で共通）。
 *
 * 状態は 5 つ。製品は自分の照合の記録をこの状態へ写すだけにし、文言・色・アイコンはここで決める。
 * - verified: 照合が完了し、段落の印が 1 つも無い（`valid` ではなく印の有無で決める）。
 * - flagged: 段落の印・外した段落・回答全体の注意がある。印があれば「照合済み」より優先する。
 * - skipped: 照合の対象外（空の回答・確認の質問だけの回答など）。理由を添える。
 * - off: 設定で照合を止めていて、試していない。
 * - failed: 照合を試したが、時間切れ・エラー・入力の上限超えで終わらなかった。理由を添える。
 *
 * 状態は色だけで表さない（StatusBadge のアイコンと文字を必ず添える）。
 */
export type AnswerCheckState = "verified" | "flagged" | "skipped" | "off" | "failed";

export interface AnswerCheck {
  state: AnswerCheckState;
  /** flagged の段落の印の記録。件数と内訳はここから数える。 */
  passages?: AnswerPassageRecords | null;
  /** flagged で段落の印の記録の無い過去の回答の件数（0 は件数を出さない）。 */
  count?: number;
  /** バッジの横に添える理由の 1 行（skipped・off・failed）。 */
  reason?: string | null;
}

export interface AnswerCheckLabels {
  title: string;
  verified: string;
  flagged: (count: number) => string;
  flaggedNoCount: string;
  skipped: string;
  off: string;
  failed: string;
  offReason: string;
  /** 印の内訳（「未確認 2」）。 */
  breakdown: { unverified: (count: number) => string; warning: (count: number) => string; removed: (count: number) => string };
  breakdownSeparator: string;
  breakdownWrap: (text: string) => string;
}

export const DEFAULT_ANSWER_CHECK_LABELS: AnswerCheckLabels = {
  title: "資料との照合",
  verified: "照合済み",
  flagged: (count) => `照合で要確認 ${count} か所`,
  flaggedNoCount: "照合で要確認あり",
  skipped: "照合の対象外",
  off: "照合オフ",
  failed: "照合に失敗",
  offReason: "設定で照合を無効にしています",
  breakdown: {
    unverified: (count) => `未確認 ${count}`,
    warning: (count) => `要注意 ${count}`,
    removed: (count) => `矛盾で除外 ${count}`,
  },
  breakdownSeparator: "・",
  breakdownWrap: (text) => `（${text}）`,
};

function badgeOf(check: AnswerCheck, labels: AnswerCheckLabels): { variant: StatusVariant; label: string; note: string | null; off?: true } {
  switch (check.state) {
    case "verified":
      return { variant: "success", label: labels.verified, note: null };
    case "flagged": {
      const counts = answerPassageCounts(check.passages);
      const parts = [
        counts.unverified ? labels.breakdown.unverified(counts.unverified) : "",
        counts.warning ? labels.breakdown.warning(counts.warning) : "",
        counts.removed ? labels.breakdown.removed(counts.removed) : "",
      ].filter(Boolean);
      const total = check.passages ? counts.unverified + counts.warning + counts.removed : (check.count ?? 0);
      return {
        variant: "warning",
        label: total > 0 ? labels.flagged(total) : labels.flaggedNoCount,
        // 内訳が 1 種類だけなら件数と同じことの繰り返しになるので、2 種類以上のときだけ添える。
        note: parts.length > 1 ? labels.breakdownWrap(parts.join(labels.breakdownSeparator)) : null,
      };
    }
    case "skipped":
      return { variant: "neutral", label: labels.skipped, note: check.reason ?? null };
    case "off":
      return { variant: "neutral", label: labels.off, note: check.reason ?? labels.offReason, off: true };
    case "failed":
      return { variant: "warning", label: labels.failed, note: check.reason ?? null };
  }
}

/**
 * 照合の状態のバッジと、その横の内訳・理由の 1 行。表の列・概要のバッジの並び（実行の履歴）では `compact` で
 * バッジだけを出す。
 */
export function AnswerCheckBadge({
  check,
  labels = DEFAULT_ANSWER_CHECK_LABELS,
  compact = false,
  testId = "answer-check-badge",
}: {
  check: AnswerCheck;
  labels?: AnswerCheckLabels;
  compact?: boolean;
  testId?: string;
}) {
  const { variant, label, note, off } = badgeOf(check, labels);
  return (
    <>
      <span className="inline-flex" data-testid={testId} data-answer-check={check.state}>
        <StatusBadge variant={variant} label={label} icon={off ? CircleOff : true} />
      </span>
      {note && !compact ? (
        <span className="min-w-0 break-words font-normal text-fg-muted" data-testid={`${testId}-note`}>
          {note}
        </span>
      ) : null}
    </>
  );
}

/**
 * 資料との照合の行（回答の下。並びは 回答 → 資料との照合 → 回答の注意書き → 出典 → 評価）。見出し・製品固有の
 * バッジ（`badges`。Agent の回答の対応など）・照合の状態のバッジを 1 行に置く。`children` があるときだけ、
 * 同じ行を開いて中身（Agent の確かめた条件など）を見られるようにする。
 * `check` も `badges` も `children` も無ければ何も出さない（照合の記録の無い過去の回答）。
 */
export function AnswerCheckPanel({
  check,
  badges,
  children,
  labels = DEFAULT_ANSWER_CHECK_LABELS,
  className,
  testId = "answer-check",
}: {
  check: AnswerCheck | null;
  badges?: ReactNode;
  children?: ReactNode;
  labels?: AnswerCheckLabels;
  className?: string;
  testId?: string;
}) {
  if (!check && !badges && !children) return null;
  // 見出しとバッジは同じ行に置き、狭い幅ではバッジを次の行へ折り返す（見出しの文字を縦に潰さない）。
  // バッジは上下にはみ出させて行の高さを文字の行の高さに保つ（見出しのアイコンを文字の 1 行目の中心にそろえる。#2371）。
  const heading = (
    <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-1 [&_[data-status-variant]]:-my-0.5">
      <span className="whitespace-nowrap">{labels.title}</span>
      {badges}
      {check ? <AnswerCheckBadge check={check} labels={labels} testId={`${testId}-badge`} /> : null}
    </span>
  );
  if (children) {
    return (
      <Disclosure
        variant="plain"
        size="sm"
        icon={ShieldCheck}
        summary={heading}
        summaryProps={{ "data-testid": `${testId}-summary` }}
        className={className}
        data-testid={testId}
      >
        {children}
      </Disclosure>
    );
  }
  // 開いても中身が無いときは、畳まずに見出しだけを出す（⌄ を出さない）。
  return (
    <p className={cn("flex items-start gap-1.5 text-xs font-medium text-fg", className)} data-testid={testId}>
      <span className="pr-icon-line text-fg-muted">
        <ShieldCheck size={TEXT_ICON_SIZE.xs} aria-hidden="true" />
      </span>
      <span className="min-w-0 break-words">{heading}</span>
    </p>
  );
}

import { CircleHelp, CircleMinus, TriangleAlert, type LucideIcon } from "lucide-react";
import { useId, useState, type ReactNode } from "react";

import { cn } from "../../lib/utils";
import { StatusBadge } from "../data/status-badge";
import { Banner } from "../ui/banner";
import { TEXT_ICON_SIZE } from "../ui/text-icon";
import {
  answerPassageCounts,
  answerPassageDisplayText,
  type AnswerPassageMarkGroup,
  type AnswerPassageMarkState,
  type AnswerPassageReason,
  type AnswerPassageRecords,
  type AnswerWarning,
} from "./answer-passage-records";

/**
 * 回答の段落の印の見た目（#2738。RAG のチャット・RAG 検索・Agent のチャットと実行の詳細で共通）。
 *
 * - 印の段落: 左に細い区切りの線（未確認 = warning の色、要注意 = 控えめの線）と、段落の末尾の小さな印
 *   （`CircleHelp`「未確認」/ `TriangleAlert`「要注意」）。印を押すと理由（backend の `label` と `detail`）を開く。
 * - 節「根拠と矛盾し除いた内容」「確認が必要な点」は `AnswerSections` が本文の後ろに組み立てる。
 * - 回答全体の注意（`answer_warnings`）は本文の上の warning の `Banner`。
 * - バッジ（「未確認 N 件」「要注意 N 件」「矛盾で除外 N 件」。0 件は出さない）は `AnswerPassageBadges`。
 *
 * 状態は色だけで表さない（アイコンと文字を必ず添える）。文言は製品が i18n で渡す（既定は日本語）。
 */

export interface AnswerPassageLabels {
  /** 印の短いラベル（「未確認」「要注意」）。 */
  state: Record<AnswerPassageMarkState, string>;
  /** 理由の無い印を開いたときの説明。 */
  stateDescription: Record<AnswerPassageMarkState, string>;
  /** 印のボタンの読み上げに足す語（見た目には出さない。例:「の理由」）。 */
  toggleHint: string;
  /** 節「根拠と矛盾し除いた内容」の見出しと説明。 */
  removedTitle: string;
  removedNote: string;
  /** 節「確認が必要な点」の見出しと説明。 */
  marksTitle: string;
  marksNote: string;
  /** 「確認が必要な点」の項目を押して本文の段落へ移るボタンの読み上げ名。`quote` は表示している引用。 */
  jumpLabel: (quote: string) => string;
  /** 回答全体の注意が複数あるときの見出し。 */
  warningsTitle: string;
  /** バッジ。 */
  badge: {
    unverified: (count: number) => string;
    warning: (count: number) => string;
    removed: (count: number) => string;
  };
}

export const DEFAULT_ANSWER_PASSAGE_LABELS: AnswerPassageLabels = {
  state: { unverified: "未確認", warning: "要注意" },
  stateDescription: {
    unverified: "この段落は根拠で確かめられていません。",
    warning: "この段落はシステムの検査で注意が必要とされました。",
  },
  toggleHint: "の理由",
  removedTitle: "根拠と矛盾し除いた内容",
  removedNote: "根拠と矛盾したため、次の内容は回答に載せていません。",
  marksTitle: "確認が必要な点",
  marksNote: "次の段落は根拠で確かめられていないか、注意が必要です。押すと本文の段落へ移ります。",
  jumpLabel: (quote) => `${quote}（本文の段落へ移動）`,
  warningsTitle: "回答全体に注意が必要です",
  badge: {
    unverified: (count) => `未確認 ${count} 件`,
    warning: (count) => `要注意 ${count} 件`,
    removed: (count) => `矛盾で除外 ${count} 件`,
  },
};

/** 印の状態のアイコン。 */
export const ANSWER_PASSAGE_STATE_ICON: Record<AnswerPassageMarkState, LucideIcon> = {
  unverified: CircleHelp,
  warning: TriangleAlert,
};

// 区切りの線（未確認は warning の色、要注意は控えめの線。どちらも 3:1 以上の線の色）。
const STATE_LINE: Record<AnswerPassageMarkState, string> = {
  unverified: "border-warning-fg",
  warning: "border-border-control",
};

// 印の文字の色と、押せることを示す hover の地。
const STATE_TOGGLE: Record<AnswerPassageMarkState, string> = {
  unverified: "text-warning-fg hover:bg-warning-subtle",
  warning: "text-fg-muted hover:bg-surface-hover",
};

/** 引用の短い形。可視性の判定は記録の解析・件数と共通にする。 */
export function answerPassageQuote(text: string, limit = 80): string {
  const flat = answerPassageDisplayText(text);
  return `「${flat.length > limit ? `${flat.slice(0, limit)}…` : flat}」`;
}

/** 理由の 1 行（「根拠と矛盾：…」）。 */
export function answerPassageReasonText(reason: AnswerPassageReason): string {
  return reason.detail ? `${reason.label}：${reason.detail}` : reason.label;
}

/** 本文の段落へ移る（段落を画面の中ほどに出し、フォーカスを移す。動きを減らす設定では瞬時に移る）。 */
export function focusAnswerPassage(anchorId: string): void {
  const target = typeof document === "undefined" ? null : document.getElementById(anchorId);
  if (!target) return;
  const reduce =
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
      : true;
  if (typeof target.scrollIntoView === "function") {
    target.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
  }
  target.focus({ preventScroll: true });
}

/**
 * 印の付いた段落の入れ物。左の区切りの線・本文の後ろの印（`render` に渡す）・開いた理由を持つ。
 * `render` は段落の本文の要素（`<p>` など）を返し、末尾に渡された印を置く。
 */
export function AnswerPassageMarked({
  group,
  anchorId,
  labels = DEFAULT_ANSWER_PASSAGE_LABELS,
  render,
  className,
}: {
  group: AnswerPassageMarkGroup;
  /** 「確認が必要な点」から移る先の id。 */
  anchorId: string;
  labels?: AnswerPassageLabels;
  render: (marker: ReactNode) => ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const reasonsId = useId();
  const Icon = ANSWER_PASSAGE_STATE_ICON[group.state];
  const marker = (
    <>
      {" "}
      <button
        type="button"
        aria-expanded={open}
        aria-controls={reasonsId}
        onClick={() => setOpen((value) => !value)}
        className={cn(
          // 文の後ろに続けて置く小さな印。タッチ端末では見た目を変えずに当たり判定を 44px にする（pr-touch-target）。
          "pr-touch-target relative inline-flex items-center gap-0.5 whitespace-nowrap rounded-sm px-1 align-baseline text-xs font-medium leading-normal",
          STATE_TOGGLE[group.state],
        )}
        data-testid="answer-passage-mark-toggle"
      >
        <Icon size={TEXT_ICON_SIZE.xs} aria-hidden className="shrink-0" />
        {labels.state[group.state]}
        <span className="sr-only">{labels.toggleHint}</span>
      </button>
    </>
  );
  return (
    <div
      id={anchorId}
      // 「確認が必要な点」から移ったときにフォーカスを受ける（Tab の順には入れない）。
      tabIndex={-1}
      className={cn("min-w-0 scroll-mt-4 border-l-2 pl-2.5", STATE_LINE[group.state], className)}
      data-testid="answer-passage-mark"
      data-passage-state={group.state}
    >
      {render(marker)}
      {open ? (
        <div
          id={reasonsId}
          className="mt-1.5 whitespace-normal rounded-md border border-border bg-surface-sunken px-2.5 py-2 text-xs leading-relaxed [overflow-wrap:anywhere]"
          data-testid="answer-passage-mark-reasons"
        >
          {group.reasons.length ? (
            <ul className="space-y-1">
              {group.reasons.map((reason, index) => (
                <li key={`${reason.code}:${index}`}>
                  <span className="font-medium text-fg">{reason.label}</span>
                  {reason.detail ? <span className="block text-fg-muted">{reason.detail}</span> : null}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-fg-muted">{labels.stateDescription[group.state]}</p>
          )}
        </div>
      ) : (
        // 閉じている間も aria-controls の先を残す（中身は無い）。
        <div id={reasonsId} hidden />
      )}
    </div>
  );
}

/** 回答全体の注意（本文の上の warning の Banner）。無ければ何も出さない。 */
export function AnswerPassageWarnings({
  warnings,
  labels = DEFAULT_ANSWER_PASSAGE_LABELS,
  testId = "answer-passage-warnings",
}: {
  warnings: readonly AnswerWarning[];
  labels?: AnswerPassageLabels;
  testId?: string;
}) {
  if (!warnings.length) return null;
  const [first] = warnings;
  return (
    <div data-testid={testId}>
      {warnings.length === 1 ? (
        <Banner severity="warning" title={first.label}>
          {first.detail || undefined}
        </Banner>
      ) : (
        <Banner severity="warning" title={labels.warningsTitle}>
          <ul className="list-disc space-y-0.5 pl-5">
            {warnings.map((warning, index) => (
              <li key={`${warning.code}:${index}`} className="break-words">
                {answerPassageReasonText(warning)}
              </li>
            ))}
          </ul>
        </Banner>
      )}
    </div>
  );
}

/**
 * 印の数のバッジ（「未確認 N 件」「要注意 N 件」「矛盾で除外 N 件」）。0 件のバッジは出さず、どれも 0 件なら null。
 * 回答の対応のバッジと同じ行に置く（Agent の「資料との照合」・実行の詳細、RAG の回答の案内）。
 */
export function AnswerPassageBadges({
  records,
  labels = DEFAULT_ANSWER_PASSAGE_LABELS,
  className,
  testId = "answer-passage-badges",
}: {
  records: AnswerPassageRecords | null | undefined;
  labels?: AnswerPassageLabels;
  className?: string;
  testId?: string;
}) {
  const counts = answerPassageCounts(records);
  const badges: { key: keyof typeof counts; node: ReactNode }[] = [];
  if (counts.unverified > 0) {
    badges.push({
      key: "unverified",
      node: <StatusBadge variant="warning" icon={CircleHelp} label={labels.badge.unverified(counts.unverified)} />,
    });
  }
  if (counts.warning > 0) {
    badges.push({
      key: "warning",
      node: <StatusBadge variant="neutral" icon={TriangleAlert} label={labels.badge.warning(counts.warning)} />,
    });
  }
  if (counts.removed > 0) {
    badges.push({
      key: "removed",
      node: <StatusBadge variant="danger" icon={CircleMinus} label={labels.badge.removed(counts.removed)} />,
    });
  }
  if (!badges.length) return null;
  return (
    <span className={cn("inline-flex flex-wrap items-center gap-1.5", className)} data-testid={testId}>
      {badges.map((badge) => (
        <span key={badge.key} className="inline-flex" data-testid={`${testId}-${badge.key}`}>
          {badge.node}
        </span>
      ))}
    </span>
  );
}

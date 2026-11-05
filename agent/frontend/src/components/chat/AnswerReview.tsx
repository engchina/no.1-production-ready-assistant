import { useId, type ReactNode } from "react";
import { Info } from "lucide-react";
import { AnswerCheckPanel, StatusBadge, TEXT_ICON_SIZE, type AnswerCheck, type StatusVariant } from "@production-ready/ui";

import type { AnswerReview, ReviewOutcome, ReviewValidation } from "@/lib/answer-review";
import { t, type I18nKey } from "@/lib/i18n";

/**
 * 資料との照合（#1286・#2361・#141）。回答の下に、照合の結果（回答の対応と照合のバッジ）を 1 行で出す。行の器・
 * 照合の 5 状態のバッジは RAG と共通の `AnswerCheckPanel`。確かめた条件・確認待ちの質問・
 * 足りない情報があるときだけ、同じ行を開いて見られるようにする。印の付いた段落・外した段落の一覧は回答の本文の節
 * 「確認が必要な点」「根拠と矛盾し除いた内容」の 1 か所だけに出し、ここには件数のバッジだけを出す（#2738）。
 * 成果物の JSON・内部の語は出さない（handoff §13。元の JSON は実行の詳細の監査ログで、監査の閲覧の権限のある利用者だけが見る。#2403）。
 */
export function AnswerReviewPanel({ review, testId }: { review: AnswerReview; testId: string }) {
  const hasDetails =
    review.conditions.length > 0 || review.clarifications.length > 0 || review.gaps.length > 0;
  return (
    <>
      {review.limitReached ? <LimitNote testId={`${testId}-limit`} /> : null}
      <AnswerCheckPanel
        check={review.validation ? answerCheck(review.validation) : null}
        badges={review.outcome ? <OutcomeBadge outcome={review.outcome} testId={`${testId}-outcome`} /> : null}
        testId={testId}
      >
        {hasDetails ? <AnswerReviewSections review={review} /> : null}
      </AnswerCheckPanel>
    </>
  );
}

/** 資料との照合の中身（チャットの回答の下で使う）。 */
function AnswerReviewSections({ review }: { review: AnswerReview }) {
  return (
    <div className="space-y-3 text-xs [overflow-wrap:anywhere]">
      {review.conditions.length > 0 ? (
        <ReviewGroup title={t("chat.review.conditions")} testId="answer-review-conditions">
          <ul className="space-y-1">
            {review.conditions.map((condition) => (
              <li key={condition.key} className="break-words text-fg">
                <span className="font-medium">{condition.label}</span>
                {t("chat.review.separator")}
                {condition.value}
                {condition.source ? (
                  <span className="text-fg-muted">{t("chat.review.sourceWrap", { source: t("chat.review.source.user") })}</span>
                ) : null}
                {condition.previousValue ? (
                  <span className="block text-fg-muted">
                    {t("chat.review.previousValue", { value: condition.previousValue })}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </ReviewGroup>
      ) : null}

      {review.clarifications.length > 0 ? (
        <ReviewGroup title={t("chat.review.clarifications")} testId="answer-review-clarifications">
          <ul className="space-y-1">
            {review.clarifications.map((item) => (
              <li key={item.key} className="break-words text-fg">
                {item.question}
                {item.options.length > 0 ? (
                  <span className="block text-fg-muted">
                    {t("chat.review.options", { options: item.options.join(" / ") })}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </ReviewGroup>
      ) : null}

      {review.gaps.length > 0 ? (
        <ReviewGroup title={t("chat.review.gaps")} testId="answer-review-gaps">
          <ul className="list-disc space-y-1 pl-4">
            {review.gaps.map((gap, index) => (
              <li key={`${index}:${gap}`} className="break-words text-fg">
                {gap}
              </li>
            ))}
          </ul>
        </ReviewGroup>
      ) : null}

    </div>
  );
}

function ReviewGroup({ title, testId, children }: { title: string; testId: string; children: ReactNode }) {
  const id = useId();
  return (
    <div role="group" aria-labelledby={id} className="space-y-1" data-testid={testId}>
      <p id={id} className="font-medium text-fg-muted">
        {title}
      </p>
      {children}
    </div>
  );
}

/** 予算の上限に達したときの控えめな案内（内部の語は出さない）。 */
function LimitNote({ testId }: { testId: string }) {
  return (
    <p className="flex items-start gap-1.5 text-xs text-fg-muted" data-testid={testId}>
      {/* 印は文字の大きさで、1 行目の中心にそろえる（#2371）。 */}
      <span className="pr-icon-line">
        <Info size={TEXT_ICON_SIZE.xs} aria-hidden="true" />
      </span>
      <span className="min-w-0 break-words">{t("chat.review.limitReached")}</span>
    </p>
  );
}

/**
 * 回答の対応 → StatusBadge の対応表（#1314）。文言と色は RAG の回答の詳細（`outcomeBadge`。#1252）と同じにする
 * （3 製品で同じ機能は同じ書き方）。「答えた」は出さない（`ReviewOutcome` に含めない）。
 */
const OUTCOME_BADGE: Record<ReviewOutcome, { variant: StatusVariant; label: I18nKey }> = {
  conditional: { variant: "warning", label: "chat.review.outcome.conditional" },
  needs_clarification: { variant: "info", label: "chat.review.outcome.needsClarification" },
  needs_environment_data: { variant: "warning", label: "chat.review.outcome.needsEnvironmentData" },
  needs_human: { variant: "warning", label: "chat.review.outcome.needsHuman" },
  insufficient_evidence: { variant: "danger", label: "chat.review.outcome.insufficientEvidence" },
};

/** 回答の対応のバッジ（チャットの資料との照合の見出しと、実行の詳細の回答で使う。アイコン付き）。 */
export function OutcomeBadge({ outcome, testId }: { outcome: ReviewOutcome; testId: string }) {
  const { variant, label } = OUTCOME_BADGE[outcome];
  return (
    <span className="inline-flex" data-testid={testId}>
      <StatusBadge variant={variant} label={t(label)} />
    </span>
  );
}

/** 成果物「資料との照合」の結果 → 共通の照合の 5 状態（#141）。Agent は設定で照合を止めないので off は無い。 */
export function answerCheck(validation: ReviewValidation): AnswerCheck {
  switch (validation.state) {
    case "verified":
      return { state: "verified" };
    case "marked":
      return { state: "flagged", passages: validation.passages };
    case "withheld":
      // 段落の印の記録の無い過去の Run（#2738）。件数は本文の節の項目の数（節と一致させる。#2361）。節の記録が無い
      // 古い Run は外した段落の数。本文を載せなかった Run は件数なし。
      return { state: "flagged", count: validation.withheldAll ? 0 : validation.listedItems || validation.withheldClaims };
    case "unvalidated":
      return {
        state: "failed",
        reason: validation.cause === "noEvidence" ? t("chat.review.message.noEvidence") : t("chat.review.message.unvalidated"),
      };
    case "skipped":
      return { state: "skipped", reason: skippedReason(validation) };
  }
}

function skippedReason(validation: ReviewValidation): string {
  if (validation.cause === "emptyAnswer") return t("chat.review.message.skippedEmpty");
  if (validation.cause === "clarification") return t("chat.review.message.skippedClarification");
  if (validation.cause === "absenceOnly") return t("chat.review.message.skippedAbsenceOnly");
  if (validation.cause === "ragDeclined") return t("chat.review.message.skippedRagDeclined");
  if (validation.cause === "noToolGrounding") return t("chat.review.message.skippedNoToolGrounding");
  return t("chat.review.message.skipped");
}

import { useId, type ReactNode } from "react";
import { Info, ShieldCheck } from "lucide-react";
import { AnswerPassageBadges, Disclosure, StatusBadge, TEXT_ICON_SIZE, type StatusVariant } from "@production-ready/ui";

import type { AnswerReview, ConditionSource, ReviewOutcome, ReviewValidation } from "@/lib/answer-review";
import { answerPassageLabels } from "@/lib/answer-structure";
import { t, type I18nKey } from "@/lib/i18n";

/**
 * 資料との照合（#1286・#2361）。回答の下に、照合の結果（回答の対応と照合のバッジ）を 1 行で出す。確かめた条件・
 * 確認待ちの質問・使った業務ガイド・足りない情報、照合できなかった・対象外の理由があるときだけ、同じ行を開いて
 * 見られるようにする（出典と同じ plain の Disclosure）。印の付いた段落・外した段落の一覧は回答の本文の節
 * 「確認が必要な点」「根拠と矛盾し除いた内容」（段落の印の記録の無い過去の Run は「資料から確認できなかった事項」）の
 * 1 か所だけに出し、ここには件数のバッジだけを出す（#2738）。
 * 成果物の JSON・内部の語は出さない（handoff §13。元の JSON は実行の詳細の監査ログで、監査の閲覧の権限のある利用者だけが見る。#2403）。
 */
export function AnswerReviewPanel({ review, testId }: { review: AnswerReview; testId: string }) {
  const hasDetails =
    (review.validation !== null && validationMessage(review.validation) !== null) ||
    review.conditions.length > 0 ||
    review.clarifications.length > 0 ||
    review.guide !== null ||
    review.gaps.length > 0;
  // 見出しとバッジは同じ行に置き、狭い幅ではバッジを次の行へ折り返す（見出しの文字を縦に潰さない）。
  // バッジは上下にはみ出させて行の高さを文字の行の高さに保つ（見出しのアイコンを文字の 1 行目の中心にそろえる。#2371）。
  const heading = (
    <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-1 [&_[data-status-variant]]:-my-0.5">
      <span className="whitespace-nowrap">{t("chat.review.title")}</span>
      {review.outcome ? <OutcomeBadge outcome={review.outcome} testId={`${testId}-outcome`} /> : null}
      {review.validation ? <ValidationBadge validation={review.validation} /> : null}
    </span>
  );
  return (
    <>
      {review.limitReached ? <LimitNote testId={`${testId}-limit`} /> : null}
      {hasDetails ? (
        <Disclosure
          variant="plain"
          size="sm"
          icon={ShieldCheck}
          summary={heading}
          summaryProps={{ "data-testid": `${testId}-summary` }}
          data-testid={testId}
        >
          <AnswerReviewSections review={review} />
        </Disclosure>
      ) : review.outcome || review.validation ? (
        // 開いても中身が無い（対応と照合のバッジだけ）ときは、畳まずに見出しだけを出す（⌄ を出さない）。
        <p className="flex items-start gap-1.5 text-xs font-medium text-fg" data-testid={testId}>
          <span className="pr-icon-line text-fg-muted">
            <ShieldCheck size={TEXT_ICON_SIZE.xs} aria-hidden="true" />
          </span>
          <span className="min-w-0 break-words">{heading}</span>
        </p>
      ) : null}
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
                  <span className="text-fg-muted">{t("chat.review.sourceWrap", { source: sourceLabel(condition.source) })}</span>
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

      {review.guide ? (
        <ReviewGroup title={t("chat.review.guide")} testId="answer-review-guide">
          <p className="break-words text-fg">
            {review.guide.title || t("chat.review.guideUntitled")}
            {review.guide.revision ? (
              <span className="text-fg-muted">{t("chat.review.guideRevision", { revision: review.guide.revision })}</span>
            ) : null}
          </p>
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

      {review.validation ? <ValidationGroup validation={review.validation} /> : null}
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

/**
 * 照合の結果の説明（照合できなかった・対象外の理由だけ。#2361）。見出しのバッジと同じ内容（小見出し・バッジ）と、
 * 回答の本文の節にある除いた内容・その説明は出し直さない。
 */
function ValidationGroup({ validation }: { validation: ReviewValidation }) {
  const message = validationMessage(validation);
  if (message === null) return null;
  return (
    <p className="break-words text-fg" data-testid="answer-review-validation">
      {message}
    </p>
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

function sourceLabel(source: Exclude<ConditionSource, null>): string {
  return source === "user" ? t("chat.review.source.user") : t("chat.review.source.question");
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

/**
 * 照合の結果のバッジ（状態の対応表。色だけに頼らず StatusBadge のアイコンを付ける）。段落の印の記録のある Run（#2738）は
 * 「未確認 N 件」「要注意 N 件」「矛盾で除外 N 件」（0 件は出さない。RAG と同じ共有の部品）。
 */
function ValidationBadge({ validation }: { validation: ReviewValidation }) {
  if (validation.state === "marked") {
    return <AnswerPassageBadges records={validation.passages} labels={answerPassageLabels()} testId="answer-review-passages" />;
  }
  const { variant, label } = validationBadge(validation);
  return <StatusBadge variant={variant} label={label} />;
}

function validationBadge(validation: ReviewValidation): { variant: StatusVariant; label: string } {
  switch (validation.state) {
    case "verified":
      return { variant: "success", label: t("chat.review.state.verified") };
    case "marked":
      // 印の数のバッジ（ValidationBadge が出す）。ここには来ない。
      return { variant: "warning", label: t("chat.review.state.unverifiedPointsNoCount") };
    case "withheld": {
      // 段落の印の記録の無い過去の Run（#2738）。件数は本文の節の項目の数（節と一致させる。#2361）。節の記録が無い
      // 古い Run は外した段落の数。本文を載せなかった Run は件数なし。
      const count = validation.withheldAll ? 0 : validation.listedItems || validation.withheldClaims;
      return count > 0
        ? { variant: "warning", label: t("chat.review.state.unverifiedPoints", { count }) }
        : { variant: "warning", label: t("chat.review.state.unverifiedPointsNoCount") };
    }
    case "unvalidated":
      return { variant: "warning", label: t("chat.review.state.unvalidated") };
    case "skipped":
      return { variant: "neutral", label: t("chat.review.state.skipped") };
  }
}

/**
 * 開いた中に出す説明。照合できなかった・対象外の理由だけで、照合済み・除いた内容があるとき（バッジと本文の節で
 * 分かる）は null。
 */
function validationMessage(validation: ReviewValidation): string | null {
  switch (validation.state) {
    case "verified":
    case "marked":
    case "withheld":
      return null;
    case "unvalidated":
      return validation.cause === "noEvidence"
        ? t("chat.review.message.noEvidence")
        : t("chat.review.message.unvalidated");
    case "skipped":
      if (validation.cause === "emptyAnswer") return t("chat.review.message.skippedEmpty");
      if (validation.cause === "clarification") return t("chat.review.message.skippedClarification");
      if (validation.cause === "absenceOnly") return t("chat.review.message.skippedAbsenceOnly");
      if (validation.cause === "ragDeclined") return t("chat.review.message.skippedRagDeclined");
      if (validation.cause === "noToolGrounding") return t("chat.review.message.skippedNoToolGrounding");
      return t("chat.review.message.skipped");
  }
}

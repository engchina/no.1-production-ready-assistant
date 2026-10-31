// 回答前の確認の画面（#1439 / #1442 / #1715 / #1761）。SQL 生成の画面（「回答前の確認」のボタンで開く）とチャット
// （送信のときに自動で始める）が、この 1 つの部品で同じ表示・同じ操作・同じ文言を出す。
//
// 画面ごとに違うのは入口（いつ始めるか・session の作り方・SQL 生成の画面の業務プロファイルの選択）と、置く場所に
// 合わせた操作部品の高さだけ。検査中・失敗（理由・「詳細」・もう一度確認・確認せずに生成）・1 件ずつの確認事項・
// 確認済みの一覧からの直し・要件のまとめ・確かめて生成するカード（対象の選び直し・その他（自由入力）・「この内容で
// SQL を生成して実行」）・終わった確認は、ここで決める。確認事項が 0 件でも、利用者が「この内容で SQL を生成して
// 実行」を押すまで要件を生成に渡さない（#1761）。確認事項と確かめて生成するカードは 3 製品共通の回答前の確認の
// カード（`ClarificationCard`。#1839）で出す。
//
// どちらの画面も「確認をやめる」を出さない（#2303 / #2311）。確認を終える操作は「確認せずに SQL を生成」だけに
// し、質問を取り消さずに SQL の生成へ進む。AI の処理中（要件の下書き・回答の後の再検査）も、経過の行の同じ操作で
// 待たずに進める。「この内容で回答」を押したら、カードがフォームと確認事項の警告を消し、答えた内容と経過の行 1 つ
// に置き換える。経過時間はその処理を始めた時から数える（利用者が答えていた時間を含めない）。
import type { ReactNode } from "react";
import { CheckCircle2, MessageSquareText, Sparkles } from "lucide-react";

import { Banner, Button, ProcessingIndicator, StatusBadge } from "@production-ready/ui";

import { t } from "@/lib/i18n";

import {
  RequirementAnswerProgress,
  RequirementCard,
  RequirementCheckFailure,
  RequirementErrorBanner,
  RequirementItemsList,
  requirementCardConfirming,
  requirementPhaseBadge,
  RequirementStateBanners,
  RequirementSummary,
  type RequirementCheckRunner,
  type RequirementSessionController,
} from "./RequirementClarification";

/** 検査の段階（業務プロファイルの推薦 → 確定 → 要件の下書き）。チャットは下書きだけ。 */
export type RequirementStartPhase = "recommend_profile" | "confirm_profile" | "prepare_questions";

const START_PHASE_LABELS: Record<RequirementStartPhase, Parameters<typeof t>[0]> = {
  recommend_profile: "nl2sql.clarification.loading.recommendProfile",
  confirm_profile: "nl2sql.clarification.loading.confirmProfile",
  prepare_questions: "nl2sql.clarification.loading.prepareQuestions",
};

export interface RequirementClarificationViewProps {
  controller: RequirementSessionController;
  check: RequirementCheckRunner;
  startPhase: RequirementStartPhase;
  /** 「もう一度確認」（検査をやり直す）。 */
  onRetry: () => void;
  /** 「確認せずに SQL を生成」（確認を終える唯一の操作。AI の処理中の経過の行にも出す。#2303 / #2311）。 */
  onSkip: () => void;
  /** 確かめた要件で次へ進む（反映 / SQL の生成）。 */
  onApply: () => void;
  /**
   * SQL を実行できる（実行の権限がある）か。無ければ確定の操作は「この内容で SQL を生成」（生成だけ）にする。
   * 画面ごとの文言の違いではなく、権限による違い（#1761。文言は両方の画面で同じ）。
   */
  canExecuteSql?: boolean;
  /** 入口の前段（SQL 生成の画面の業務プロファイルの選択）。出している間は下の「中止」の行を出さない。 */
  startSlot?: ReactNode;
  /** 操作部品の高さ（置く場所の行に合わせる。SQL 生成の画面は lg、チャットは sm）。 */
  size: "sm" | "lg";
  /** 要素の id の接頭辞（同じ画面に 2 つ置かないので、画面ごとに 1 つ）。 */
  idPrefix: string;
}

export function RequirementClarificationView({
  controller,
  check,
  startPhase,
  onRetry,
  onSkip,
  onApply,
  canExecuteSql = true,
  startSlot,
  size,
  idPrefix,
}: RequirementClarificationViewProps) {
  const { session, busyAction, error, sessionEnded, state, draft, activeItem, showApply } = controller;
  const badge = state ? requirementPhaseBadge(state) : null;
  // 確定して進む操作の文言は、SQL 生成の画面とチャットで同じ（#1761。押すと確定した要件で SQL を生成して実行する）。
  const applyLabel = canExecuteSql ? t("chat.requirement.generate") : t("chat.requirement.generateOnly");
  const titleId = `${idPrefix}-guided-clarification-title`;
  // 確認のカードが「この内容で回答」を送っている間は、カードが経過の行を出す（フォームと警告は消す）。
  const cardConfirming = requirementCardConfirming(controller);
  // 確認を終える操作（終わった確認・答える項目の無い状態の出口）。
  const exitButton = (
    <Button
      type="button"
      variant="secondary"
      size={size}
      icon={MessageSquareText}
      disabled={Boolean(busyAction)}
      onClick={onSkip}
      data-testid="nl2sql-clarification-skip"
    >
      {t("chat.requirement.skip")}
    </Button>
  );
  return (
    <section
      aria-labelledby={titleId}
      className="grid min-w-0 gap-4 rounded-md border border-accent-emphasis bg-surface p-4"
      data-testid="nl2sql-guided-clarification"
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <Sparkles size={20} className="text-accent-fg" aria-hidden="true" />
          <h3 id={titleId} className="text-base font-semibold text-fg">
            {t("nl2sql.clarification.title")}
          </h3>
          {badge && !sessionEnded ? <StatusBadge variant={badge.variant} label={badge.label} /> : null}
        </div>
        <p className="mt-1 text-sm leading-6 text-fg-muted">
          {state?.message_ja || t("nl2sql.clarification.description")}
        </p>
      </div>

      <div aria-live="polite" aria-atomic="true" className="sr-only">
        {state?.message_ja}
      </div>

      {!sessionEnded ? <RequirementErrorBanner error={error} /> : null}
      {check.checkError ? (
        <RequirementCheckFailure
          error={check.checkError}
          onRetry={onRetry}
          onSkip={onSkip}
          size={size}
          testId="nl2sql-guided-clarification-error"
        />
      ) : null}
      {sessionEnded ? (
        <Banner severity="warning">{session?.error_message_ja || t("nl2sql.clarification.ended")}</Banner>
      ) : null}

      {check.checking ? (
        <div className="rounded-md border border-border bg-surface-sunken p-4" data-start-phase={startPhase}>
          <ProcessingIndicator
            active
            operationKey="guided-clarification-start"
            label={t(START_PHASE_LABELS[startPhase])}
            placement="panel"
            testId="nl2sql-guided-start-progress"
            onCancel={onSkip}
            labels={{ cancel: t("chat.requirement.skip") }}
          />
        </div>
      ) : null}
      <RequirementAnswerProgress active={busyAction === "answer" && !cardConfirming} onSkip={onSkip} />

      {startSlot}

      {/* 答えを送っている間は、送った確認事項の警告を出さない（次の応答の状態で出し直す）。 */}
      {cardConfirming ? null : (
        <RequirementStateBanners state={state} unresolvedCount={controller.unresolvedCount} />
      )}

      {activeItem ? (
        <RequirementCard
          controller={controller}
          size={size}
          applyLabel={applyLabel}
          onApply={onApply}
          onSkip={onSkip}
          onSkipWhileConfirming={onSkip}
          idPrefix={idPrefix}
        />
      ) : null}

      {state && draft ? <RequirementItemsList controller={controller} /> : null}

      <RequirementSummary controller={controller} idPrefix={idPrefix} />

      {showApply && !activeItem ? (
        <div className="grid min-w-0 gap-3 border-t border-border pt-4">
          <p className="flex items-start gap-2 text-sm text-success-fg">
            {/* 折り返しても文字の 1 行目の中心にそろえる（#2371）。 */}
            <span className="pr-icon-line">
              <CheckCircle2 size={16} aria-hidden="true" />
            </span>
            <span className="min-w-0">{t("chat.requirement.ready")}</span>
          </p>
          <RequirementCard
            controller={controller}
            size={size}
            applyLabel={applyLabel}
            onApply={onApply}
            onSkip={onSkip}
            idPrefix={idPrefix}
          />
        </div>
      ) : null}

      {!activeItem && !showApply && !startSlot && !check.checkError && !check.checking ? (
        <div className="flex flex-wrap justify-end border-t border-border pt-4">{exitButton}</div>
      ) : null}
    </section>
  );
}

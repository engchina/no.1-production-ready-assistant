import { CheckCircle2, Save, ThumbsDown, ThumbsUp } from "lucide-react";
import { useId, useState } from "react";

import { cn } from "../../lib/utils";
import { Button } from "../ui/button";
import { FieldLegend } from "../ui/field-label";
import { TextareaField } from "../ui/textarea-field";
import { ToggleChip } from "../ui/toggle-chip";

export type FeedbackRating = "helpful" | "not_helpful";

/** 保存済みの評価（製品の API の形から変換して渡す）。 */
export interface FeedbackControlsValue<R extends string = string> {
  rating: FeedbackRating;
  reason?: R | null;
  comment?: string | null;
  correctedAnswer?: string | null;
}

/** 保存する評価。コメント・修正した回答は前後の空白を除き、空なら null。「役に立った」は理由・コメントを持たない。 */
export interface FeedbackControlsSubmission<R extends string = string> {
  rating: FeedbackRating;
  reason: R | null;
  comment: string | null;
  correctedAnswer: string | null;
}

export interface FeedbackReasonOption<R extends string = string> {
  value: R;
  /** 翻訳済みの理由の名前。 */
  label: string;
}

/** 文言（すべて翻訳済み。製品の i18n から渡す）。 */
export interface FeedbackControlsLabels {
  /** 問い（例:「この回答は役に立ちましたか？」）。ボタンの組の名前にもなる。 */
  question: string;
  helpful: string;
  notHelpful: string;
  /** 保存済みの表示（例:「保存済み・変更できます」）。`compact` では出さない。 */
  savedInline: string;
  reasonLegend: string;
  commentLabel: string;
  commentPlaceholder?: string;
  /** コメントの文字数の表示。省略すると表示しない。 */
  commentCount?: (count: number, maxLength: number) => string;
  correctedAnswerLabel?: string;
  correctedAnswerPlaceholder?: string;
  correctedAnswerHelp?: string;
  save: string;
  cancel: string;
  retry: string;
  /** 保存の失敗の既定の文言（`getErrorMessage` が文言を返さないとき）。 */
  saveError: string;
}

export interface FeedbackControlsProps<R extends string = string> {
  /** 保存済みの評価。無ければ null。 */
  value: FeedbackControlsValue<R> | null | undefined;
  /** 「役に立たなかった」の理由の選択肢（並び順のまま出す）。 */
  reasons: readonly FeedbackReasonOption<R>[];
  /**
   * 保存する。解決したら理由の欄を閉じる（成功の Toast・保存した値の反映は製品が行う）。
   * reject したら `getErrorMessage` の文言と「再試行」を出す。
   */
  onSubmit: (submission: FeedbackControlsSubmission<R>) => Promise<unknown>;
  labels: FeedbackControlsLabels;
  /** 失敗の文言。undefined / 空なら `labels.saveError`。 */
  getErrorMessage?: (error: unknown) => string | null | undefined;
  /** コメントの最大文字数（既定 1000）。 */
  commentMaxLength?: number;
  /** 「修正した回答」の欄を出す（回答の評価だけ。引用の評価では出さない）。 */
  correctedAnswer?: boolean;
  /** 修正した回答の最大文字数（既定 20000）。 */
  correctedAnswerMaxLength?: number;
  /** 引用のカードの中などの小さな形。問いは読み上げだけ、右寄せ、保存済みの表示なし、上の区切り線なし。 */
  compact?: boolean;
  /** 操作できない（保存済みの評価の読み込み中など）。 */
  disabled?: boolean;
  /** 保存済みの評価を見せるだけ（ボタンは押せず、理由とコメントを文で出す）。 */
  readOnly?: boolean;
  /** コメントの欄の id（省略時は自動）。 */
  commentId?: string;
  /** 修正した回答の欄の id（省略時は自動）。 */
  correctedAnswerId?: string;
  className?: string;
  "data-testid"?: string;
}

function normalizeText(value: string | null | undefined): string | null {
  return value?.trim() || null;
}

/** 保存済みの評価と同じか（同じなら送り直さない）。 */
export function isSameFeedback<R extends string>(
  value: FeedbackControlsValue<R> | null | undefined,
  submission: FeedbackControlsSubmission<R>
): boolean {
  if (!value) return false;
  return (
    value.rating === submission.rating &&
    (value.reason ?? null) === submission.reason &&
    normalizeText(value.comment) === submission.comment &&
    normalizeText(value.correctedAnswer) === submission.correctedAnswer
  );
}

/**
 * 回答・引用への評価（#805。RAG の `FeedbackControls` と Agent の `AnswerFeedback`（#774）を 1 つにした）。
 *
 * - 「役に立った」はすぐ保存する。「役に立たなかった」は理由（必須）・コメント（任意）・修正した回答（任意）をその場で開いて保存する。
 *   付け直すと上書きする。保存済みと同じ内容なら送り直さず閉じる。
 * - 保存中は押したボタンだけが `loading`（アイコンがスピナー）になり、ほかは押せない。
 * - 失敗は部品の下に `role="alert"` の文言と「再試行」を出す（次の保存まで残す）。成功の Toast は製品が出す（messaging §4.2）。
 * - 保存の API・payload の形・権限は製品が持つ（`onSubmit`）。部品は業務の文言を持たない（`labels`）。
 */
export function FeedbackControls<R extends string = string>({
  value,
  reasons,
  onSubmit,
  labels,
  getErrorMessage,
  commentMaxLength = 1000,
  correctedAnswer: showCorrectedAnswer = false,
  correctedAnswerMaxLength = 20000,
  compact = false,
  disabled = false,
  readOnly = false,
  commentId,
  correctedAnswerId,
  className,
  "data-testid": testId,
}: FeedbackControlsProps<R>) {
  const autoId = useId();
  const [showReasons, setShowReasons] = useState(false);
  const [selectedReason, setSelectedReason] = useState<R | null>(null);
  const [comment, setComment] = useState("");
  const [correctedAnswer, setCorrectedAnswer] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState<FeedbackRating | null>(null);
  const [lastSubmission, setLastSubmission] = useState<FeedbackControlsSubmission<R> | null>(null);
  const busy = pending !== null;
  const locked = disabled || readOnly || busy;
  const rated = (rating: FeedbackRating) => value?.rating === rating;

  async function send(submission: FeedbackControlsSubmission<R>) {
    setError("");
    setLastSubmission(submission);
    setPending(submission.rating);
    try {
      await onSubmit(submission);
      setShowReasons(false);
    } catch (caught) {
      setError(getErrorMessage?.(caught) || labels.saveError);
    } finally {
      setPending(null);
    }
  }

  function submit(submission: FeedbackControlsSubmission<R>) {
    if (isSameFeedback(value, submission)) {
      setShowReasons(false);
      return;
    }
    void send(submission);
  }

  function toggleReasons() {
    setError("");
    setSelectedReason(value?.reason ?? null);
    setComment(value?.comment ?? "");
    setCorrectedAnswer(value?.correctedAnswer ?? "");
    setShowReasons((open) => !open);
  }

  const savedReason = value?.reason ? reasons.find((item) => item.value === value.reason)?.label : undefined;

  return (
    <div className={cn("min-w-0", !compact && "border-t border-border pt-3", className)} data-testid={testId}>
      <div className={cn("flex gap-2", compact ? "items-center justify-end" : "flex-wrap items-center")}>
        <span className={compact ? "sr-only" : "mr-1 text-sm font-medium text-fg"}>{labels.question}</span>
        <div className="flex gap-1" role="group" aria-label={labels.question}>
          <Button
            type="button"
            variant={rated("helpful") ? "secondary" : "ghost"}
            size="sm"
            iconOnly
            icon={ThumbsUp}
            className={rated("helpful") ? "text-success-fg" : undefined}
            aria-label={labels.helpful}
            aria-pressed={rated("helpful")}
            disabled={locked}
            loading={pending === "helpful"}
            onClick={() => submit({ rating: "helpful", reason: null, comment: null, correctedAnswer: null })}
          />
          <Button
            type="button"
            variant={rated("not_helpful") ? "secondary" : "ghost"}
            size="sm"
            iconOnly
            icon={ThumbsDown}
            className={rated("not_helpful") ? "text-danger-fg" : undefined}
            aria-label={labels.notHelpful}
            aria-pressed={rated("not_helpful")}
            aria-expanded={readOnly ? undefined : showReasons}
            disabled={locked}
            onClick={toggleReasons}
          />
        </div>
        {value && !compact && !readOnly ? (
          <span className="inline-flex items-center gap-1 text-xs text-fg-muted" role="status">
            <CheckCircle2 size={14} className="text-success-fg" aria-hidden />
            {labels.savedInline}
          </span>
        ) : null}
      </div>

      {readOnly && value?.rating === "not_helpful" && (savedReason || normalizeText(value.comment)) ? (
        <div className="mt-2 space-y-1 text-sm text-fg-muted">
          {savedReason ? <p>{`${labels.reasonLegend}: ${savedReason}`}</p> : null}
          {normalizeText(value.comment) ? (
            <p className="whitespace-pre-wrap break-words">{`${labels.commentLabel}: ${value.comment?.trim()}`}</p>
          ) : null}
        </div>
      ) : null}

      {showReasons && !readOnly ? (
        <fieldset className="mt-3 rounded-md border border-border bg-surface-sunken p-3">
          {/* 役に立たなかった理由は必須（未選択では保存できない）。コメントと修正した回答は任意なので何も付けない（#531）。 */}
          <FieldLegend required className="px-1 text-xs font-medium">
            {labels.reasonLegend}
          </FieldLegend>
          <div className="flex flex-wrap gap-1" role="group" aria-label={labels.reasonLegend}>
            {reasons.map((reason) => (
              <ToggleChip
                key={reason.value}
                selected={selectedReason === reason.value}
                disabled={busy}
                onClick={() => setSelectedReason(reason.value)}
              >
                {reason.label}
              </ToggleChip>
            ))}
          </div>
          <TextareaField
            id={commentId ?? `${autoId}-comment`}
            label={labels.commentLabel}
            className="mt-3"
            value={comment}
            maxLength={commentMaxLength}
            rows={3}
            disabled={busy}
            placeholder={labels.commentPlaceholder}
            showCount={labels.commentCount ? (count) => labels.commentCount!(count, commentMaxLength) : false}
            onChange={(event) => setComment(event.target.value)}
          />
          {showCorrectedAnswer ? (
            <TextareaField
              id={correctedAnswerId ?? `${autoId}-corrected`}
              label={labels.correctedAnswerLabel ?? ""}
              className="mt-3"
              value={correctedAnswer}
              maxLength={correctedAnswerMaxLength}
              rows={3}
              disabled={busy}
              placeholder={labels.correctedAnswerPlaceholder}
              helper={labels.correctedAnswerHelp}
              onChange={(event) => setCorrectedAnswer(event.target.value)}
            />
          ) : null}
          <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-border pt-3">
            <Button
              type="button"
              size="md"
              icon={Save}
              loading={pending === "not_helpful"}
              disabled={!selectedReason}
              onClick={() =>
                selectedReason
                  ? submit({
                      rating: "not_helpful",
                      reason: selectedReason,
                      comment: normalizeText(comment),
                      correctedAnswer: showCorrectedAnswer ? normalizeText(correctedAnswer) : null,
                    })
                  : undefined
              }
            >
              {labels.save}
            </Button>
            <Button type="button" variant="ghost" size="md" onClick={() => setShowReasons(false)}>
              {labels.cancel}
            </Button>
          </div>
        </fieldset>
      ) : null}

      {error ? (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-sm text-danger-fg" role="alert">
          <span>{error}</span>
          {lastSubmission ? (
            <Button
              type="button"
              size="sm"
              variant="secondary"
              disabled={busy}
              onClick={() => void send(lastSubmission)}
            >
              {labels.retry}
            </Button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

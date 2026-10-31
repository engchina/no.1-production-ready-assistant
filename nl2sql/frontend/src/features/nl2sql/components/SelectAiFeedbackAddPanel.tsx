import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { MessageSquareText, ThumbsDown, ThumbsUp } from "lucide-react";

import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  toast,
  StatusBadge,
  FormGrid,
  FormStatus,
  TextareaField,
} from "@production-ready/ui";

import { apiPost } from "@/lib/api";
import { useValuesChanged } from "@/lib/render-sync";
import { t } from "@/lib/i18n";
import { userFeedbackRatingBadgeLabel } from "../feedbackLabels";
import type {
  GeneratedSqlPanelData,
  HistoryItem,
  Nl2SqlResult,
  FeedbackData,
} from "../types";

type Rating = "good" | "bad";
type SelectAiFeedbackSource = (GeneratedSqlPanelData | Nl2SqlResult) & {
  original_question?: string;
  history_id?: string;
};

export function SelectAiFeedbackAddPanel({
  result,
  history,
  questionText,
  onSaved,
}: {
  result: SelectAiFeedbackSource | null;
  history: HistoryItem | null;
  questionText?: string;
  onSaved: () => void | Promise<void>;
}) {
  const [feedbackContent, setFeedbackContent] = useState("");
  const [savingRating, setSavingRating] = useState<Rating | null>(null);
  const [message, setMessage] = useState("");
  const [contentError, setContentError] = useState("");
  const saving = useRef(false);
  const currentHistoryId = useRef("");

  const generatedSql = useMemo(
    () => (result?.executable_sql || result?.generated_sql || "").trim(),
    [result?.executable_sql, result?.generated_sql]
  );
  const question = history?.question || result?.original_question || questionText || "";
  const historyId = history?.id || result?.history_id || "";
  // 最新の履歴 ID を commit 時に入れる（render 中に ref を書かない）。
  useLayoutEffect(() => { currentHistoryId.current = historyId; });

  // 対象の SQL・履歴が変わったら、入力欄を保存済みのコメントに戻す（render 中に同期する）。
  if (useValuesChanged([generatedSql, result?.original_question, history?.feedback_comment, history?.id])) {
    setFeedbackContent(history?.feedback_comment ?? "");
    setMessage("");
    setContentError("");
  }

  if (!result) return null;

  const submit = async (rating: Rating) => {
    if (saving.current) return;
    const trimmedContent = feedbackContent.trim();

    if (!question.trim()) return;
    if (!historyId) {
      setMessage(t("nl2sql.selectAiFeedbackAdd.requiresHistory"));
      return;
    }
    // 「違う」のコメントの未入力は欄の直下に出し、その欄へフォーカスする（#541）。backend も同じ規則で拒否する（#540）。
    if (rating === "bad" && !trimmedContent) {
      setMessage("");
      setContentError(t("nl2sql.selectAiFeedbackAdd.requiresContent"));
      document.getElementById("nl2sql-select-ai-feedback-content")?.focus();
      return;
    }
    setContentError("");

    saving.current = true;
    setSavingRating(rating);
    setMessage("");
    try {
      await apiPost<FeedbackData>("/api/nl2sql/feedback", {
        history_id: historyId,
        rating,
        feedback_content: trimmedContent,
        comment: trimmedContent,
      });
      try {
        await onSaved();
        toast.success(t("nl2sql.selectAiFeedbackAdd.saved"));
      } catch {
        toast.warning(t("nl2sql.selectAiFeedbackAdd.refreshFailed"));
      }
    } catch (err) {
      if (currentHistoryId.current === historyId) {
        setMessage(err instanceof Error ? err.message : t("nl2sql.selectAiFeedbackAdd.failed"));
      }
    } finally {
      saving.current = false;
      setSavingRating(null);
    }
  };

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4">
        <div className="space-y-2">
          <CardTitle className="flex items-center gap-2">
            <MessageSquareText size={20} aria-hidden="true" />
            {t("nl2sql.selectAiFeedbackAdd.title")}
          </CardTitle>
          <p className="text-sm text-fg-muted">{t("nl2sql.selectAiFeedbackAdd.description")}</p>
        </div>
        {history?.feedback_rating && (
          <div className="flex flex-wrap justify-end gap-2">
            <StatusBadge
              variant="neutral"
              label={userFeedbackRatingBadgeLabel(history.feedback_rating)}
            />
          </div>
        )}
      </CardHeader>
      <CardContent>
        {/* 生成 SQL・改善点と評価の行は FormGrid の読みやすい幅（60rem）まで（#1891）。 */}
        <FormGrid columns={1}>
          <TextareaField
            id="nl2sql-select-ai-feedback-response"
            label={t("nl2sql.selectAiFeedbackAdd.response")}
            surface="code"
            value={generatedSql}
            readOnly
            rows={12}
            width="full"
            placeholder={t("nl2sql.selectAiFeedbackAdd.responsePlaceholder")}
          />
          {/* 評価は送信ボタンで決まるので、入力の aria-required ではなく「「違う」のとき必須」のタグで条件を伝える（#531）。 */}
          <TextareaField
            id="nl2sql-select-ai-feedback-content"
            label={t("nl2sql.selectAiFeedbackAdd.content")}
            required
            requiredLabel={t("nl2sql.selectAiFeedbackAdd.contentRequiredWhenBad")}
            requiredAnnouncedByControl={false}
            disabled={savingRating !== null}
            value={feedbackContent}
            onChange={(event) => {
              setFeedbackContent(event.currentTarget.value);
              setContentError("");
            }}
            rows={3}
            width="full"
            error={contentError || undefined}
            placeholder={t("nl2sql.selectAiFeedbackAdd.contentPlaceholder")}
          />
          <div className="flex flex-wrap items-center justify-end gap-3">
            <FormStatus
              tone="danger"
              message={message}
              className="mr-auto"
            />
            {/* 評価トグル: primary は画面の主 CTA 専用のため(button spec §0.2)、
                選択状態は枠線 + aria-pressed で表現する。 */}
            <div
              role="group"
              aria-label={t("nl2sql.selectAiFeedbackAdd.rating")}
              className="flex flex-wrap items-center gap-3"
            >
              <Button
                type="button"
                variant="secondary"
                size="sm"

                aria-pressed={history?.feedback_rating === "good"}
                loading={savingRating === "good"}
                disabled={savingRating !== null}
                onClick={() => void submit("good")} icon={ThumbsUp}>
                <span>{t("nl2sql.feedback.good")}</span>
              </Button>
              <Button
                type="button"
                variant="secondary"
                size="sm"

                aria-pressed={history?.feedback_rating === "bad"}
                loading={savingRating === "bad"}
                disabled={savingRating !== null}
                onClick={() => void submit("bad")} icon={ThumbsDown}>
                <span>{t("nl2sql.feedback.bad")}</span>
              </Button>
            </div>
          </div>
        </FormGrid>
      </CardContent>
    </Card>
  );
}

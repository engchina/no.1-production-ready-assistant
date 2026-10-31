import type { ReactNode } from "react";

import { cn } from "../../lib/utils";
import { AnswerNotice } from "./answer-notice";

export interface AnswerFooterProps {
  /** 回答の注意書きの文（`AnswerNotice` に渡す。空・null なら出さない）。 */
  notice?: string | null;
  /** 注意書きの読み上げの名前（翻訳済み。例:「回答の注意書き」）。 */
  noticeLabel?: string;
  /** 回答の根拠の塊（「AI に渡した N 件 ／ 回答で引用 M 件」の `AnswerEvidence` など。製品が描く）。 */
  evidence?: ReactNode;
  /** 回答の評価（「この回答は役に立ちましたか？」の `FeedbackControls` など。製品が描く）。 */
  feedback?: ReactNode;
  className?: string;
  testId?: string;
}

/**
 * チャットの回答のカードの末尾（3 製品共通。#2707）。回答の本文の後に、
 * 回答の注意書き → 回答の根拠（AI に渡した N 件 ／ 回答で引用 M 件）→ 回答の評価 の順で並べる。
 *
 * 注意書きは回答を読む前提（AI が作ったこと・窓口）なので本文の直後に置き、根拠の一覧（既定で閉じる）と
 * 評価はその後ろにする。並びはここだけで決め、製品は中身だけを渡す。どれも無ければ何も出さない。
 */
export function AnswerFooter({
  notice,
  noticeLabel,
  evidence,
  feedback,
  className,
  testId = "answer-footer",
}: AnswerFooterProps) {
  const hasNotice = Boolean(notice?.trim());
  if (!hasNotice && !evidence && !feedback) return null;
  return (
    <div className={cn("flex min-w-0 flex-col gap-3", className)} data-testid={testId}>
      {hasNotice ? <AnswerNotice notice={notice} label={noticeLabel} /> : null}
      {evidence}
      {feedback}
    </div>
  );
}

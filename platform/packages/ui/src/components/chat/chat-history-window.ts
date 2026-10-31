/**
 * チャットの「会話の履歴」に出す期間の文言（3 製品の 4 つのチャットで同じ。#2340）。
 *
 * 会話の一覧の API は、最後の更新が直近 N 日の会話だけを返し、応答に期間の日数（`history_days`。0 は
 * 絞っていない）を付ける（共通の設定 `PLATFORM_CHAT_HISTORY_DAYS`）。画面は見出しの横に「最近 N 日」を出し、
 * 0 件のときは期間を含めた文にする。期間の外の会話は消えておらず、実行の詳細の「会話を開く」・直接の
 * リンクからは開ける。
 */

export interface ChatHistoryWindowLabels {
  /** 見出しの横の補足（例:「最近 90 日」）。 */
  caption: string;
  /** 期間の中に会話が無いときの文（例:「最近 90 日の会話はありません」）。 */
  empty: string;
}

/** 期間の日数から文言を作る。日数が分からない（読み込み前）・0（絞っていない）なら null。 */
export function chatHistoryWindowLabels(days: number | null | undefined): ChatHistoryWindowLabels | null {
  if (days == null || !Number.isFinite(days) || days <= 0) return null;
  const value = Math.floor(days);
  return {
    caption: `最近 ${value} 日`,
    empty: `最近 ${value} 日の会話はありません`,
  };
}

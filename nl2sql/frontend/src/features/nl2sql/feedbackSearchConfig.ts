import type { FeedbackSearchConfigData } from "./types";

/** 類似検索インデックスの「最低スコア」の既定（設定を保存していないときの値。backend と同じ 0.9。#1721）。 */
export const DEFAULT_FEEDBACK_SIMILARITY_THRESHOLD = 0.9;
/** 類似検索インデックスの「最大候補数」の既定（backend と同じ 3）。 */
export const DEFAULT_FEEDBACK_MATCH_LIMIT = 3;

/** 設定の読み込み前は既定の値を返し、読み込んだ後は保存済みの値をそのまま返す。 */
export function feedbackSearchConfigOrDefault(
  config: FeedbackSearchConfigData | null | undefined
): FeedbackSearchConfigData {
  return {
    similarity_threshold: config?.similarity_threshold ?? DEFAULT_FEEDBACK_SIMILARITY_THRESHOLD,
    match_limit: config?.match_limit ?? DEFAULT_FEEDBACK_MATCH_LIMIT,
  };
}

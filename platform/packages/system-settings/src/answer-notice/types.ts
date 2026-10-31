/** 回答の注意書きの API の型（backend の pr_system_settings.answer_notice と同じ形。#2405）。 */
export interface AnswerNoticeSettingsData {
  enabled: boolean;
  /** 管理者が書いた本文（未設定・既定に戻したときは null）。 */
  text: string | null;
  /** 既定の本文。 */
  default_text: string;
  /** 管理者が書いた窓口（連絡先の名前・連絡の方法。未設定・既定に戻したときは null。#2400）。 */
  contact?: string | null;
  /** 既定の窓口。 */
  default_contact?: string;
  /** 本文の後に続ける窓口の案内の 1 行（`{contact}` に窓口が入る）。 */
  contact_template?: string;
  /** 回答に付ける文（本文と窓口の案内）。無効のときも、有効にしたときの文。 */
  effective_text: string;
  max_chars: number;
  contact_max_chars?: number;
  updated_at?: string | null;
}

export interface AnswerNoticeSettingsUpdate {
  enabled: boolean;
  /** 本文。null・空・既定の本文なら既定に戻す。 */
  text: string | null;
  /** 窓口。null・空・既定の窓口なら既定に戻す（#2400）。 */
  contact?: string | null;
}

/** 製品側の API 関数（fetch wrapper・認証・エラー形式は製品ごとに異なるため注入する）。 */
export interface AnswerNoticeApi {
  get: (options?: { signal?: AbortSignal }) => Promise<AnswerNoticeSettingsData>;
  update: (payload: AnswerNoticeSettingsUpdate) => Promise<AnswerNoticeSettingsData>;
}

/** React Query key（製品の画面はその製品の設定だけを読む）。 */
export const ANSWER_NOTICE_QUERY_KEY = ["settings", "answer-notice"] as const;

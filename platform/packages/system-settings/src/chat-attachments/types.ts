/** チャットの添付の設定の API の型（backend の pr_system_settings.chat_attachments.settings と同じ形。#2517）。 */
export interface ChatAttachmentSettingsData {
  /** この製品のチャットで添付を使えるか（既定は無効）。 */
  enabled: boolean;
  updated_at?: string | null;
  /** 共通の `.env` の上限（この画面では変えない）。 */
  max_bytes: number;
  max_files: number;
  ttl_hours: number;
}

export interface ChatAttachmentSettingsUpdate {
  enabled: boolean;
}

/** 製品側の API 関数（fetch wrapper・認証・エラー形式は製品ごとに異なるため注入する）。 */
export interface ChatAttachmentSettingsApi {
  get: (options?: { signal?: AbortSignal }) => Promise<ChatAttachmentSettingsData>;
  update: (payload: ChatAttachmentSettingsUpdate) => Promise<ChatAttachmentSettingsData>;
}

/** React Query key（製品の画面はその製品の設定だけを読む）。チャットの画面も同じ key を捨てる。 */
export const CHAT_ATTACHMENT_SETTINGS_QUERY_KEY = ["settings", "chat-attachments"] as const;

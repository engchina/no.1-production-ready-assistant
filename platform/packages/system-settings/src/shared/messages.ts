/**
 * 設定画面の共通部品（テストの結果パネル）の既定の文言。
 * key は NL2SQL の i18n key と同じにしている（#100）。
 */
export const SHARED_MESSAGES = {
  "settings.testResult.checkedAt": "確認時刻",
  "settings.testResult.details": "詳細",
  "settings.testResult.elapsed": "所要時間",
  "settings.testResult.errorType": "エラー種別",
  "settings.testResult.errorDetail": "エラーの詳細",
  "settings.testResult.troubleshooting": "確認ポイント",
} as const;

export type SharedMessageKey = keyof typeof SHARED_MESSAGES;

/** 文言を取り出し、`{name}` を params で置き換える（NL2SQL の t() と同じ動き）。 */
export function t(key: SharedMessageKey, params?: Record<string, string | number>): string {
  let value: string = SHARED_MESSAGES[key] ?? key;
  if (params) {
    for (const [name, replacement] of Object.entries(params)) {
      value = value.replace(new RegExp(`\\{${name}\\}`, "g"), String(replacement));
    }
  }
  return value;
}

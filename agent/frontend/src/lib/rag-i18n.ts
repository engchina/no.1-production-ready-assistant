/**
 * 日本語文言。文言はハードコードせずここを経由する。
 */
export const ja = {
  // 共通メッセージ機構・ボタン（platform/docs/ux-contracts/messaging.md / buttons.md）
  "common.confirm": "実行",

  // ページタイトルは AGENTS.md 準拠の正式名を維持し、サイドバー表示のみ短縮形(*.sidebar)。
  "nav.settingsOci": "OCI 認証設定",
  "nav.settingsOci.sidebar": "OCI 認証",
  "nav.settingsUploadStorage": "アップロード保存先",
  "nav.settingsModel": "モデル設定",
  "nav.settingsModel.sidebar": "モデル",
  "nav.settingsDatabase": "データベース設定",
  "nav.settingsDatabase.sidebar": "データベース",
  "nav.section.toggle.collapse": "{section} を折りたたむ",
  "nav.section.toggle.expand": "{section} を展開",

  "settings.oci.subtitle": "OCI API キー認証情報を設定します。",
  "settings.uploadStorage.subtitle":
    "ドキュメントアップロード時の原本保存先を local または OCI Object Storage から選択します。",
  "settings.model.subtitle":
    "OCI Enterprise AI の LLM カタログと OCI Generative AI（埋め込み/リランク）のモデルを設定します。",
  "settings.database.subtitle": "Oracle AI Database への接続を設定します。",

  "pager.prev": "前へ",
  "pager.next": "次へ",
  "pager.range": "{start}–{end} / {total} 件",
} as const;

export type I18nKey = keyof typeof ja;

/** 文言を取得する。`{name}` 形式のプレースホルダを params で置換する。 */
export function t(key: I18nKey, params?: Record<string, string | number>): string {
  const template = ja[key];
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (_, name: string) =>
    name in params ? String(params[name]) : `{${name}}`
  );
}

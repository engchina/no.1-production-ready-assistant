/**
 * NL2SQL の日本語基底文言。文言はハードコードせずここを経由する。
 */
export const ja = {
  // 共通メッセージ機構・ボタン（platform/docs/ux-contracts/messaging.md / buttons.md）
  "common.confirm": "実行",
  "common.cancel": "キャンセル",
  "common.dismiss": "閉じる",
  "common.notifications": "通知",
  "common.action.refresh": "最新情報を取得",
  "common.action.schemaRefresh": "DB 構造を再取得",
  "common.schemaRefreshedAt": "DB 構造の最終取得: {date}",
  "common.action.refreshed": "最新の状態に更新しました。",
  "common.action.schemaRefreshed": "DB 構造を再取得しました。",
  "common.action.downloaded": "ファイルをダウンロードしました。",
  "common.action.downloadFailed": "ファイルをダウンロードできませんでした。ブラウザのダウンロード設定を確認して再試行してください。",
  // DB ゲート（3製品共通の部品。#325）。既定の文言は platform が持ち、製品名の入る文言だけを上書きする（#820）。
  "dbGate.notConfigured.message":
    "NL2SQL の各機能（SQL 生成・データ準備・改善・運用）を利用するには、まずデータベースの接続情報を設定してください。設定が完了すると、この画面は自動的に利用できるようになります。",
  "dbGate.setupRequired.settingsHint":
    "OCI 認証・アップロード保存先・モデル・データベース・システムテーブル・外観と証明書の各設定ページは引き続き利用できます。",
  "dbGate.recovering": "保存済みの業務データを復元しています…",
  "dbGate.persistenceFailed.title": "保存済みの業務データを復元できません",
  "dbGate.persistenceFailed.message":
    "データベース接続は正常ですが、NL2SQL の保存領域を利用できません。再試行しても解消しない場合は、システムテーブルとバックエンドログを確認してください。",
  "persistence.memoryWarning.title": "非永続モードで実行中です",
  "persistence.memoryWarning.message":
    "業務プロファイル、タスク、履歴、フィードバック、評価はメモリだけに保存され、バックエンドの再起動で失われます。",
  "common.delete": "削除",
  "common.retry": "再試行",
  "common.skipToMain": "本文へスキップ",
  "settings.testResult.elapsed": "所要時間",
  "settings.testResult.checkedAt": "確認時刻",
  "settings.testResult.troubleshooting": "確認ポイント",
  "settings.testResult.errorType": "エラー種別",

  "settings.preview.env.title": ".env プレビュー",
  "settings.preview.json.title": "JSON プレビュー",
  "settings.preview.copy": "コピー",
  "settings.preview.actions": "{label} 操作",
  "settings.preview.env.copy": ".env をコピー",
  "settings.preview.json.copy": "JSON をコピー",
  "settings.preview.copy.failed": "コピーできませんでした",

  // ページタイトルは AGENTS.md 準拠の正式名を維持し、サイドバー表示のみ短縮形(*.sidebar)。
  "nav.settingsOci": "OCI 認証設定",
  "nav.settingsOci.sidebar": "OCI 認証",
  "nav.settingsUploadStorage": "アップロード保存先",
  "nav.settingsModel": "モデル設定",
  "nav.settingsModel.sidebar": "モデル",
  "nav.settingsDatabase": "データベース設定",
  "nav.settingsDatabase.sidebar": "データベース",
  "nav.settingsSystemTables": "システムテーブル管理",
  "nav.settingsSystemTables.sidebar": "システムテーブル",
  "command.count": "{count} 件",

  "settings.oci.subtitle": "OCI API キー認証情報を設定します。",
  "settings.uploadStorage.subtitle":
    "ドキュメントアップロード時の原本保存先を local または OCI Object Storage から選択します。",

  "settings.model.subtitle":
    "OCI Enterprise AI の LLM カタログと OCI Generative AI（埋め込み/リランク）のモデルを設定します。",

  "settings.database.subtitle": "Oracle AI Database への接続を設定します。",
  "settings.systemTables.subtitle":
    "NL2SQL 内部 schema、migration、保存領域の状態を確認・初期化します。",
  "settings.database.loading": "データベース設定を読み込んでいます。",
  "knowledgeBasePicker.selectAll": "すべて選択",
  "knowledgeBasePicker.clear": "選択をすべて解除",
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

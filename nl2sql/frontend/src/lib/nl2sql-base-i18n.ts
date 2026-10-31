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
    "OCI 認証・アップロード保存先・モデル・データベース・システムテーブルの各設定ページは引き続き利用できます。",
  "dbGate.recovering": "保存済みの業務データを復元しています…",
  "dbGate.persistenceFailed.title": "保存済みの業務データを復元できません",
  "dbGate.persistenceFailed.message":
    "データベース接続は正常ですが、NL2SQL の保存領域を利用できません。再試行しても解消しない場合は、システムテーブルとバックエンドログを確認してください。",
  "dbGate.check.invalid_configuration":
    "Oracle Deep Data Security は python-oracledb の Thin mode でだけ使えます。NL2SQL_ORACLE_DEEPSEC_ENABLED=true の場合は PLATFORM_ORACLE_DRIVER_MODE=thin にしてください。",
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
  "nav.settingsSelectAiCredential": "Select AI Credential 管理",
  "nav.settingsSelectAiCredential.sidebar": "Select AI Credential",
  "command.count": "{count} 件",

  "settings.oci.subtitle": "OCI API キー認証情報を設定します。",
  "settings.uploadStorage.subtitle":
    "ドキュメントアップロード時の原本保存先を local または OCI Object Storage から選択します。",

  "settings.model.subtitle":
    "OCI Enterprise AI の LLM カタログと OCI Generative AI（埋め込み/リランク）のモデルを設定します。",

  "settings.database.subtitle": "Oracle AI Database への接続を設定します。",
  "settings.systemTables.subtitle":
    "NL2SQL 内部 schema、migration、保存領域の状態を確認・初期化します。",
  "settings.selectAiCredential.subtitle":
    "Select AI（DBMS_CLOUD_AI）が OCI を呼び出すための Credential を、データベースに作成・更新します。",
  "settings.database.loading": "データベース設定を読み込んでいます。",
  "settings.database.selectAiCredential.title": "Select AI Credential",
  "settings.database.selectAiCredential.loading": "Select AI Credential の状態を読み込んでいます",
  "settings.database.selectAiCredential.description":
    "Oracle DBMS_CLOUD_AI が OCI を呼び出すための署名鍵 Credential を、現在のデータベースユーザーに作成します。秘密鍵はブラウザへ返しません。",
  "settings.database.selectAiCredential.status.created": "作成済み",
  "settings.database.selectAiCredential.status.notCreated": "未作成",
  "settings.database.selectAiCredential.field.name": "Credential 名",
  "settings.database.selectAiCredential.field.schema": "Oracle schema",
  "settings.database.selectAiCredential.field.region": "Select AI 既定リージョン",
  "settings.database.selectAiCredential.field.regionHelper":
    "新しい業務プロファイルの初期値として使用します。Credential 自体の作成パラメータには含まれません。",
  "settings.database.selectAiCredential.ociReady":
    "サーバーの OCI 認証材料を利用できます。秘密鍵は Oracle の bind variable だけで渡されます。",
  "settings.database.selectAiCredential.ociMissing":
    "OCI 認証材料を準備できません。不足または修正が必要な項目: {fields}。OCI 認証設定を確認して、修正後に状態を再取得してください。",
  "settings.database.selectAiCredential.missing.configFile": "OCI config",
  "settings.database.selectAiCredential.missing.user": "User OCID",
  "settings.database.selectAiCredential.missing.tenancy": "Tenancy OCID",
  "settings.database.selectAiCredential.missing.fingerprint": "Fingerprint",
  "settings.database.selectAiCredential.missing.keyFile": "秘密鍵ファイル",
  "settings.database.selectAiCredential.missing.keyPermissions": "秘密鍵のファイル権限 (0600)",
  "settings.database.selectAiCredential.missing.keyEncrypted": "非暗号化の秘密鍵",
  "settings.database.selectAiCredential.missing.keyInvalid": "有効な PEM 秘密鍵",
  "settings.database.selectAiCredential.missing.unknown": "OCI 認証設定",
  "settings.database.selectAiCredential.action.refresh": "最新情報を取得",
  "settings.database.selectAiCredential.action.create": "Credential を作成",
  "settings.database.selectAiCredential.progress.creating": "Select AI の Credential を作成しています",
  "settings.database.selectAiCredential.progress.recreating": "Select AI の Credential を再作成しています",
  "settings.database.selectAiCredential.action.recreate": "Credential を再作成",
  "settings.database.selectAiCredential.confirmation.createHelper":
    "作成するには確認語 {phrase} を入力してください。",
  "settings.database.selectAiCredential.confirmation.recreateHelper":
    "再作成するには確認語 {phrase} を入力してください。既存 Credential は置き換えられます。",
  "settings.database.selectAiCredential.success":
    "Select AI Credential を作成し、既定リージョンを保存しました。",
  "settings.database.selectAiCredential.error.load":
    "Select AI Credential の状態を取得できませんでした。データベース接続を確認して再試行してください。",
  "settings.database.selectAiCredential.error.change":
    "Select AI Credential を変更できませんでした。OCI 認証設定とデータベース権限を確認してください。",

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

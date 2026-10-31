/** アップロード保存先画面の既定の文言（NL2SQL の文言。#97）。製品側は `messages` prop で上書きできる。 */
export const UPLOAD_STORAGE_MESSAGES = {
  loading: "アップロード保存先設定を読み込んでいます。",
  loadError: "アップロード保存先設定の取得に失敗しました。",
  refetchError:
    "最新のアップロード保存先設定を取得できませんでした。表示中の値は前回取得した内容です。再試行してください。",
  saveError: "アップロード保存先設定の保存に失敗しました。",
  retry: "再試行",
  destinationTitle: "保存先",
  destinationDescription: "アップロードされた PDF、画像、テキスト原本をどこへ保管するかを設定します。",
  fieldBackend: "アップロード原本の保存先",
  backendLocal: "ローカルディレクトリ",
  backendLocalDescription: "バックエンドが稼働する環境の PLATFORM_LOCAL_STORAGE_DIR 配下へ保存します。",
  backendOci: "OCI Object Storage",
  backendOciDescription: "OCI 認証設定の namespace と指定 bucket へ保存します。",
  fieldLocalStorageDir: "ローカル保存ディレクトリ",
  fieldObjectStorageRegion: "Object Storage リージョン",
  fieldObjectStorageNamespace: "Object Storage ネームスペース",
  fieldObjectStorageBucket: "Object Storage バケット",
  helperLocalStorageDir: "バックエンドプロセスから作成・書き込みできるディレクトリを指定します。",
  helperObjectStorageRegion:
    "原本ファイルを保存する bucket のリージョン。OCI 認証設定の Object Storage リージョンと合わせます。",
  helperObjectStorageNamespace: "OCI 認証設定で保存済みの Object Storage namespace を使用します。",
  helperObjectStorageBucket:
    "Object Storage ネームスペースは OCI 認証設定の値を使用します。原本ファイルを保存する bucket 名を指定します。",
  regionPlaceholder: "選択してください",
  required: "必須",
  save: "保存",
  actionsLabel: "アップロード保存先の操作",
  saved: "保存しました",
  openOciSettings: "OCI 認証設定を開く",
  ociSettingsIncomplete: "OCI Object Storage を使うには、リージョンとネームスペースの設定が必要です。",
  validationLocalStorageDir: "ローカル保存ディレクトリを入力してください。",
  validationObjectStorageRegion: "OCI 認証設定で Object Storage リージョンを選択してください。",
  validationObjectStorageNamespace: "OCI 認証設定で Object Storage ネームスペースを設定してください。",
  validationObjectStorageBucket: "Object Storage バケットを入力してください。",
  validationObjectStorageName: "英数字、ハイフン、アンダースコア、ドットで入力してください。",
  // OCI Object Storage の bucket と権限の確認（#2101）
  test: "接続を確認",
  testRunning: "OCI Object Storage の bucket と権限を確かめています。",
  testError: "接続を確認できませんでした。",
  testHint:
    "保存の前にも同じ確認を行います。bucket が無い・権限が足りないときは保存しません。確認では、確認用の小さな object を書き込んで削除します。",
  testStagesLabel: "確認の段階",
  testStageConfig: "OCI 認証の設定",
  testStageBucket: "bucket の確認",
  testStageWrite: "書き込み",
  testStageRead: "読み取り",
  testStageDelete: "削除",
  testStageSuccess: "成功",
  testStageFailed: "失敗",
  testStageSkipped: "未実施",
  savedUnverified:
    "保存しましたが、OCI Object Storage を確かめられませんでした。時間をおいて「接続を確認」を実行してください。",
};

export type UploadStorageMessages = typeof UPLOAD_STORAGE_MESSAGES;

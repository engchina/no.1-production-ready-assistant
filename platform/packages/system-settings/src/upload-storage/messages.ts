/** アップロード保存先画面の既定の文言（NL2SQL の文言。#97）。製品側は `messages` prop で上書きできる。 */
export const UPLOAD_STORAGE_MESSAGES = {
  loading: "アップロード保存先設定を読み込んでいます。",
  loadError: "アップロード保存先設定の取得に失敗しました。",
  refetchError:
    "最新のアップロード保存先設定を取得できませんでした。表示中の値は前回取得した内容です。再試行してください。",
  saveError: "アップロード保存先設定の保存に失敗しました。",
  retry: "再試行",
  destinationTitle: "保存先",
  destinationDescription:
    "アップロードされた PDF、画像、テキスト原本を、バックエンドが稼働する環境のディレクトリ（PLATFORM_LOCAL_STORAGE_DIR）へ保管します。",
  fieldLocalStorageDir: "ローカル保存ディレクトリ",
  helperLocalStorageDir: "バックエンドプロセスから作成・書き込みできるディレクトリを指定します。",
  required: "必須",
  save: "保存",
  actionsLabel: "アップロード保存先の操作",
  saved: "保存しました",
  validationLocalStorageDir: "ローカル保存ディレクトリを入力してください。",
};

export type UploadStorageMessages = typeof UPLOAD_STORAGE_MESSAGES;

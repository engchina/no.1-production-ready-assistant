/** チャットの添付の設定の画面の既定の文言（#2517）。設定は製品ごと。 */
export const CHAT_ATTACHMENT_SETTINGS_MESSAGES = {
  loading: "チャットの添付の設定を読み込んでいます。",
  loadError: "チャットの添付の設定の取得に失敗しました。",
  refetchError:
    "最新のチャットの添付の設定を取得できませんでした。表示中の値は前回取得した内容です。再試行してください。",
  saveError: "チャットの添付の設定の保存に失敗しました。",
  retry: "再試行",
  settingsTitle: "チャットの添付",
  settingsDescription:
    "この製品のチャットで、利用者が手元のファイルを会話に添付して質問できるようにします。添付は会話の期間だけ保存し、期限を過ぎると自動で消します。",
  enabledLabel: "チャットでファイルを添付できるようにする",
  enabledHint:
    "無効にすると、入力欄に「ファイルを添付」を出さず、添付を受け付けません。会話に残っている添付も回答に使いません。",
  limitsTitle: "上限",
  limitsDescription: "上限はシステムの設定（platform/.env）で決まり、この画面では変えません。",
  maxBytes: "1 つのファイルの大きさ",
  maxFiles: "1 つの会話の件数",
  ttl: "保存する期間",
  ttlValue: "最後に使ってから {hours} 時間",
  maxFilesValue: "{count} 件",
  formats: "添付できる形式",
  formatsValue: "PDF・Word・PowerPoint・Excel・CSV・テキスト・画像（画像は画像を読み取れるモデルが必要）",
  save: "保存",
  saved: "保存しました。チャットの画面を開き直すと反映されます。",
  actionsLabel: "チャットの添付の設定の操作",
};

export type ChatAttachmentSettingsMessages = typeof CHAT_ATTACHMENT_SETTINGS_MESSAGES;

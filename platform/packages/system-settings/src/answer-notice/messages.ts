/**
 * 回答の注意書きの画面の既定の文言（#2405）。設定は製品ごと（#2449）で、どの回答に出すかの説明は
 * 製品側が `messages` prop（`settingsDescription` など）で上書きする。
 */
export const ANSWER_NOTICE_MESSAGES = {
  loading: "回答の注意書きを読み込んでいます。",
  loadError: "回答の注意書きの取得に失敗しました。",
  refetchError:
    "最新の回答の注意書きを取得できませんでした。表示中の値は前回取得した内容です。再試行してください。",
  saveError: "回答の注意書きの保存に失敗しました。",
  retry: "再試行",
  settingsTitle: "注意書き",
  settingsDescription:
    "この製品の回答の末尾に、毎回同じ文を出します。確認の質問・エラー・停止には出しません。",
  enabledLabel: "回答の末尾に注意書きを出す",
  enabledHint: "無効にすると、この製品の回答には注意書きを出しません。本文は残ります。",
  fieldText: "本文",
  helperText:
    "プレーンテキストで書きます。改行はそのまま出し、URL は自動でリンクにします。HTML・Markdown の書式は使えません。連絡先は下の「窓口」に書きます。",
  fieldContact: "窓口",
  helperContact:
    "本文の後に「{line}」と出します。窓口の名前と連絡の方法（電話・URL など）を 1 行で書きます。",
  validationContactTooLong: "窓口は {max} 字以内で入力してください。",
  usingDefault: "既定の文を使っています。",
  usingCustom: "管理者が変えた文を使っています。",
  resetToDefault: "既定の文に戻す",
  save: "保存",
  saved: "保存しました。次の回答から反映されます。",
  actionsLabel: "回答の注意書きの操作",
  validationRequired: "本文を入力してください。既定の文に戻すには「既定の文に戻す」を押します。",
  validationTooLong: "本文は {max} 字以内で入力してください。",
  previewTitle: "表示の確認",
  previewDescription: "回答の本文と根拠の後に、この見た目で出します。",
  previewAnswer: "（回答の本文と根拠）",
  previewDisabled: "注意書きは無効です。回答の末尾には何も出しません。",
  noticeLabel: "回答の注意書き",
};

export type AnswerNoticeMessages = typeof ANSWER_NOTICE_MESSAGES;

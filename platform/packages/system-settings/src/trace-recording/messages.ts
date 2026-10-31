/** 3製品で共通の運用設定の文言。 */
export const TRACE_RECORDING_SETTINGS_MESSAGES = {
  loading: "軌跡の記録設定を読み込んでいます。", loadError: "軌跡の記録設定を取得できませんでした。",
  refetchError: "最新の設定を取得できませんでした。表示中の値は前回取得した内容です。再試行してください。",
  saveError: "軌跡の記録設定を保存できませんでした。", retry: "再試行",
  settingsTitle: "軌跡の記録", settingsDescription: "Agent・RAG・NL2SQL に共通の設定です。通常は本文を記録せず、問題を調べるときに有効にします。",
  enabledLabel: "入力・結果の本文を軌跡に記録する",
  enabledHint: "既定は無効です。有効にすると記録の容量と書き込みが増えます。保存後まもなく、新しく始まる呼び出しに反映されます。実行中の呼び出しには開始時の設定を使います。",
  scopeTitle: "記録する情報", scopeDescription: "この設定は、軌跡に追加する本文だけを切り替えます。",
  bodyLabel: "有効なときに記録", bodyValue: "モデルの入力・出力・返却された思考・Schema、ツールの引数と結果、検索の本文、embedding のベクトル、rerank の内容、SQL と結果行、本文を含む診断の詳細。記録の上限と閲覧権限は引き続き適用します。",
  metadataLabel: "無効でも記録", metadataValue: "処理の親子関係、開始・終了時刻、所要時間、状態、モデル、token の使用量、内容を含まないエラーの種類。",
  historyHint: "通常の質問・回答の履歴や業務の結果は変わりません。既存の軌跡の本文も削除しません。",
  save: "保存", saved: "保存しました。3製品の新しい呼び出しに反映されます。", actionsLabel: "軌跡の記録設定の操作",
};
export type TraceRecordingSettingsMessages = typeof TRACE_RECORDING_SETTINGS_MESSAGES;

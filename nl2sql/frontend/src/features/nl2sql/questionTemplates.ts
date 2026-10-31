// クエリ（自然言語）の穴埋めテンプレート。
// 値は空欄のまま挿入し、スキーマ参照パネルのカーソル挿入でテーブル名・列名を埋める想定。

// テンプレート解析用のスロットラベル(表示文字列ではなくプロトコル定数)。
// backend へ送る質問本文とバイト一致が必要なため i18n の対象外とし、
// テンプレ本文(QUESTION_TEMPLATES)と同じファイルで単一ソース管理する。
export const QUESTION_SLOT_LABELS = [
  "対象テーブル",
  "対象テーブル（複数可）",
  "テーブル間の関連",
  "抽出項目",
  "抽出条件",
  "条件",
  "WHERE条件",
  "WHERE 条件",
  "検索条件",
  "集計内容（件数・合計・平均など）",
  "集計単位（グループ化）",
  "並び替え（項目と昇順／降順）",
  "表示件数（上位N件）",
];

// スロットのうち「抽出条件」系(空欄ガード・解釈表示の filter 判定に使う)。
export const QUESTION_FILTER_LABELS = ["抽出条件", "条件", "WHERE条件", "WHERE 条件", "検索条件"];
export const QUESTION_TEMPLATES: Array<{ labelKey: string; body: string }> = [
  // 先頭は「自由入力」。body 空でクリック時にクエリを空へ戻す(テンプレ未適用の初期状態)。
  {
    labelKey: "nl2sql.question.template.default",
    body: "",
  },
  {
    labelKey: "nl2sql.question.template.basic",
    body: "対象テーブル：\n抽出項目：\n抽出条件：",
  },
  {
    labelKey: "nl2sql.question.template.aggregate",
    body: "対象テーブル：\n集計内容（件数・合計・平均など）：\n集計単位（グループ化）：\n抽出条件：",
  },
  {
    labelKey: "nl2sql.question.template.topn",
    body: "対象テーブル：\n抽出項目：\n並び替え（項目と昇順／降順）：\n表示件数（上位N件）：\n抽出条件：",
  },
  {
    labelKey: "nl2sql.question.template.join",
    body: "対象テーブル（複数可）：\nテーブル間の関連：\n抽出項目：\n抽出条件：",
  },
];

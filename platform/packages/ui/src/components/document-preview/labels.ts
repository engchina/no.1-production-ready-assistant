/**
 * 原文のプレビュー（ページの画像のビューア・テキスト・表示できない形式の案内）の文言（すべて翻訳済み。#2389）。
 * RAG の文書の詳細・根拠のパネルと、Agent の根拠のパネルで同じ書き方にするため、既定の {@link documentPreviewLabels}
 * を使う。製品だけの案内（Agent の根拠のパネルのダウンロードの案内など）は部品の `unsupportedMessage` で渡す。
 */
export interface DocumentPreviewLabels {
  toolbar: string;
  region: (fileName: string) => string;
  keyboardHint: string;
  previousPage: string;
  nextPage: string;
  pageStatus: (page: number, total: number) => string;
  pageStatusLabel: (page: number, total: number) => string;
  zoomIn: string;
  zoomOut: string;
  zoomStatus: (percent: string) => string;
  zoomStatusLabel: (percent: string) => string;
  fitWidth: string;
  fitPage: string;
  rotateLeft: string;
  rotateRight: string;
  focusHighlight: string;
  highlightStatus: (page: number, count: number) => string;
  highlightElsewhere: string;
  highlightPages: string;
  highlightPage: (page: number) => string;
  highlightPageLabel: (page: number) => string;
  /** ページの一覧を読み込んでいる間。 */
  loading: string;
  /** ページの画像を読み込んでいる間（#2474）。 */
  pageLoading: string;
  pageError: string;
  textLoading: string;
  /** テキストのプレビューで、引用した箇所を見つけて強調したときの案内（#2512）。 */
  textHighlightStatus: string;
  fetchError: string;
  retry: string;
  /** ページの画像を描けないとき（iframe・テキスト）の位置の案内。値は % の文字列。 */
  bboxFocus: (values: { page: number | string; x: string; y: string; width: string; height: string }) => string;
  bboxMapLabel: (page: number | string) => string;
  bboxPreviewLabel: (page: number | string) => string;
  unsupported: string;
  download: string;
}

/** 3 製品で同じ原文のプレビューの文言。 */
export function documentPreviewLabels(overrides: Partial<DocumentPreviewLabels> = {}): DocumentPreviewLabels {
  return {
    toolbar: "プレビューの操作",
    region: (fileName) => `${fileName} のプレビュー`,
    keyboardHint:
      "キーボード: + / - で拡大・縮小、0 で全体表示、W で横幅に合わせる、R で右に回転（Shift+R で左）、PageUp / PageDown でページ送り、Home / End で最初・最後のページ、H で強調した位置へ移動、矢印キーで移動します。Ctrl を押しながらホイールでも拡大・縮小できます。",
    previousPage: "前のページ",
    nextPage: "次のページ",
    pageStatus: (page, total) => `${page} / ${total}`,
    pageStatusLabel: (page, total) => `${total} ページ中 ${page} ページ目`,
    zoomIn: "拡大",
    zoomOut: "縮小",
    zoomStatus: (percent) => `${percent}%`,
    zoomStatusLabel: (percent) => `表示倍率 ${percent}%`,
    fitWidth: "横幅に合わせる",
    fitPage: "全体を表示",
    rotateLeft: "左に回転",
    rotateRight: "右に回転",
    focusHighlight: "強調した位置へ移動",
    highlightStatus: (page, count) => `p.${page} の ${count} か所を強調しています`,
    highlightElsewhere: "強調した位置は別のページにあります",
    highlightPages: "強調のあるページ",
    highlightPage: (page) => `p.${page}`,
    highlightPageLabel: (page) => `強調のある ${page} ページ目を表示`,
    loading: "プレビューを読み込んでいます",
    pageLoading: "ページを読み込んでいます",
    pageError: "このページの画像を表示できませんでした。",
    textLoading: "テキストを読み込んでいます",
    textHighlightStatus: "引用した箇所を強調しています",
    fetchError: "プレビューを取得できませんでした。",
    retry: "再試行",
    bboxFocus: ({ page, x, y, width, height }) => `位置: p.${page} / bbox x=${x}% y=${y}% w=${width}% h=${height}%`,
    bboxMapLabel: (page) => `p.${page} の bbox 座標マップ`,
    bboxPreviewLabel: (page) => `p.${page} の bbox プレビュー`,
    unsupported: "このファイル形式はプレビューに対応していません。",
    download: "ファイルをダウンロード",
    ...overrides,
  };
}

export const DEFAULT_DOCUMENT_PREVIEW_LABELS: DocumentPreviewLabels = documentPreviewLabels();

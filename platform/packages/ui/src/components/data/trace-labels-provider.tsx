import { createContext, useContext, type ReactNode } from "react";

import { DEFAULT_TRACE_DETAILS_LABELS, type TraceDetailsLabels } from "../../lib/trace-labels";

const TraceDetailsLabelsContext = createContext<TraceDetailsLabels>(DEFAULT_TRACE_DETAILS_LABELS);

/**
 * 「トレースの詳細」の文言と段の名前（#3219）。製品の root で 1 回だけ渡し、チャット・検索・実行の詳細など
 * すべての画面の `TraceDetails` / `RunProcessDetails` / `ChatProgress` が同じ文言を使う（画面ごとに渡さない）。
 * `labels` は描画のたびに作らず、module の定数を渡す。
 */
export function TraceDetailsLabelsProvider({ labels, children }: { labels: TraceDetailsLabels; children?: ReactNode }) {
  return <TraceDetailsLabelsContext.Provider value={labels}>{children}</TraceDetailsLabelsContext.Provider>;
}

export function useTraceDetailsLabels(): TraceDetailsLabels {
  return useContext(TraceDetailsLabelsContext);
}

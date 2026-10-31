import { useCallback } from "react";

import { useTraceProgressSnapshot, type TraceProgressSnapshot } from "@production-ready/ui";

import { apiGet } from "@/lib/api";
import { API_TIMEOUT_MS } from "@/lib/requestPolicy";
import { CHAT_PROGRESS_STEP_DEFINITIONS } from "./chatProgress";

/**
 * 記録（ジョブ・実行履歴の行・DB 管理チャットのターン）の処理の経過（#3226 の案 A）。
 *
 * span は共通の span の表にあり、記録ごとの API（`path`。記録の判定を通した後に、その記録の trace id で読む）が
 * snapshot（`{ events, dropped_spans }`）を返す。`targetId` はイベントの `target_id`（記録の ID）。`path` が null
 * なら読まない。実行中の読み直し・1 回の読み出しの上限・終わった直後の書き終わりを待つ読み直し（書き手は span を
 * 後からまとめて書く）・一部だけの記録（`partial`）は共通の `useTraceProgressSnapshot` が持つ。
 */
export function useNl2SqlTraceProgress({
  targetId,
  path,
  active,
}: {
  targetId: string;
  path: string | null;
  active: boolean;
}): TraceProgressSnapshot {
  const fetchSnapshot = useCallback(
    (signal: AbortSignal) => (path ? apiGet<unknown>(path, { signal, timeoutMs: API_TIMEOUT_MS.interactiveDetail }) : Promise.resolve([])),
    [path],
  );
  return useTraceProgressSnapshot({
    key: path ? targetId : null,
    active,
    fetchSnapshot,
    definitions: CHAT_PROGRESS_STEP_DEFINITIONS,
    timeoutMs: API_TIMEOUT_MS.interactiveDetail,
  });
}

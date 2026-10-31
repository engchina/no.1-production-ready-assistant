/**
 * 処理の経過を共通の span の表の snapshot から読む（#3226 の案 A）。3 製品共通の `useTraceProgressSnapshot` に RAG の工程の
 * 定義を渡す（実行中の読み直し・1 回の読み出しの上限・終わった直後の書き終わりを待つ読み直し・書けなかった span の数
 * `dropped_spans` からの `partial` は共通の hook が持つ）。
 *
 * snapshot の API は記録ごと（チャットの回答・実行履歴・RAG 検索の実行）にあり、画面は記録の判定を通した API だけを
 * 読む（任意の trace id を渡さない）。
 */
import { useTraceProgressSnapshot, type TraceProgressSnapshot } from "@production-ready/ui";

import type { TraceSnapshotData } from "./api";
import { CHAT_PROGRESS_DEFINITIONS } from "./chat-progress";

export function useRagTraceProgress({
  key,
  active,
  fetchSnapshot,
  intervalMs,
}: {
  /** 対象（回答のメッセージ・実行の id。snapshot のイベントの `target_id` と同じ値）。null なら読まない。 */
  key: string | null;
  active: boolean;
  fetchSnapshot: (signal: AbortSignal) => Promise<TraceSnapshotData>;
  intervalMs?: number;
}): TraceProgressSnapshot {
  return useTraceProgressSnapshot({ key, active, definitions: CHAT_PROGRESS_DEFINITIONS, intervalMs, fetchSnapshot });
}

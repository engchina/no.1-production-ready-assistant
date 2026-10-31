import { useCallback, useMemo } from "react";
import { useTraceProgressSnapshot, type TraceProgressSnapshot } from "@production-ready/ui";

import { agentApi, type RunState } from "@/lib/api";
import { agentProgressDefinitions, runProgressIsLegacy } from "@/lib/run-history";

/**
 * Run の処理の経過（共通の span の表の snapshot。#3226 の案 A）を読む hook。チャットと実行の詳細で同じ記録・名前にする。
 *
 * - `GET /api/runs/{id}/trace` を共通の `useTraceProgressSnapshot` で読む（実行中の読み直し・1 回の読み出しの上限・終わった
 *   直後の書き終わりを待つ読み直し・書けなかった span の数は共通の hook が持つ）。最終の状態は Run の状態（正本）で、backend
 *   が表で実行中のままの span を Run の終端に合わせて閉じる。失敗したら前の内容を残し、`failed` の間は画面が「接続を確認して
 *   います」を出す（#1160）。
 * - 古い形式の記録（`runProgressIsLegacy`。span の表より前の Run）は読まない。表に書けなかった span があれば `partial`
 *   （Run に保存した数も見る。実行と再開が別の process のとき）。
 */
export function useRunTraceProgress(
  run: Pick<RunState, "id" | "steps" | "trace_store" | "trace_id" | "trace_dropped_spans" | "status">,
  { active }: { active: boolean }
): TraceProgressSnapshot {
  const legacy = runProgressIsLegacy(run);
  const runId = run.id;
  const fetchSnapshot = useCallback((signal: AbortSignal) => agentApi.getRunTrace(runId, { signal }), [runId]);
  // ツールの span の詳細に、Run の step のデータの範囲の記録・取り直した結果を足す（チャットと実行の詳細で同じ。#2294）。
  const definitions = useMemo(() => agentProgressDefinitions(run.steps), [run.steps]);
  const snapshot = useTraceProgressSnapshot({ key: legacy ? null : runId, active: !legacy && active, fetchSnapshot, definitions });
  const droppedSpans = Math.max(snapshot.droppedSpans, run.trace_dropped_spans ?? 0);
  return { ...snapshot, legacy, droppedSpans, partial: !legacy && droppedSpans > 0 };
}

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useTraceDetailsLabels } from "../data/trace-labels-provider";
import type { ChatProgressStep } from "./chat-progress";
import {
  chatProgressHasSpans,
  chatProgressTerminalOf,
  chatProgressTraceIdOf,
  labelChatProgressSteps,
  parseChatProgressEvents,
  reduceChatProgressEvents,
  type ChatProgressEvent,
  type ChatProgressStepDefinitions,
  type ChatProgressTerminalStatus,
} from "./chat-progress-events";

/**
 * 処理の経過（trace の投影。#3226 の案 A）を、共通の span の表の snapshot から読む hook（3 製品共通）。
 *
 * span の表は span ごとの最新の状態を持ち、全体の番号（`seq`）を持たないので、配信は snapshot の読み直しにする。
 * 実行中（`active`）は `intervalMs` ごとに読み直す。製品は自分の記録の判定を通した API（例
 * `GET /api/runs/{run_id}/trace`）を `fetchSnapshot` で渡す（`{ events, dropped_spans }` か、イベントの一覧）。
 * 一覧の保持（処理中は出した段を消さない・状態を戻さない）は `ChatProgress` が持つ。
 *
 * - 1 回の読み出しは `timeoutMs` で打ち切る（応答しない読み出しで読み直しを止めない）。失敗したら前の内容を残し、
 *   `failed` の間は画面が「接続を確認しています」を出す（#1160）。
 * - span は書き手が間隔をおいてまとめて書くので、終わった直後の読み出しには最後の span（すぐ終わった実行では
 *   すべての span）がまだ無いことがある。実行中から終わったとき、または終わった記録の snapshot に終端・span が
 *   無い・読み出しが失敗したときは、`settleDelayMs` ごとに `settleAttempts` 回まで読み直す（`settling`）。
 *   読み直しの間は古い形式（`legacy`）と出さない（本当に古い形式の記録は、既定でおよそ 3 秒遅れて出る）。
 * - `dropped_spans` が 1 以上なら `partial`（「一部の処理だけを記録しています。」。`ChatProgress` /
 *   `RunTimelineSection` の `partial` に渡す）。一部だけの記録は古い形式と出さない。
 */
export interface TraceProgressSnapshotOptions {
  /** 対象（回答・ジョブ・Run。イベントの `target_id` と同じ値）。変わったら読み直す。null なら読まない。 */
  key: string | null;
  /** 実行中か（読み直しを続ける）。 */
  active: boolean;
  /** snapshot を読む（`{ events, dropped_spans }` かイベントの一覧。未検証の入力として検証する）。 */
  fetchSnapshot: (signal: AbortSignal) => Promise<unknown>;
  definitions: ChatProgressStepDefinitions;
  intervalMs?: number;
  /** 1 回の読み出しの上限（ミリ秒）。 */
  timeoutMs?: number;
  /** 終わった記録を読み直す間隔（ミリ秒）と回数。 */
  settleDelayMs?: number;
  settleAttempts?: number;
}

export interface TraceProgressSnapshot {
  events: ChatProgressEvent[];
  steps: ChatProgressStep[];
  traceId: string | undefined;
  terminal: ChatProgressTerminalStatus | null;
  /** 読めた記録に span が無い（古い形式・記録なし）。読み直しの間と一部だけの記録では false。 */
  legacy: boolean;
  /** 最後の読み出しが失敗した（前の内容は残す）。 */
  failed: boolean;
  loaded: boolean;
  /** 表に書けなかった span の数。 */
  droppedSpans: number;
  /** 表に書けなかった span がある（一部だけの記録）。 */
  partial: boolean;
  /** 終わった記録を、書き終わるのを待って読み直している。 */
  settling: boolean;
  refresh: () => void;
}

export const TRACE_SNAPSHOT_INTERVAL_MS = 1000;
export const TRACE_SNAPSHOT_TIMEOUT_MS = 8000;
/** span の書き手の既定の間隔（0.5 秒）より長く待つ。 */
export const TRACE_SNAPSHOT_SETTLE_DELAY_MS = 1500;
export const TRACE_SNAPSHOT_SETTLE_ATTEMPTS = 2;

interface SnapshotState {
  key: string | null;
  events: ChatProgressEvent[];
  droppedSpans: number;
  failed: boolean;
  loaded: boolean;
}

function parseSnapshot(raw: unknown): { events: ChatProgressEvent[]; droppedSpans: number } {
  if (Array.isArray(raw)) return { events: parseChatProgressEvents(raw), droppedSpans: 0 };
  const record = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const dropped = record.dropped_spans;
  return {
    events: parseChatProgressEvents(record.events),
    droppedSpans: typeof dropped === "number" && Number.isFinite(dropped) && dropped > 0 ? Math.floor(dropped) : 0,
  };
}

async function withTimeout<T>(signal: AbortSignal, timeoutMs: number, run: (signal: AbortSignal) => Promise<T>): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  const timer = setTimeout(abort, timeoutMs);
  if (signal.aborted) abort();
  else signal.addEventListener("abort", abort, { once: true });
  try {
    return await run(controller.signal);
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", abort);
  }
}

export function useTraceProgressSnapshot({
  key,
  active,
  fetchSnapshot,
  definitions,
  intervalMs = TRACE_SNAPSHOT_INTERVAL_MS,
  timeoutMs = TRACE_SNAPSHOT_TIMEOUT_MS,
  settleDelayMs = TRACE_SNAPSHOT_SETTLE_DELAY_MS,
  settleAttempts = TRACE_SNAPSHOT_SETTLE_ATTEMPTS,
}: TraceProgressSnapshotOptions): TraceProgressSnapshot {
  const traceLabels = useTraceDetailsLabels();
  const [state, setState] = useState<SnapshotState>({ key, events: [], droppedSpans: 0, failed: false, loaded: false });
  const fetchRef = useRef(fetchSnapshot);
  useEffect(() => {
    fetchRef.current = fetchSnapshot;
  });
  const [generation, setGeneration] = useState(0);
  const refresh = useCallback(() => setGeneration((value) => value + 1), []);

  useEffect(() => {
    if (key === null) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const raw = await withTimeout(controller.signal, timeoutMs, (signal) => fetchRef.current(signal));
        if (controller.signal.aborted) return;
        setState({ key, ...parseSnapshot(raw), failed: false, loaded: true });
      } catch {
        if (controller.signal.aborted) return;
        setState((before) =>
          before.key === key ? { ...before, failed: true } : { key, events: [], droppedSpans: 0, failed: true, loaded: false },
        );
      }
      if (active && !controller.signal.aborted) timer = setTimeout(() => void load(), intervalMs);
    };
    void load();
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [key, active, intervalMs, timeoutMs, generation]);

  const current = state.key === key ? state : null;
  const events = useMemo(() => current?.events ?? [], [current]);
  const steps = useMemo(() => {
    const reduced = reduceChatProgressEvents(null, { key, events, since: 0 });
    return labelChatProgressSteps(reduced.steps, definitions, { traceLabels });
  }, [events, key, definitions, traceLabels]);
  const loaded = Boolean(current?.loaded);
  const failed = Boolean(current?.failed);
  const terminal = chatProgressTerminalOf(events, key);
  const hasSpans = chatProgressHasSpans(events);

  // 終わった記録の読み直し（対象ごとに数える）。前の描画の結果から決める状態なので描画の中で合わせる。
  const [settle, setSettle] = useState<{ key: string | null; attempts: number; justFinished: boolean }>({
    key,
    attempts: 0,
    justFinished: false,
  });
  const [previous, setPrevious] = useState({ key, active });
  if (previous.key !== key || previous.active !== active) {
    const finished = previous.key === key && previous.active && !active;
    setPrevious({ key, active });
    setSettle({ key, attempts: 0, justFinished: finished });
  }
  const attempts = settle.key === key ? settle.attempts : 0;
  const justFinished = settle.key === key && settle.justFinished;
  const settling =
    key !== null &&
    !active &&
    (loaded || failed) &&
    attempts < settleAttempts &&
    (justFinished || failed || terminal === null || !hasSpans);
  useEffect(() => {
    if (!settling) return;
    const timer = setTimeout(() => {
      setSettle((before) => ({ key, attempts: (before.key === key ? before.attempts : 0) + 1, justFinished: false }));
      refresh();
    }, settleDelayMs);
    return () => clearTimeout(timer);
  }, [settling, attempts, key, settleDelayMs, refresh]);

  const droppedSpans = current?.droppedSpans ?? 0;
  const partial = droppedSpans > 0;
  return {
    events,
    steps,
    traceId: chatProgressTraceIdOf(events),
    terminal,
    legacy: loaded && !settling && !partial && events.length > 0 && !hasSpans,
    failed,
    loaded,
    droppedSpans,
    partial,
    settling,
    refresh,
  };
}

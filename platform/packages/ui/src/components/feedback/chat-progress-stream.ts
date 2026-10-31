import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { useTraceDetailsLabels } from "../data/trace-labels-provider";
import {
  CHAT_PROGRESS_SSE_EVENT,
  CHAT_PROGRESS_SSE_HEARTBEAT_EVENT,
  labelChatProgressSteps,
  parseChatProgressEvent,
  reduceChatProgressEvents,
  type ChatProgressEvent,
  type ChatProgressEventsState,
  type ChatProgressPage,
  type ChatProgressStepDefinitions,
  type ChatProgressTerminalStatus,
} from "./chat-progress-events";
import { useChatProgressTracker, type ChatProgressTrackerProps } from "./chat-progress-tracker";
import type { ChatProgressStep } from "./chat-progress";

/** polling に縮退したときの既定の間隔。 */
export const DEFAULT_CHAT_PROGRESS_POLL_INTERVAL_MS = 2_000;
/** SSE の接続が続けて失敗したら polling に縮退する回数。 */
export const CHAT_PROGRESS_SSE_MAX_ERRORS = 3;

/** 配信の方式。`push` は製品が受け取ったイベント（`events`）だけを使う。 */
export type ChatProgressTransport = "sse" | "polling" | "push" | "idle";

/**
 * チャットの処理の段階の配信を受け取る hook の入力（3 製品共通。#1359）。
 *
 * 配信は 3 つの口のどれか（組み合わせてよい）:
 * - `streamUrl`: SSE（`EventSource`）。hook が `since=<seq>` を付けて開き、切れたらブラウザが `Last-Event-ID` で
 *   続きから張り直す。続けて失敗する・使えないときは `fetchEvents` の polling に縮退する。
 * - `fetchEvents`: polling と取り直し（`since` より後のイベント）。SSE が無いときの配信、途絶え・番号の飛びの
 *   取り直しに使う。
 * - `events`: 製品が受け取ったイベント（保存済みの記録・製品の自前の配信）。増えたら積む（重複は捨てる）。
 */
export interface ChatProgressStreamOptions {
  /** 対象（`target_id`。RAG の回答のメッセージ・NL2SQL のジョブ・Agent の Run）。null は追わない。 */
  key: string | null;
  /** 製品の段階の定義（`kind` → i18n の名前）。 */
  definitions: ChatProgressStepDefinitions;
  /** 製品が受け取ったイベント。 */
  events?: readonly ChatProgressEvent[];
  /** SSE の URL。null・省略は SSE を使わない。 */
  streamUrl?: string | null;
  /** `since` より後のイベントを取る（polling・取り直し）。対象が無ければ null。 */
  fetchEvents?: (since: number, signal: AbortSignal) => Promise<ChatProgressPage | null>;
  /**
   * SSE が無いとき `fetchEvents` で polling するか。既定 true。
   * false は製品の自前の配信（RAG の回答の SSE）が段階を運ぶとき: `fetchEvents` は途絶えの取り直しと番号の飛びの
   * 穴埋めにだけ使い、定期の取得はしない。取り直しの成功は配信の再開として数えない（途絶えは製品の配信の
   * `touch()` だけで判定し、`onStalled` で製品の配信を張り直させる）。
   */
  pollEvents?: boolean;
  /**
   * 処理中か（製品が対象の状態で知っているとき）。終端のイベントが届いたら、この値に関係なく終わり。
   * 省略時は、終端のイベントが届くまで処理中。
   */
  active?: boolean;
  /** false の間は配信を受け取らない（keep-alive で画面が隠れている・承認を待っているなど）。既定 true。 */
  enabled?: boolean;
  /** 全体の所要時間（完了後の 1 行）。 */
  elapsedMs?: number | null;
  /** polling の間隔。既定 2 秒。 */
  pollIntervalMs?: number;
  /** 更新が途絶えたとみなす時間（`useChatProgressTracker`）。 */
  staleAfterMs?: number;
  refreshTimeoutMs?: number;
  maxBackoffMs?: number;
  /** 更新が途絶えたときに、製品の自前の配信を張り直す（RAG の回答の配信）。 */
  onStalled?: () => void;
  /** 終端のイベントが届いた（対象ごとに 1 回）。結果の取り直しなどに使う。 */
  onTerminal?: (status: ChatProgressTerminalStatus) => void;
  /** テスト用: `EventSource` を作る。 */
  createEventSource?: (url: string) => EventSource;
}

export interface ChatProgressStream {
  /** 名前を付けた段階の一覧。 */
  steps: ChatProgressStep[];
  /** 処理中か（終端でない）。 */
  active: boolean;
  /** 今の試行の終端（届いていなければ null）。 */
  terminal: ChatProgressTerminalStatus | null;
  /** 続けて受け取った最後の番号。 */
  cursor: number;
  transport: ChatProgressTransport;
  /** 更新が途絶え、取り直している（「接続を確認しています」）。 */
  reconnecting: boolean;
  /** 製品の自前の配信（heartbeat を含む）を受け取ったときに呼ぶ。 */
  touch: () => void;
  /** 待たずに取り直す。 */
  refreshNow: () => void;
  /** `<ChatProgress {...stream.progressProps} />` で渡す。 */
  progressProps: ChatProgressTrackerProps;
}

function defaultEventSource(url: string): EventSource {
  return new EventSource(url);
}

function withSince(url: string, since: number): string {
  return `${url}${url.includes("?") ? "&" : "?"}since=${since}`;
}

function documentHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

/**
 * チャットの処理の段階の配信を受け取り、段階の一覧にまとめる（3 製品共通。#1359）。
 *
 * - イベントは `reduceChatProgressEvents` で積む（`seq` で古い・重複を捨て、番号が飛んだら取り直しで埋める。
 *   状態は戻さない。試行・対象の切り替えで作り直す）。
 * - 配信は SSE を優先し、使えない・続けて失敗したら polling に縮退する。終端のイベントで閉じる。
 * - 途絶え（SSE の heartbeat・polling の応答が `staleAfterMs` 届かない）は `useChatProgressTracker` で扱い、
 *   取り直し（`fetchEvents`・SSE の張り直し・`onStalled`）と「接続を確認しています」を出す。
 */
export function useChatProgressStream({
  key,
  definitions,
  events,
  streamUrl,
  fetchEvents,
  pollEvents = true,
  active: activeProp,
  enabled = true,
  elapsedMs,
  pollIntervalMs = DEFAULT_CHAT_PROGRESS_POLL_INTERVAL_MS,
  staleAfterMs,
  refreshTimeoutMs,
  maxBackoffMs,
  onStalled,
  onTerminal,
  createEventSource = defaultEventSource,
}: ChatProgressStreamOptions): ChatProgressStream {
  const [state, setState] = useState<ChatProgressEventsState>(() =>
    reduceChatProgressEvents(null, { key, events: events ?? [] })
  );
  // 対象が変わった描画では、前の対象の状態を出さない（effect で作り直す前の 1 回）。
  const current = state.key === key ? state : reduceChatProgressEvents(null, { key, events: events ?? [] });

  const keyRef = useRef(key);
  const cursorRef = useRef(current.lastSeq);
  useLayoutEffect(() => {
    keyRef.current = key;
    cursorRef.current = current.lastSeq;
  });
  const dispatch = useCallback((incoming: readonly ChatProgressEvent[], since?: number) => {
    setState((previous) => reduceChatProgressEvents(previous, { key: keyRef.current, events: incoming, since }));
  }, []);

  // 製品が受け取ったイベント（と対象の切り替え）を積む。新しい番号が届いたら、配信を受け取ったとして数える。
  const touchRef = useRef<() => void>(() => undefined);
  useLayoutEffect(() => {
    setState((previous) => reduceChatProgressEvents(previous, { key, events: events ?? [] }));
    if (events?.some((event) => event.target_id === key && event.seq > cursorRef.current)) touchRef.current();
  }, [key, events]);

  const terminal = current.terminal;
  const active = key !== null && terminal === null && (activeProp ?? true);
  const following = enabled && active && key !== null;

  const fetchRef = useRef(fetchEvents);
  const onStalledRef = useRef(onStalled);
  const onTerminalRef = useRef(onTerminal);
  const createRef = useRef(createEventSource);
  useLayoutEffect(() => {
    fetchRef.current = fetchEvents;
    onStalledRef.current = onStalled;
    onTerminalRef.current = onTerminal;
    createRef.current = createEventSource;
  });

  /** 取り直し（`since` = 今の番号）。応答は `since` の後をすべて含む。 */
  const pull = useCallback(
    async (signal: AbortSignal): Promise<boolean> => {
      const fetcher = fetchRef.current;
      const target = keyRef.current;
      if (!fetcher || target === null) return false;
      const since = cursorRef.current;
      const page = await fetcher(since, signal);
      if (signal.aborted || keyRef.current !== target || !page) return false;
      dispatch(page.events, since);
      return true;
    },
    [dispatch]
  );

  const [sseFailedKey, setSseFailedKey] = useState<string | null>(null);
  const [sseGeneration, setSseGeneration] = useState(0);
  const sseAvailable = Boolean(streamUrl) && (typeof EventSource !== "undefined" || createEventSource !== defaultEventSource);
  const useSse = following && sseAvailable && sseFailedKey !== key;
  const usePolling = following && !useSse && fetchEvents !== undefined && pollEvents;
  // 製品の自前の配信だけが段階を運ぶ（取り直しは穴埋めだけ）。
  const pushOnly = !useSse && !pollEvents;
  const transport: ChatProgressTransport = !following
    ? "idle"
    : useSse
      ? "sse"
      : usePolling
        ? "polling"
        : "push";

  // 途絶えの判定と取り直し（#1160 の hook を中で使う）。
  const useSseRef = useRef(useSse);
  const pushOnlyRef = useRef(pushOnly);
  useLayoutEffect(() => {
    useSseRef.current = useSse;
    pushOnlyRef.current = pushOnly;
  });
  const refresh = useCallback(
    async (signal: AbortSignal) => {
      onStalledRef.current?.();
      if (useSseRef.current) setSseGeneration((value) => value + 1);
      if ((await pull(signal)) && !pushOnlyRef.current) touchRef.current();
    },
    [pull]
  );
  // span の段の技術の名前と段の名前は、製品の root の文言（`TraceDetailsLabelsProvider`）で付ける（#3226）。
  const traceLabels = useTraceDetailsLabels();
  const steps = useMemo(
    () => labelChatProgressSteps(current.steps, definitions, { traceLabels }),
    [current.steps, definitions, traceLabels]
  );
  const canRefresh = fetchEvents !== undefined || Boolean(streamUrl) || onStalled !== undefined;
  const tracker = useChatProgressTracker({
    key,
    steps,
    active,
    elapsedMs,
    refresh: canRefresh ? refresh : undefined,
    staleAfterMs,
    refreshTimeoutMs,
    maxBackoffMs,
    enabled,
  });
  useLayoutEffect(() => {
    touchRef.current = tracker.touch;
  });

  // SSE。
  const sseUrl = useSse && streamUrl ? streamUrl : null;
  useEffect(() => {
    if (sseUrl === null || key === null) return undefined;
    const target = key;
    let errors = 0;
    const source = createRef.current(withSince(sseUrl, cursorRef.current));
    const fail = () => {
      source.close();
      if (keyRef.current === target) setSseFailedKey(target);
    };
    const onProgress = (message: MessageEvent) => {
      errors = 0;
      touchRef.current();
      let data: unknown;
      try {
        data = JSON.parse(String(message.data));
      } catch {
        return;
      }
      const event = parseChatProgressEvent(data);
      if (event) dispatch([event]);
    };
    const onHeartbeat = () => {
      errors = 0;
      touchRef.current();
    };
    const onOpen = () => touchRef.current();
    const onError = () => {
      errors += 1;
      // 応答が SSE でない（4xx / 5xx・204）と閉じる。続けて失敗したら polling に縮退する。
      if (source.readyState === 2 || errors >= CHAT_PROGRESS_SSE_MAX_ERRORS) fail();
    };
    source.addEventListener(CHAT_PROGRESS_SSE_EVENT, onProgress as EventListener);
    source.addEventListener(CHAT_PROGRESS_SSE_HEARTBEAT_EVENT, onHeartbeat);
    source.addEventListener("open", onOpen);
    source.addEventListener("error", onError);
    return () => {
      source.removeEventListener(CHAT_PROGRESS_SSE_EVENT, onProgress as EventListener);
      source.removeEventListener(CHAT_PROGRESS_SSE_HEARTBEAT_EVENT, onHeartbeat);
      source.removeEventListener("open", onOpen);
      source.removeEventListener("error", onError);
      source.close();
    };
  }, [sseUrl, key, sseGeneration, dispatch]);

  // polling。
  useEffect(() => {
    if (!usePolling || key === null) return undefined;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | null = null;
    const tick = async () => {
      if (disposed) return;
      if (!documentHidden()) {
        controller = new AbortController();
        try {
          if (await pull(controller.signal)) touchRef.current();
        } catch {
          // 失敗は次の回で取り直す（途絶えが続けば「接続を確認しています」）。
        }
      }
      if (!disposed) timer = setTimeout(() => void tick(), pollIntervalMs);
    };
    void tick();
    return () => {
      disposed = true;
      if (timer !== undefined) clearTimeout(timer);
      controller?.abort();
    };
  }, [usePolling, key, pollIntervalMs, pull]);

  // 番号が飛んだら（前の番号のイベントが届かない）、待たずに取り直して埋める。
  const gapAt = current.pending.length > 0 ? current.lastSeq : null;
  useEffect(() => {
    if (gapAt === null || !fetchRef.current) return undefined;
    const controller = new AbortController();
    pull(controller.signal).catch(() => undefined);
    return () => controller.abort();
  }, [gapAt, pull]);

  // 終端（対象ごとに 1 回）。
  const notified = useRef<string | null>(null);
  useEffect(() => {
    if (terminal === null || key === null) return;
    const mark = `${key}#${current.attempt}`;
    if (notified.current === mark) return;
    notified.current = mark;
    onTerminalRef.current?.(terminal);
  }, [terminal, key, current.attempt]);

  return {
    steps,
    active,
    terminal,
    cursor: current.lastSeq,
    transport,
    reconnecting: tracker.reconnecting,
    touch: tracker.touch,
    refreshNow: tracker.refreshNow,
    progressProps: {
      ...tracker.progressProps,
      // 試行が変わったら一覧を作り直す（`ChatProgress` の `progressKey`）。
      progressKey: key === null ? null : `${key}#${current.attempt}`,
    },
  };
}

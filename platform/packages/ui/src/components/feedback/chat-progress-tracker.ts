import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { isChatProgressActive, type ChatProgressStep } from "./chat-progress";

/** 更新が途絶えたとみなす既定の時間（#1160）。polling は数秒ごとに応答が届くので、その数回分。 */
export const DEFAULT_CHAT_PROGRESS_STALE_AFTER_MS = 15_000;
/** 1 回の取り直しの既定の上限。 */
export const DEFAULT_CHAT_PROGRESS_REFRESH_TIMEOUT_MS = 15_000;
/** 取り直しが続けて失敗したときの待ちの既定の上限（指数 backoff の上限）。 */
export const DEFAULT_CHAT_PROGRESS_MAX_BACKOFF_MS = 30_000;
const FIRST_BACKOFF_MS = 1_000;

/**
 * 続けて失敗した回数から、次の取り直しまでの待ちを決める（1 秒・2 秒・4 秒 … 上限まで）。
 * `failures` は 1 以上。
 */
export function chatProgressBackoffMs(failures: number, maxBackoffMs = DEFAULT_CHAT_PROGRESS_MAX_BACKOFF_MS): number {
  const exponent = Math.max(0, Math.min(failures - 1, 16));
  return Math.min(maxBackoffMs, FIRST_BACKOFF_MS * 2 ** exponent);
}

/**
 * チャットの回答の処理の状態を追う hook の入力（3 製品共通。#1160）。
 *
 * 製品は自分の配信（NL2SQL: ジョブの polling、RAG: SSE、Agent: Run の polling）を、次の 2 つの口に合わせる。
 * - 段階の更新を受け取る口: `steps`（今の段階の一覧）と、配信を受け取った印（polling は `receivedAt`、
 *   SSE・WebSocket などの push は戻り値の `touch()`）。
 * - 状態を取り直す関数: `refresh`（polling の取り直し・保存済みの結果の取り直し）。
 */
export interface ChatProgressTrackerOptions {
  /** 追う対象（ジョブ ID・Run ID・送信の ID など）。null は追わない。変わったら途絶えの判定を初めから数える。 */
  key: string | null;
  /** 今の段階の一覧（3 製品共通の契約 `ChatProgressStep`）。 */
  steps: ChatProgressStep[];
  /**
   * 処理中か。省略時は段階から決める（`isChatProgressActive`）。ジョブ・Run の状態で終端が分かるときは渡す。
   * false（終端: 完了・失敗・停止）になったら取り直しをやめる。
   */
  active?: boolean;
  /** 全体の所要時間（完了後の 1 行）。`ChatProgress` へそのまま渡す。 */
  elapsedMs?: number | null;
  /**
   * 最後に配信を受け取った時刻（ms）。polling は TanStack Query の `dataUpdatedAt`（取得に成功するたびに進む）。
   * push の配信は代わりに戻り値の `touch()` を呼ぶ。
   */
  receivedAt?: number;
  /**
   * 状態を取り直す。更新が `staleAfterMs` の間途絶えたら呼ぶ。失敗・`refreshTimeoutMs` の超過は backoff して
   * 続け、`active` が false（終端）になるか、対象が変わるまで止めない。`signal` は上限の超過で中止される。
   * 取り直した結果は、製品の配信の口（`steps` / `receivedAt` / `touch()`）で渡す。
   */
  refresh?: (signal: AbortSignal) => unknown;
  /** 更新が途絶えたとみなす時間。既定 15 秒。 */
  staleAfterMs?: number;
  /** 1 回の取り直しの上限。既定 15 秒。 */
  refreshTimeoutMs?: number;
  /** 続けて失敗したときの待ちの上限。既定 30 秒。 */
  maxBackoffMs?: number;
  /** false の間は追わない（keep-alive で画面が隠れているなど）。既定 true。 */
  enabled?: boolean;
}

/** `ChatProgress` へそのまま渡す値。 */
export interface ChatProgressTrackerProps {
  steps: ChatProgressStep[];
  active: boolean;
  elapsedMs?: number | null;
  reconnecting: boolean;
  /** 追う対象（`key`）。`ChatProgress` は対象が変わったら段階の一覧を作り直す（処理中は消さない。#1358）。 */
  progressKey: string | null;
}

export interface ChatProgressTracker {
  /** 処理中か（終端でない）。 */
  active: boolean;
  /** 更新が途絶え、状態を取り直している（「接続を確認しています」を出す）。更新が届いたら false に戻る。 */
  reconnecting: boolean;
  /** push の配信（SSE のイベント・heartbeat、WebSocket のメッセージ）を受け取ったときに呼ぶ。 */
  touch: () => void;
  /** 待たずに取り直す（接続が切れた・配信が終わったのに終端が届かないと分かったとき）。 */
  refreshNow: () => void;
  /** `<ChatProgress {...tracker.progressProps} />` で渡す。 */
  progressProps: ChatProgressTrackerProps;
}

interface StallState {
  key: string;
  /** 取り直しを始めた時刻。これより後に配信を受け取ったら途絶えは終わり。 */
  at: number;
}

/** signal が中止されたら reject する（取り直しの上限）。 */
function abortedPromise(signal: AbortSignal): Promise<never> {
  return new Promise((_, reject) => {
    if (signal.aborted) reject(signal.reason);
    else signal.addEventListener("abort", () => reject(signal.reason), { once: true });
  });
}

function documentHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

/**
 * チャットの回答の処理の状態を追う（3 製品共通。#1160）。
 *
 * - 段階の一覧・経過時間・遅延の案内は `ChatProgress` が出す。この hook は終端の判定（`active`）と、
 *   配信が途絶えたときの扱いを持つ。
 * - 配信（polling の応答・SSE のイベント）が `staleAfterMs` 届かなければ `refresh` を呼び、
 *   「接続を確認しています」を出す。失敗・時間切れは 1 秒・2 秒・4 秒 … `maxBackoffMs` の間隔で続け、
 *   終端（`active` が false）まで追う。配信が届いたら案内を消し、間隔を初めに戻す。
 * - タブが非表示の間は取り直さず、表示に戻ったら待たずに確かめる（polling もタブが非表示の間は止まるため）。
 */
export function useChatProgressTracker({
  key,
  steps,
  active: activeProp,
  elapsedMs,
  receivedAt,
  refresh,
  staleAfterMs = DEFAULT_CHAT_PROGRESS_STALE_AFTER_MS,
  refreshTimeoutMs = DEFAULT_CHAT_PROGRESS_REFRESH_TIMEOUT_MS,
  maxBackoffMs = DEFAULT_CHAT_PROGRESS_MAX_BACKOFF_MS,
  enabled = true,
}: ChatProgressTrackerOptions): ChatProgressTracker {
  const active = activeProp ?? isChatProgressActive(steps);
  const tracking = enabled && active && key !== null && refresh !== undefined;

  const refreshRef = useRef(refresh);
  useLayoutEffect(() => {
    refreshRef.current = refresh;
  });

  /** 最後に配信を受け取った時刻（`receivedAt` と `touch()` の新しい方）。描画せずに更新する。 */
  const lastActivityRef = useRef(0);
  useLayoutEffect(() => {
    if (receivedAt !== undefined && receivedAt > lastActivityRef.current) lastActivityRef.current = receivedAt;
  }, [receivedAt]);

  const [stall, setStall] = useState<StallState | null>(null);
  const stallRef = useRef<StallState | null>(null);
  /** 取り直している間に `touch()` で届いた配信の時刻（案内を消すために描画する）。 */
  const [touchedAt, setTouchedAt] = useState(0);
  const touchedAtRef = useRef(0);
  /** 監視の処理（effect の中の check）を、`refreshNow()` から呼ぶため。 */
  const checkNowRef = useRef<(() => void) | null>(null);

  const touch = useCallback(() => {
    const now = Date.now();
    lastActivityRef.current = Math.max(lastActivityRef.current, now);
    // 取り直しを始めた後に初めて届いたときだけ描画する（SSE の delta ごとに描画し直さない）。
    const current = stallRef.current;
    if (current && now > current.at && touchedAtRef.current <= current.at) {
      touchedAtRef.current = now;
      setTouchedAt(now);
    }
  }, []);

  const refreshNow = useCallback(() => {
    checkNowRef.current?.();
  }, []);

  useEffect(() => {
    if (!tracking || key === null) return undefined;
    const trackedKey = key;
    // 追い始め（対象が変わった・終端から戻った）は、配信を受け取ったものとして数え始める。
    lastActivityRef.current = Math.max(lastActivityRef.current, Date.now());
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | null = null;
    let refreshing = false;
    let failures = 0;
    /** `refreshNow()` の時刻。これより後に配信が届くまでは、時間に関係なく途絶えとして取り直す。 */
    let forcedAt: number | null = null;

    const wait = (ms: number) => {
      if (timer !== undefined) clearTimeout(timer);
      timer = setTimeout(check, Math.max(0, ms));
    };

    const startStall = (at: number) => {
      const next = { key: trackedKey, at };
      stallRef.current = next;
      setStall(next);
    };

    async function attempt() {
      refreshing = true;
      const startedAt = Date.now();
      if (failures === 0) startStall(startedAt);
      const current = new AbortController();
      controller = current;
      const timeout = setTimeout(() => current.abort(new DOMException("chat progress refresh timed out", "TimeoutError")), refreshTimeoutMs);
      try {
        // 応答しない取り直し（signal を見ない関数・終わらない promise）も、上限で打ち切って次へ進む。
        await Promise.race([Promise.resolve(refreshRef.current?.(current.signal)), abortedPromise(current.signal)]);
      } catch {
        // 取り直しの失敗は、次の取り直しで回復する（backoff して続ける）。
      } finally {
        clearTimeout(timeout);
        refreshing = false;
      }
      if (disposed) return;
      if (lastActivityRef.current > startedAt) {
        // 取り直しで配信が届いた。途絶えの判定を初めから数える。
        failures = 0;
        check();
        return;
      }
      failures += 1;
      wait(chatProgressBackoffMs(failures, maxBackoffMs));
    }

    function check() {
      if (disposed || refreshing) return;
      if (forcedAt !== null && lastActivityRef.current > forcedAt) forcedAt = null;
      const idle = Date.now() - lastActivityRef.current;
      if (forcedAt === null && idle < staleAfterMs) {
        failures = 0;
        wait(staleAfterMs - idle);
        return;
      }
      // タブが非表示の間は取り直さない（表示に戻ったら visibilitychange で確かめる）。
      if (documentHidden()) return;
      void attempt();
    }

    checkNowRef.current = () => {
      forcedAt = Date.now();
      if (timer !== undefined) clearTimeout(timer);
      check();
    };

    const onVisibility = () => {
      if (!documentHidden()) check();
    };
    document.addEventListener("visibilitychange", onVisibility);
    wait(staleAfterMs);

    return () => {
      disposed = true;
      if (timer !== undefined) clearTimeout(timer);
      controller?.abort();
      checkNowRef.current = null;
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [tracking, key, staleAfterMs, refreshTimeoutMs, maxBackoffMs]);

  // 取り直しを始めた後に配信が届いていなければ「接続を確認しています」。
  const lastSeen = Math.max(receivedAt ?? 0, touchedAt);
  const reconnecting = tracking && stall !== null && stall.key === key && lastSeen <= stall.at;

  return {
    active,
    reconnecting,
    touch,
    refreshNow,
    progressProps: { steps, active, elapsedMs, reconnecting, progressKey: key },
  };
}
